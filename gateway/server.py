"""HTTP front-end for the gateway (stdlib only).

Endpoints
  POST /v1/diagnose   Authorization: Bearer <projected SA token>
  POST /v1/runbooks/sync
  GET  /v1/maintenance  cluster-wide pause switch (same auth)
  GET  /healthz       process is up
  GET  /readyz        model reachable and pulled
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from gateway.adapters import KubeTokenReviewer, OllamaLLM
from gateway.core import Authenticator, Gateway, GatewayConfig

log = logging.getLogger("kubelantern.gateway")


def make_handler(gw: Gateway, ready_fn):
    class Handler(BaseHTTPRequestHandler):
        server_version = "kubelantern-gateway"
        sys_version = ""

        def _send(self, status: int, payload: dict) -> None:
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            if "retry_after_seconds" in payload:
                self.send_header("Retry-After", str(payload["retry_after_seconds"]))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/healthz":
                return self._send(200, {"status": "ok"})
            if self.path == "/readyz":
                ok, detail = ready_fn()
                return self._send(200 if ok else 503, {"ready": ok, "detail": detail})
            if self.path == "/v1/maintenance":
                status, payload = gw.maintenance(self.headers.get("Authorization"))
                return self._send(status, payload)
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            if self.path not in ("/v1/diagnose", "/v1/runbooks/sync"):
                return self._send(404, {"error": "not found"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._send(400, {"error": "bad content-length"})
            if length > gw.config.max_body_bytes * 4:
                return self._send(400, {"error": "request too large"})
            body = self.rfile.read(length) if length else b""
            handler = gw.sync_runbooks if self.path == "/v1/runbooks/sync" else gw.diagnose
            status, payload = handler(self.headers.get("Authorization"), body)
            return self._send(status, payload)

        def log_message(self, fmt, *args):  # quiet default access log; audit log covers it
            log.debug("%s %s", self.address_string(), fmt % args)

    return Handler


def serve(gw: Gateway, ready_fn, host: str, port: int) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), make_handler(gw, ready_fn))
    httpd.daemon_threads = True
    return httpd


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="kubelantern-gateway")
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    p.add_argument("--ollama-url", default=os.environ.get("OLLAMA_URL", "http://ollama:11434"))
    p.add_argument("--model", default=os.environ.get("KUBELANTERN_MODEL", "qwen2.5:1.5b"))
    p.add_argument("--audience", default=os.environ.get("KUBELANTERN_AUDIENCE", "kubelantern-gateway"))
    p.add_argument("--rate-per-minute", type=float,
                   default=float(os.environ.get("KUBELANTERN_RATE_PER_MINUTE", "6")))
    p.add_argument("--pull", action="store_true",
                   default=os.environ.get("KUBELANTERN_PULL_MODEL", "true").lower() == "true",
                   help="pull the model in the background if missing")
    p.add_argument("--qdrant-url", default=os.environ.get("QDRANT_URL", ""),
                   help="enable runbook retrieval (Stage 7); empty = off")
    p.add_argument("--embed-model", default=os.environ.get("KUBELANTERN_EMBED_MODEL", "nomic-embed-text"))
    p.add_argument("--shared-runbooks", default=os.environ.get("KUBELANTERN_SHARED_RUNBOOKS",
                                                               "/etc/kubelantern/runbooks"),
                   help="folder of shared runbooks (*.md), one sub-folder per mounted ConfigMap")
    p.add_argument("--runbook-reload-seconds", type=float,
                   default=float(os.environ.get("KUBELANTERN_RUNBOOK_RELOAD_SECONDS", "30")),
                   help="how often to check the shared runbooks for changes; 0 = load once")
    args = p.parse_args(argv)

    logging.basicConfig(level=os.environ.get("KUBELANTERN_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s", stream=sys.stderr)

    llm = OllamaLLM(args.ollama_url, args.model)
    knowledge = None
    if args.qdrant_url:
        from gateway.knowledge import KnowledgeBase, OllamaEmbedder, QdrantStore

        knowledge = KnowledgeBase(OllamaEmbedder(args.ollama_url, args.embed_model),
                                  QdrantStore(args.qdrant_url),
                                  min_score=float(os.environ.get("KUBELANTERN_RUNBOOK_MIN_SCORE", "0.35")))
    from kubelantern_common.maintenance import read_dir

    maintenance_dir = os.environ.get("KUBELANTERN_MAINTENANCE_DIR", "/etc/kubelantern/maintenance")
    gw = Gateway(
        GatewayConfig(audience=args.audience, rate_per_minute=args.rate_per_minute),
        Authenticator(KubeTokenReviewer(), args.audience),
        llm,
        knowledge=knowledge,
        # Re-read on every call: the kubelet refreshes the mounted ConfigMap, so
        # the switch takes effect without restarting anything.
        maintenance=lambda: read_dir(maintenance_dir),
    )

    def ensure_model(m: OllamaLLM) -> None:
        # Ollama may still be starting; keep trying until the model is present.
        while True:
            ok, detail = m.ready()
            if ok:
                return
            if "not pulled" in detail and args.pull:
                try:
                    m.pull()
                    continue
                except Exception:
                    log.exception("pull of %s failed; retrying", m.model)
            else:
                log.info("waiting for ollama (%s): %s", m.model, detail)
            time.sleep(15)

    def startup() -> None:
        ensure_model(llm)
        if knowledge is None:
            return
        ensure_model(OllamaLLM(args.ollama_url, args.embed_model))
        while True:  # Qdrant may still be starting
            try:
                knowledge.load_shared(args.shared_runbooks)
                break
            except Exception as e:  # noqa: BLE001 — retry until dependencies are up
                log.info("waiting for knowledge base: %s", e)
                time.sleep(15)
        # Shared runbooks live in mounted ConfigMaps; the kubelet updates the files
        # in place, so edits are picked up here without a restart or a new image.
        while args.runbook_reload_seconds > 0:
            time.sleep(args.runbook_reload_seconds)
            try:
                knowledge.load_shared(args.shared_runbooks, force=False)
            except Exception as e:  # noqa: BLE001 — keep serving the last good set
                log.warning("reloading shared runbooks failed (keeping the previous set): %s", e)

    threading.Thread(target=startup, name="startup", daemon=True).start()

    httpd = serve(gw, llm.ready, "0.0.0.0", args.port)
    log.info("gateway listening on :%d model=%s ollama=%s runbooks=%s", args.port, args.model,
             args.ollama_url, args.qdrant_url or "off")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
