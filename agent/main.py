"""KubeLantern namespace agent — entrypoint.

Pipeline:  PodWatcher -> IncidentManager -> (DiagnosticCollector) -> output

Namespace resolution order:
  1. --namespace flag
  2. KUBELANTERN_NAMESPACE env var
  3. the pod's own namespace (service account mount)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

from kubernetes import config

from agent.collector.collector import DiagnosticCollector, format_evidence
from agent.diagnosis.client import (
    DiagnosisWorker,
    GatewayClient,
    format_diagnosis,
    format_diagnosis_error,
)
from agent.incident.manager import IncidentManager, IncidentUpdate, format_update
from agent.watcher.detector import Failure
from agent.watcher.pod_watcher import PodWatcher

SA_NAMESPACE_FILE = Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace")
log = logging.getLogger("kubelantern")


def resolve_namespace(cli_value: str | None) -> str:
    if cli_value:
        return cli_value
    if os.environ.get("KUBELANTERN_NAMESPACE"):
        return os.environ["KUBELANTERN_NAMESPACE"]
    if SA_NAMESPACE_FILE.exists():
        return SA_NAMESPACE_FILE.read_text().strip()
    raise SystemExit("namespace not set: use --namespace or KUBELANTERN_NAMESPACE")


def load_kube_config() -> None:
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()


class Agent:
    """Glues watcher, incident manager, collector and output together."""

    def __init__(self, namespace: str, manager: IncidentManager, collector: DiagnosticCollector,
                 output: str = "text", log_lines: int = 5, diagnosis=None, store=None) -> None:
        self.namespace = namespace
        self.manager = manager
        self.collector = collector
        self.output = output
        self.log_lines = log_lines
        self._print_lock = threading.Lock()
        # Optional DiagnosisWorker: only OPENED / CAUSE CHANGED go to the LLM.
        self.diagnosis = diagnosis
        # Optional IncidentStore: incidents persisted as objects in this namespace.
        self.store = store
        # Restored incidents that never got a diagnosis (agent restarted mid-way).
        self._undiagnosed: set[str] = set()

    def restore(self) -> list[dict]:
        """Pick up open incidents persisted before a restart (no new OPENED)."""
        if self.store is None:
            return []
        try:
            records = self.store.load_open()
        except Exception:
            log.exception("loading persisted incidents failed; starting fresh")
            return []
        restored = self.manager.restore(records, time.time())
        self._undiagnosed = {r["id"] for r in restored if not self.store.has_diagnosis(r["id"])}
        if restored:
            self._print("Restored open incident(s): " + ", ".join(r["id"] for r in restored))
        return restored

    # watcher callbacks
    def on_failure(self, f: Failure) -> None:
        updates = self.manager.observe(f, time.time())
        for u in updates:
            self.emit(u)
        if not updates and self._undiagnosed:
            self._diagnose_restored(f)

    def _diagnose_restored(self, f: Failure) -> None:
        inc = self.manager.incident_for(f)
        if inc is None or inc["id"] not in self._undiagnosed:
            return
        self._undiagnosed.discard(inc["id"])
        if self.diagnosis is None:
            return
        try:
            bundle = self.collector.collect(f)
        except Exception:
            log.exception("evidence collection failed for incident %s", inc["id"])
            return
        self.diagnosis.submit(inc, bundle)

    def on_clear(self, namespace: str, pod: str, container: str) -> None:
        self.manager.clear(namespace, pod, container, time.time())

    # periodic
    def tick(self) -> None:
        for u in self.manager.tick(time.time()):
            self.emit(u)

    def emit(self, u: IncidentUpdate) -> None:
        bundle = None
        if u.needs_evidence and u.failure is not None:
            try:
                bundle = self.collector.collect(u.failure)
            except Exception:
                log.exception("evidence collection failed for incident %s", u.incident["id"])

        if self.output == "json":
            out = json.dumps({"event": u.kind, "incident": u.incident, "evidence": bundle}, default=str)
        else:
            out = format_update(u, time.time())
            if bundle is not None:
                out += "\n" + format_evidence(bundle, log_lines=self.log_lines, header=False)
        self._print(out)

        if self.store is not None:
            self.store.save(u.incident)
        if self.diagnosis is not None and u.needs_evidence and bundle is not None:
            self.diagnosis.submit(u.incident, bundle)

    # diagnosis callbacks (run on the diagnosis worker thread)
    def on_diagnosis(self, incident: dict, result: dict) -> None:
        if self.store is not None:
            self.store.save_diagnosis(incident["id"], result)
        if self.output == "json":
            self._print(json.dumps({"event": "diagnosis", "incident_id": incident["id"], **result}))
        else:
            self._print(format_diagnosis(incident, result))

    def on_diagnosis_error(self, incident: dict, error: str) -> None:
        log.warning("diagnosis for %s failed: %s", incident["id"], error)
        if self.output == "json":
            self._print(json.dumps({"event": "diagnosis_error", "incident_id": incident["id"],
                                    "error": error}))
        else:
            self._print(format_diagnosis_error(incident, error))

    def _print(self, text: str) -> None:
        with self._print_lock:
            print(text + "\n", flush=True)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="kubelantern-agent")
    parser.add_argument("--namespace", "-n", help="namespace to watch (one only)")
    parser.add_argument("--log-level", default=os.environ.get("KUBELANTERN_LOG_LEVEL", "INFO"))
    parser.add_argument(
        "--output",
        choices=["text", "json"],
        default=os.environ.get("KUBELANTERN_OUTPUT", "text"),
        help="text = human summary, json = incident + evidence bundle per update",
    )
    parser.add_argument(
        "--log-lines",
        type=int,
        default=int(os.environ.get("KUBELANTERN_LOG_LINES", "5")),
        help="log lines to show per section in text output",
    )
    parser.add_argument(
        "--reminder-minutes",
        type=float,
        default=_env_float("KUBELANTERN_REMINDER_MINUTES", 30),
        help="re-announce an incident that is still failing after this long",
    )
    parser.add_argument(
        "--resolve-after-seconds",
        type=float,
        default=_env_float("KUBELANTERN_RESOLVE_AFTER_SECONDS", 300),
        help="resolve an incident after its pods have been healthy this long",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )

    namespace = resolve_namespace(args.namespace)
    load_kube_config()

    agent = Agent(
        namespace,
        IncidentManager(
            reminder_seconds=args.reminder_minutes * 60,
            resolve_after_seconds=args.resolve_after_seconds,
        ),
        DiagnosticCollector(namespace),
        output=args.output,
        log_lines=args.log_lines,
    )
    if os.environ.get("KUBELANTERN_INCIDENT_STORE", "true").lower() == "true":
        from agent.incident.store import IncidentStore

        agent.store = IncidentStore(
            namespace,
            history=int(_env_float("KUBELANTERN_INCIDENT_HISTORY", 50)),
            retention_days=_env_float("KUBELANTERN_INCIDENT_RETENTION_DAYS", 30),
        ).start()
    gateway_url = os.environ.get("KUBELANTERN_GATEWAY_URL", "").strip()
    token_path = os.environ.get("KUBELANTERN_TOKEN_PATH", "/var/run/secrets/kubelantern/token")
    stop = threading.Event()
    if gateway_url:
        agent.diagnosis = DiagnosisWorker(
            GatewayClient(gateway_url, token_path),
            on_result=agent.on_diagnosis,
            on_error=agent.on_diagnosis_error,
        ).start()
        sync_seconds = _env_float("KUBELANTERN_RUNBOOK_SYNC_SECONDS", 0)
        if sync_seconds > 0:
            from agent.diagnosis.runbooks import RunbookSync

            syncer = RunbookSync(namespace, gateway_url, token_path, interval=sync_seconds)
            threading.Thread(target=syncer.run_forever, args=(stop,), name="runbook-sync",
                             daemon=True).start()
    watcher = PodWatcher(namespace=namespace, on_failure=agent.on_failure, on_clear=agent.on_clear)

    def ticker():
        last_prune = 0.0
        while not stop.wait(10):
            try:
                agent.tick()
            except Exception:
                log.exception("incident tick failed")
            if agent.store is not None and time.time() - last_prune >= 600:
                last_prune = time.time()
                try:
                    agent.store.prune()
                except Exception:
                    log.exception("incident pruning failed")

    threading.Thread(target=ticker, name="incident-ticker", daemon=True).start()

    def _shutdown(*_):
        # The watch stream blocks in a socket read, so raising SystemExit is not
        # reliable; exit immediately (nothing to persist yet) to avoid SIGKILL.
        log.info("shutting down")
        stop.set()
        if agent.store is not None:
            try:
                agent.store.flush()   # don't lose the last incident update
            except Exception:
                log.exception("flushing incidents failed")
        watcher.stop()
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    print(
        f"KubeLantern agent started — namespace: {namespace} "
        f"(resolve after {int(args.resolve_after_seconds)}s healthy, "
        f"reminder every {args.reminder_minutes:g}m, "
        f"diagnosis: {gateway_url or 'off'})\n",
        flush=True,
    )
    agent.restore()   # after the banner, before watching: no duplicate OPENED
    watcher.run()


if __name__ == "__main__":
    main()
