"""Agent-side runbook sync (Stage 7).

Reads `Runbook` objects (kubelantern.io/v1alpha1) in the agent's own namespace and
pushes the full set to the gateway. The gateway stamps the namespace from the
agent's token, so this code cannot write another namespace's knowledge even if
it tried.

Polling (not a watch) keeps it simple and robust: a list every N seconds,
push only when the set changed, plus a periodic re-push so the knowledge base
converges after a gateway/Qdrant restart.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger("kubelantern.runbooks")

GROUP, VERSION, PLURAL = "kubelantern.io", "v1alpha1", "runbooks"


def to_payload(items: list[dict]) -> list[dict]:
    out = []
    for it in items:
        spec = it.get("spec") or {}
        out.append({
            "name": (it.get("metadata") or {}).get("name"),
            "title": spec.get("title"),
            "content": spec.get("content"),
            "category": spec.get("category"),
            "workloads": spec.get("workloads") or [],
        })
    return sorted(out, key=lambda r: r["name"] or "")


def digest(payload: list[dict]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


class RunbookSync:
    def __init__(self, namespace: str, gateway_url: str, token_path: str, custom_api=None,
                 interval: float = 30, resync_every: float = 600, clock=time.monotonic) -> None:
        self.namespace = namespace
        self.url = gateway_url.rstrip("/") + "/v1/runbooks/sync"
        self.token_path = Path(token_path)
        self.interval = interval
        self.resync_every = resync_every
        self.clock = clock
        if custom_api is None:
            from kubernetes import client

            custom_api = client.CustomObjectsApi()
        self.api = custom_api
        self.last_digest: str | None = None
        self.last_push: float | None = None
        self.crd_missing_logged = False

    def list_runbooks(self) -> list[dict] | None:
        try:
            res = self.api.list_namespaced_custom_object(GROUP, VERSION, self.namespace, PLURAL)
        except Exception as e:
            if getattr(e, "status", None) == 404:
                if not self.crd_missing_logged:
                    log.info("Runbook CRD not installed; team runbooks disabled")
                    self.crd_missing_logged = True
                return None
            raise
        self.crd_missing_logged = False
        return res.get("items", [])

    def push(self, payload: list[dict]) -> dict:
        req = urllib.request.Request(
            self.url, method="POST",
            data=json.dumps({"runbooks": payload}).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.token_path.read_text().strip()}"})
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read())

    def sync_once(self) -> str:
        """Returns what happened: 'pushed' | 'unchanged' | 'disabled' | 'error: …'."""
        try:
            items = self.list_runbooks()
        except Exception as e:  # noqa: BLE001
            return f"error: list failed ({getattr(e, 'status', '')} {type(e).__name__})"
        if items is None:
            return "disabled"
        payload = to_payload(items)
        d = digest(payload)
        now = self.clock()
        due = self.last_push is None or now - self.last_push >= self.resync_every
        if d == self.last_digest and not due:
            return "unchanged"
        try:
            out = self.push(payload)
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read()).get("error", "")
            except (ValueError, OSError):
                detail = ""
            return f"error: gateway {e.code} {detail}".rstrip()
        except (urllib.error.URLError, OSError) as e:
            return f"error: gateway unreachable ({e})"
        changed = d != self.last_digest
        self.last_digest, self.last_push = d, now
        if changed:
            log.info("synced %d runbook(s) (%d chunks) to the knowledge base",
                     out.get("runbooks", 0), out.get("chunks", 0))
        return "pushed"

    def run_forever(self, stop: threading.Event) -> None:
        while not stop.is_set():
            result = self.sync_once()
            if result.startswith("error"):
                log.warning("runbook sync: %s", result)
            stop.wait(self.interval)
