"""Incident persistence — KubeLantern `Incident` objects in the agent's namespace.

Why: incidents used to live only in the agent's memory, so every restart
(upgrade, node drain, ArgoCD sync) re-opened still-failing workloads under a new
ID and re-diagnosed them. Persisted incidents survive restarts, give teams a
history (`kubectl get incidents`), and keep IDs stable for notifications.

The agent's only write permission is this: create/update/delete Incident
objects in its OWN namespace. It never writes workloads.

Writes happen on a background thread so the pod watcher never blocks on the
API server. If the CRD is not installed, persistence switches itself off (and
re-checks every few minutes) — the agent keeps working from memory.
"""

from __future__ import annotations

import logging
import queue
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("kubelantern.incidents")

GROUP, VERSION, PLURAL, KIND = "kubelantern.io", "v1alpha1", "incidents", "Incident"
STATUS_LABEL = "kubelantern.io/status"
WORKLOAD_LABEL = "kubelantern.io/workload"


def object_name(incident_id: str) -> str:
    """Kubernetes object names are lowercase; IDs keep their 'INC' for humans."""
    return re.sub(r"[^a-z0-9-]", "-", incident_id.lower())[:253].strip("-")


def _label_value(v: str) -> str:
    v = re.sub(r"[^A-Za-z0-9._-]", "-", v)[:63]
    return re.sub(r"^[^A-Za-z0-9]+|[^A-Za-z0-9]+$", "", v)


def _ts(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch(ts: str | None) -> float | None:
    if not ts:
        return None
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def _pods(d: dict[str, int]) -> list[dict[str, Any]]:
    return [{"pod": p, "restarts": r} for p, r in sorted(d.items())]


def diagnosis_summary(result: dict) -> dict[str, Any]:
    """The part of a gateway diagnosis worth keeping on the incident."""
    d = result.get("diagnosis") or {}
    out = {
        "category": d.get("category"),
        "confidence": d.get("confidence"),
        "summary": d.get("summary"),
        "probableCause": d.get("probable_cause"),
        "nextSteps": list(d.get("next_steps") or [])[:10],
        "suggestedFix": d.get("suggested_fix"),
        "escalation": d.get("escalation"),
        "runbooks": [r.get("source") for r in result.get("references") or [] if r.get("source")],
        "model": result.get("model"),
    }
    return {k: v for k, v in out.items() if v not in (None, "", [])}


def to_object(record: dict[str, Any], diagnosis: dict | None, now: float) -> dict[str, Any]:
    labels = record.get("cause_history") or [record["cause"]]
    times = record.get("cause_history_at") or []
    status = {
        "state": "Open" if record["status"] == "open" else "Resolved",
        "cause": record["cause"],
        "causeFamily": record.get("cause_family"),
        "exitCode": record.get("exit_code"),
        "causeHistory": [
            {"cause": c, "at": _ts(times[i] if i < len(times) else record["opened_at"])}
            for i, c in enumerate(labels)
        ],
        "failingPods": _pods(record.get("failing_pods") or {}),
        "affectedPods": _pods(record.get("affected_pods") or {}),
        "peakFailing": record.get("peak_failing") or 0,
        "observations": record.get("observations") or 0,
        "lastSeenAt": _ts(record.get("last_seen_at")),
        "lastNotifiedAt": _ts(record.get("last_notified_at")),
        "resolvedAt": _ts(record.get("resolved_at")),
        "updatedAt": _ts(now),
    }
    if diagnosis:
        status["diagnosis"] = diagnosis
    return {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": KIND,
        "metadata": {
            "name": object_name(record["id"]),
            # No ArgoCD tracking label: these objects are runtime state, not Git.
            "labels": {
                "app.kubernetes.io/part-of": "kubelantern",
                "app.kubernetes.io/managed-by": "kubelantern-agent",
                STATUS_LABEL: record["status"],
                WORKLOAD_LABEL: _label_value(record.get("workload_name") or ""),
            },
        },
        "spec": {
            "id": record["id"],
            "workload": {"kind": record.get("workload_kind"), "name": record.get("workload_name")},
            "container": record["container"],
            "openedAt": _ts(record["opened_at"]),
        },
        "status": {k: v for k, v in status.items() if v is not None},
    }


def from_object(obj: dict[str, Any], namespace: str) -> dict[str, Any]:
    """Back to the manager's record format (inverse of to_object)."""
    spec, st = obj.get("spec") or {}, obj.get("status") or {}
    history = st.get("causeHistory") or []
    return {
        "id": spec["id"],
        "namespace": namespace,
        "status": "open" if st.get("state") == "Open" else "resolved",
        "workload_kind": (spec.get("workload") or {}).get("kind"),
        "workload_name": (spec.get("workload") or {}).get("name"),
        "container": spec.get("container"),
        "cause": st.get("cause"),
        "cause_family": st.get("causeFamily"),
        "exit_code": st.get("exitCode"),
        "cause_history": [h["cause"] for h in history],
        "cause_history_at": [_epoch(h.get("at")) for h in history],
        "opened_at": _epoch(spec.get("openedAt")),
        "last_seen_at": _epoch(st.get("lastSeenAt")),
        "last_notified_at": _epoch(st.get("lastNotifiedAt")),
        "resolved_at": _epoch(st.get("resolvedAt")),
        "affected_pods": {p["pod"]: p.get("restarts", 0) for p in st.get("affectedPods") or []},
        "peak_failing": st.get("peakFailing") or 0,
        "observations": st.get("observations") or 0,
        "diagnosis": st.get("diagnosis"),
    }


class IncidentStore:
    def __init__(self, namespace: str, api=None, history: int = 50, retention_days: float = 30,
                 recheck_seconds: float = 300, clock=time.time) -> None:
        self.namespace = namespace
        self._api = api
        self.history = history
        self.retention_seconds = retention_days * 86400
        self.recheck_seconds = recheck_seconds
        self.clock = clock
        self.enabled: bool | None = None  # None = not checked yet
        self._checked_at = 0.0
        self._records: dict[str, dict] = {}     # latest record per incident id
        self._diagnoses: dict[str, dict] = {}   # latest diagnosis summary per id
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def api(self):
        if self._api is None:
            from kubernetes import client

            self._api = client.CustomObjectsApi()
        return self._api

    # -- availability --------------------------------------------------------

    def available(self) -> bool:
        now = self.clock()
        if self.enabled is None or (not self.enabled and now - self._checked_at >= self.recheck_seconds):
            self._checked_at = now
            try:
                self.api.list_namespaced_custom_object(GROUP, VERSION, self.namespace, PLURAL, limit=1)
                if self.enabled is not True:
                    log.info("incident persistence on (kubelantern.io Incident objects)")
                self.enabled = True
            except Exception as e:  # noqa: BLE001
                status = getattr(e, "status", None)
                if self.enabled is not False:
                    if status == 404:
                        log.info("Incident CRD not installed; incidents are kept in memory only")
                    else:
                        log.warning("incident persistence unavailable: %s %s", status or "",
                                    getattr(e, "reason", None) or type(e).__name__)
                self.enabled = False
        return bool(self.enabled)

    # -- reads -----------------------------------------------------------------

    def load_open(self) -> list[dict[str, Any]]:
        if not self.available():
            return []
        resp = self.api.list_namespaced_custom_object(
            GROUP, VERSION, self.namespace, PLURAL, label_selector=f"{STATUS_LABEL}=open")
        records = []
        for obj in resp.get("items", []):
            try:
                r = from_object(obj, self.namespace)
            except (KeyError, TypeError, ValueError):
                log.warning("skipping unreadable incident %s", obj.get("metadata", {}).get("name"))
                continue
            with self._lock:
                self._records[r["id"]] = r
                if r.get("diagnosis"):
                    self._diagnoses[r["id"]] = r["diagnosis"]
            records.append(r)
        return records

    def has_diagnosis(self, incident_id: str) -> bool:
        with self._lock:
            return incident_id in self._diagnoses

    # -- writes (asynchronous) -------------------------------------------------

    def start(self) -> IncidentStore:
        self._thread = threading.Thread(target=self._run, name="incident-store", daemon=True)
        self._thread.start()
        return self

    def save(self, record: dict[str, Any]) -> None:
        with self._lock:
            self._records[record["id"]] = record
        self._queue.put(record["id"])

    def save_diagnosis(self, incident_id: str, result: dict) -> None:
        with self._lock:
            self._diagnoses[incident_id] = diagnosis_summary(result)
            known = incident_id in self._records
        if known:
            self._queue.put(incident_id)

    def flush(self) -> None:
        """Write everything queued (synchronously); used by tests and shutdown."""
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            if item is not None:
                self._write(item)

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            try:
                self._write(item)
            except Exception:
                log.exception("writing incident %s failed", item)

    def _write(self, incident_id: str) -> None:
        with self._lock:
            record = self._records.get(incident_id)
            diagnosis = self._diagnoses.get(incident_id)
        if record is None:
            return
        try:
            if not self.available():
                return
            body = to_object(record, diagnosis, self.clock())
            name = body["metadata"]["name"]
            try:
                self.api.patch_namespaced_custom_object(GROUP, VERSION, self.namespace, PLURAL,
                                                        name, body)
            except Exception as e:
                if getattr(e, "status", None) != 404:
                    raise
                self.api.create_namespaced_custom_object(GROUP, VERSION, self.namespace, PLURAL,
                                                         body)
        finally:
            # A resolved incident is final: forget it so memory stays bounded.
            if record.get("status") == "resolved":
                with self._lock:
                    if self._records.get(incident_id) is record:
                        self._records.pop(incident_id, None)
                        self._diagnoses.pop(incident_id, None)

    # -- retention ---------------------------------------------------------------

    def prune(self) -> int:
        """Keep the newest `history` resolved incidents, none older than retention."""
        if not self.available():
            return 0
        resp = self.api.list_namespaced_custom_object(
            GROUP, VERSION, self.namespace, PLURAL, label_selector=f"{STATUS_LABEL}=resolved")
        items = resp.get("items", [])

        def resolved_at(o):
            return _epoch((o.get("status") or {}).get("resolvedAt")) or 0

        items.sort(key=resolved_at, reverse=True)
        now = self.clock()
        doomed = [o for i, o in enumerate(items)
                  if i >= self.history or now - resolved_at(o) > self.retention_seconds]
        for o in doomed:
            name = o["metadata"]["name"]
            try:
                self.api.delete_namespaced_custom_object(GROUP, VERSION, self.namespace, PLURAL, name)
            except Exception as e:  # noqa: BLE001
                if getattr(e, "status", None) != 404:
                    log.warning("pruning incident %s failed: %s", name, e)
            with self._lock:
                spec_id = (o.get("spec") or {}).get("id")
                self._records.pop(spec_id, None)
                self._diagnoses.pop(spec_id, None)
        if doomed:
            log.info("pruned %d resolved incident(s)", len(doomed))
        return len(doomed)
