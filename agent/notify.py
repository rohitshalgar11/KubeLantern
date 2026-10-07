"""Agent side of notifications: hand events to the notifier sidecar on localhost.

The agent never holds webhook URLs. It POSTs structured events to the sidecar
(127.0.0.1) from a background thread, so the pod watcher never waits on Teams
or Slack.

Default events: `diagnosis` and `resolved` — one message when the cause is
known, one when it's over. `opened` is added automatically when AI diagnosis
is off (otherwise nobody would hear about the incident).
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
import urllib.request
from collections.abc import Callable
from typing import Any

log = logging.getLogger("kubelantern.notify")

DEFAULT_EVENTS = ("diagnosis", "resolved")

# IncidentManager update kinds -> notification kinds
UPDATE_KINDS = {"opened": "opened", "cause_changed": "cause_changed",
                "scope_changed": "scope_changed", "reminder": "ongoing", "resolved": "resolved"}


def _post(url: str, event: dict, timeout: float = 5) -> int:
    req = urllib.request.Request(url, data=json.dumps(event, default=str).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status


class NotifyClient:
    def __init__(self, url: str, events: set[str] | None = None, detail: str = "summary",
                 post: Callable[[str, dict], int] = _post,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.url = url
        self.events = set(events or DEFAULT_EVENTS)
        self.detail = detail
        self.post, self.sleep = post, sleep
        self._queue: queue.Queue[dict | None] = queue.Queue(maxsize=200)

    def wants(self, kind: str) -> bool:
        return kind in self.events

    def start(self) -> NotifyClient:
        threading.Thread(target=self._run, name="notify-client", daemon=True).start()
        return self

    def notify(self, kind: str, incident: dict, *, diagnosis: dict | None = None,
               previous_cause: str | None = None, previous_pod_count: int | None = None,
               evidence: list[str] | None = None, error: str | None = None) -> bool:
        if not self.wants(kind):
            return False
        event: dict[str, Any] = {
            "kind": kind, "incident": incident, "diagnosis": diagnosis,
            "previous_cause": previous_cause, "previous_pod_count": previous_pod_count,
            "evidence": evidence if self.detail == "full" else None, "error": error,
        }
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            log.warning("notification queue full; dropped %s for %s", kind, incident.get("id"))
            return False
        return True

    def flush(self) -> None:
        while True:
            try:
                event = self._queue.get_nowait()
            except queue.Empty:
                return
            if event is not None:
                self._send(event)

    def _run(self) -> None:
        while True:
            event = self._queue.get()
            if event is None:
                return
            self._send(event)

    def _send(self, event: dict) -> None:
        # The sidecar may still be starting: a few quick retries, then give up.
        for attempt in range(4):
            try:
                self.post(self.url, event)
                return
            except Exception as e:  # noqa: BLE001
                if attempt == 3:
                    log.warning("notifier unreachable; dropped %s for %s: %s",
                                event["kind"], event["incident"].get("id"), e)
                    return
                self.sleep(2 * (attempt + 1))
