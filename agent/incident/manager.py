"""Incident manager — turns a stream of failure observations into incidents.

Key ideas
---------
* A container's *state* flips every restart cycle (Error -> CrashLoopBackOff
  -> Error ...). The *cause* is what stays the same, so incidents are keyed
  on the workload + container, and the cause is tracked as an attribute.
* Replicas of one Deployment share an incident: 3 crashing pods = 1 problem.
* Updates are emitted only when something meaningful happens:
    opened         first observation for this workload/container
    cause_changed  e.g. crash(exit 1) -> oom
    reminder       still failing after `reminder_seconds`
    resolved       no failing pods for `resolve_after_seconds`
* The manager is pure (no Kubernetes calls) and thread-safe; time is passed
  in so it is deterministic under test.
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, field
from typing import Any

from agent.watcher.detector import Failure

IMAGE_REASONS = {"ErrImagePull", "ImagePullBackOff", "InvalidImageName"}
CONFIG_REASONS = {"CreateContainerConfigError", "CreateContainerError"}


SIGNALS = {129: "SIGHUP", 130: "SIGINT", 134: "SIGABRT", 137: "SIGKILL",
           139: "SIGSEGV", 143: "SIGTERM"}


@dataclass(frozen=True)
class Cause:
    family: str  # crash | oom | image-pull | config | evicted
    exit_code: int | None = None

    @property
    def label(self) -> str:
        if self.family == "crash":
            if self.exit_code is None:
                return "crash"
            sig = SIGNALS.get(self.exit_code)
            return f"crash (exit {self.exit_code} {sig})" if sig else f"crash (exit {self.exit_code})"
        if self.family == "oom":
            return "oom (exit 137)"
        return self.family

    def same_as(self, other: Cause) -> bool:
        if self.family != other.family:
            return False
        # Unknown exit code (e.g. first CrashLoopBackOff) matches anything.
        return self.exit_code is None or other.exit_code is None or self.exit_code == other.exit_code


def classify(f: Failure) -> Cause:
    """Map a point-in-time container state to its underlying cause."""
    if f.reason in IMAGE_REASONS:
        return Cause("image-pull")
    if f.reason in CONFIG_REASONS:
        return Cause("config")
    if f.reason == "Evicted":
        return Cause("evicted")
    if f.reason == "OOMKilled" or f.last_termination_reason == "OOMKilled":
        return Cause("oom", 137)
    return Cause("crash", f.exit_code)


IncidentKey = tuple[str, str, str, str]  # namespace, workload_kind, workload_name, container


@dataclass
class Incident:
    id: str
    namespace: str
    workload_kind: str
    workload_name: str
    container: str
    cause: Cause
    opened_at: float
    last_seen_at: float
    last_notified_at: float
    status: str = "open"
    resolved_at: float | None = None
    healthy_since: float | None = None
    failing_pods: dict[str, int] = field(default_factory=dict)  # pod -> restarts
    all_pods: dict[str, int] = field(default_factory=dict)  # pod -> max restarts seen
    observations: int = 0
    cause_history: list[tuple[float, str]] = field(default_factory=list)
    # scope tracking: the most pods ever reported failing at the same time, and
    # when the current count first went above it. Counting concurrent failures
    # (not every pod ever seen) keeps rollouts that replace pods from looking
    # like the problem spreading.
    peak_failing: int = 0
    scope_pending_since: float | None = None

    @property
    def key(self) -> IncidentKey:
        return (self.namespace, self.workload_kind, self.workload_name, self.container)

    @property
    def workload(self) -> str:
        return f"{self.workload_kind}/{self.workload_name}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "namespace": self.namespace,
            "workload": self.workload,
            "workload_kind": self.workload_kind,
            "workload_name": self.workload_name,
            "container": self.container,
            "cause": self.cause.label,
            "cause_family": self.cause.family,
            "exit_code": self.cause.exit_code,
            "opened_at": self.opened_at,
            "last_seen_at": self.last_seen_at,
            "resolved_at": self.resolved_at,
            "failing_pods": dict(self.failing_pods),
            "affected_pods": dict(self.all_pods),
            "observations": self.observations,
            "cause_history": [c for _, c in self.cause_history],
            "cause_history_at": [t for t, _ in self.cause_history],
            "peak_failing": self.peak_failing,
            "last_notified_at": self.last_notified_at,
        }


@dataclass(frozen=True)
class IncidentUpdate:
    kind: str  # opened | cause_changed | scope_changed | reminder | resolved
    incident: dict[str, Any]
    failure: Failure | None = None  # triggering observation (for evidence collection)
    previous_cause: str | None = None
    previous_pod_count: int | None = None

    @property
    def needs_evidence(self) -> bool:
        return self.kind in ("opened", "cause_changed")


def make_incident_id(key: IncidentKey, opened_at: float) -> str:
    ns, _, workload, _ = key
    digest = hashlib.sha1(f"{'/'.join(key)}@{opened_at}".encode()).hexdigest()[:6]
    return f"{ns}-{workload}-INC{digest}"


class IncidentManager:
    def __init__(
        self,
        reminder_seconds: float = 30 * 60,
        resolve_after_seconds: float = 5 * 60,
        history_size: int = 50,
        scope_window_seconds: float = 20,
    ) -> None:
        self.reminder_seconds = reminder_seconds
        self.resolve_after_seconds = resolve_after_seconds
        # Coalesce new pods for this long so "scale to 3" is one update.
        self.scope_window_seconds = scope_window_seconds
        self.history_size = history_size
        self._open: dict[IncidentKey, Incident] = {}
        self._resolved: list[Incident] = []
        self._lock = threading.Lock()

    # -- inputs ------------------------------------------------------------

    def observe(self, f: Failure, now: float) -> list[IncidentUpdate]:
        """A container was seen in a failure state."""
        cause = classify(f)
        key: IncidentKey = (f.namespace, f.workload_kind, f.workload_name or f.pod, f.container)

        with self._lock:
            inc = self._open.get(key)
            if inc is None:
                inc = Incident(
                    id=make_incident_id(key, now),
                    namespace=f.namespace,
                    workload_kind=key[1],
                    workload_name=key[2],
                    container=f.container,
                    cause=cause,
                    opened_at=now,
                    last_seen_at=now,
                    last_notified_at=now,
                    cause_history=[(now, cause.label)],
                )
                self._track(inc, f, now)
                self._open[key] = inc
                self._mark_reported(inc, now)
                return [IncidentUpdate("opened", inc.to_dict(), f)]

            self._track(inc, f, now)

            if not cause.same_as(inc.cause):
                previous = inc.cause.label
                inc.cause = cause
                inc.cause_history.append((now, cause.label))
                self._mark_reported(inc, now)
                return [IncidentUpdate("cause_changed", inc.to_dict(), f, previous_cause=previous)]

            if inc.cause.exit_code is None and cause.exit_code is not None:
                inc.cause = cause  # learned the exit code; not a change
            return []

    def clear(self, namespace: str, pod: str, container: str, now: float) -> None:
        """A previously failing container is no longer failing (or was deleted)."""
        with self._lock:
            for inc in self._open.values():
                if inc.namespace == namespace and inc.container == container and pod in inc.failing_pods:
                    del inc.failing_pods[pod]
                    if not inc.failing_pods and inc.healthy_since is None:
                        inc.healthy_since = now

    def tick(self, now: float) -> list[IncidentUpdate]:
        """Periodic housekeeping: resolve quiet incidents, remind on long ones."""
        updates: list[IncidentUpdate] = []
        with self._lock:
            for key, inc in list(self._open.items()):
                # New pods joined the incident: report once they've settled.
                if (inc.scope_pending_since is not None
                        and now - inc.scope_pending_since >= self.scope_window_seconds
                        and now - inc.last_notified_at >= self.scope_window_seconds):
                    if len(inc.failing_pods) > inc.peak_failing:
                        before = inc.peak_failing
                        self._mark_reported(inc, now)
                        updates.append(IncidentUpdate("scope_changed", inc.to_dict(),
                                                      previous_pod_count=before))
                    else:
                        inc.scope_pending_since = None

                if inc.healthy_since is not None:
                    if now - inc.healthy_since >= self.resolve_after_seconds:
                        inc.status = "resolved"
                        inc.resolved_at = now
                        del self._open[key]
                        self._resolved.append(inc)
                        self._resolved = self._resolved[-self.history_size:]
                        updates.append(IncidentUpdate("resolved", inc.to_dict()))
                elif now - inc.last_notified_at >= self.reminder_seconds:
                    inc.last_notified_at = now
                    updates.append(IncidentUpdate("reminder", inc.to_dict()))
        return updates

    def restore(self, records: list[dict[str, Any]], now: float) -> list[dict[str, Any]]:
        """Re-open incidents persisted before an agent restart.

        Restored incidents keep their ID, cause and history. Which pods are
        failing *now* is unknown until the watcher reports again, so each starts
        tentatively healthy: still-failing pods re-attach silently (same cause,
        no OPENED), and an incident whose pods recovered while the agent was
        down resolves after the usual healthy window.
        """
        restored = []
        with self._lock:
            for r in records:
                if r.get("status") != "open":
                    continue
                key: IncidentKey = (r["namespace"], r["workload_kind"], r["workload_name"],
                                    r["container"])
                if key in self._open:
                    continue
                cause = Cause(r.get("cause_family") or "crash", r.get("exit_code"))
                labels = r.get("cause_history") or [cause.label]
                times = r.get("cause_history_at") or []
                history = [(times[i] if i < len(times) else r["opened_at"], label)
                           for i, label in enumerate(labels)]
                inc = Incident(
                    id=r["id"],
                    namespace=r["namespace"],
                    workload_kind=r["workload_kind"],
                    workload_name=r["workload_name"],
                    container=r["container"],
                    cause=cause,
                    opened_at=r["opened_at"],
                    last_seen_at=r.get("last_seen_at") or now,
                    last_notified_at=r.get("last_notified_at") or now,
                    healthy_since=now,
                    all_pods=dict(r.get("affected_pods") or {}),
                    observations=r.get("observations") or 0,
                    cause_history=history,
                    peak_failing=r.get("peak_failing") or 0,
                )
                self._open[key] = inc
                restored.append(inc.to_dict())
        return restored

    def incident_for(self, f: Failure) -> dict[str, Any] | None:
        """The open incident a failure belongs to, if any."""
        key: IncidentKey = (f.namespace, f.workload_kind, f.workload_name or f.pod, f.container)
        with self._lock:
            inc = self._open.get(key)
            return inc.to_dict() if inc else None

    # -- queries -------------------------------------------------------------

    def open_incidents(self) -> list[dict[str, Any]]:
        with self._lock:
            return [i.to_dict() for i in self._open.values()]

    def resolved_incidents(self) -> list[dict[str, Any]]:
        with self._lock:
            return [i.to_dict() for i in self._resolved]

    # -- internals -------------------------------------------------------------

    @staticmethod
    def _track(inc: Incident, f: Failure, now: float) -> None:
        inc.last_seen_at = now
        inc.observations += 1
        inc.healthy_since = None
        inc.failing_pods[f.pod] = f.restarts
        if len(inc.failing_pods) > inc.peak_failing and inc.scope_pending_since is None:
            inc.scope_pending_since = now
        inc.all_pods[f.pod] = max(f.restarts, inc.all_pods.get(f.pod, 0))

    @staticmethod
    def _mark_reported(inc: Incident, now: float) -> None:
        inc.peak_failing = max(inc.peak_failing, len(inc.failing_pods))
        inc.scope_pending_since = None
        inc.last_notified_at = now


# -- console rendering --------------------------------------------------------

def _duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def _pods(d: dict[str, int]) -> str:
    return ", ".join(f"{p} (restarts {r})" for p, r in sorted(d.items())) or "none"


def format_update(u: IncidentUpdate, now: float) -> str:
    i = u.incident
    title = {
        "opened": "INCIDENT OPENED",
        "cause_changed": "INCIDENT CAUSE CHANGED",
        "scope_changed": "INCIDENT SCOPE CHANGED",
        "reminder": "INCIDENT ONGOING",
        "resolved": "INCIDENT RESOLVED",
    }[u.kind]
    lines = [
        f"[{title}] {i['id']}",
        f"Namespace : {i['namespace']}",
        f"Workload  : {i['workload']}",
        f"Container : {i['container']}",
    ]
    if u.kind == "cause_changed":
        lines.append(f"Cause     : {u.previous_cause} -> {i['cause']}")
    else:
        lines.append(f"Cause     : {i['cause']}")

    if u.kind == "scope_changed":
        lines.append(f"Scope     : {u.previous_pod_count} -> {len(i['failing_pods'])} pods failing")

    if u.kind == "resolved":
        lines.append(f"Duration  : {_duration(i['resolved_at'] - i['opened_at'])}")
        lines.append(f"Pods      : {_pods(i['affected_pods'])}")
        if len(i["cause_history"]) > 1:
            lines.append("History   : " + " -> ".join(i["cause_history"]))
    else:
        lines.append(f"Failing   : {_duration(now - i['opened_at'])}")
        lines.append(f"Pods      : {_pods(i['failing_pods'])}")
        lines.append(f"Seen      : {i['observations']} observations")
    return "\n".join(lines)
