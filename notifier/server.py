"""Notifier sidecar: accepts events from the agent on 127.0.0.1 and delivers them.

    POST /v1/notify   {"kind": ..., "incident": {...}, ...}   -> 202 (queued)
    GET  /healthz                                              -> 200

Bound to the loopback interface only: other pods can't reach it, and it holds
the only copy of the webhook URLs (mounted Secret).
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from notifier import CHANNELS, KINDS
from notifier.sender import Sender

log = logging.getLogger("kubelantern.notifier")
MAX_BODY = 256 * 1024


def validate(event) -> str | None:
    """Return an error message, or None if the event is acceptable."""
    if not isinstance(event, dict):
        return "body must be a JSON object"
    if event.get("kind") not in KINDS:
        return f"kind must be one of {', '.join(KINDS)}"
    inc = event.get("incident")
    if not isinstance(inc, dict) or not inc.get("id"):
        return "incident with an id is required"
    return None


def make_handler(q: queue.Queue):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):   # keep the log quiet; sends are logged by Sender
            pass

        def _reply(self, code: int, body: dict):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/healthz":
                self._reply(200, {"status": "ok"})
            else:
                self._reply(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/v1/notify":
                return self._reply(404, {"error": "not found"})
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self._reply(413, {"error": "too large"})
            try:
                event = json.loads(self.rfile.read(length) or b"null")
            except json.JSONDecodeError:
                return self._reply(400, {"error": "invalid JSON"})
            problem = validate(event)
            if problem:
                return self._reply(400, {"error": problem})
            try:
                q.put_nowait(event)
            except queue.Full:
                return self._reply(503, {"error": "queue full"})
            self._reply(202, {"status": "queued"})

    return Handler


def run_worker(q: queue.Queue, sender: Sender) -> None:
    while True:
        event = q.get()
        if event is None:
            return
        try:
            sender.send(event)
        except Exception:
            log.exception("notification for %s failed", event.get("incident", {}).get("id"))


def main() -> None:
    logging.basicConfig(level=os.environ.get("KUBELANTERN_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    channels = [c.strip() for c in os.environ.get("KUBELANTERN_NOTIFY_CHANNELS", "teams").split(",")
                if c.strip()]
    unknown = [c for c in channels if c not in CHANNELS]
    if unknown:
        log.error("unknown channel(s) %s; supported: %s", unknown, ", ".join(CHANNELS))
    sender = Sender(
        os.environ.get("KUBELANTERN_NOTIFY_SECRET_DIR", "/etc/kubelantern/notify"),
        channels,
        detail=os.environ.get("KUBELANTERN_NOTIFY_DETAIL", "summary"),
        allow_http=os.environ.get("KUBELANTERN_NOTIFY_ALLOW_HTTP", "false").lower() == "true",
        max_per_minute=int(os.environ.get("KUBELANTERN_NOTIFY_MAX_PER_MINUTE", "20")),
    )
    q: queue.Queue = queue.Queue(maxsize=200)
    threading.Thread(target=run_worker, args=(q, sender), name="notify", daemon=True).start()
    port = int(os.environ.get("KUBELANTERN_NOTIFY_PORT", "8081"))
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(q))
    log.info("notifier listening on 127.0.0.1:%d — channels: %s, detail: %s",
             port, ", ".join(sender.channels) or "none", sender.detail)
    server.serve_forever()


if __name__ == "__main__":
    main()
