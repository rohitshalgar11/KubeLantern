"""Failure detection — pure functions over Kubernetes pod objects.

No API calls happen here, so everything is unit-testable with plain
objects or the kubernetes client's V1Pod models.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Waiting reasons that mean the container is broken, not just starting.
FAILURE_WAITING_REASONS = {
    "CrashLoopBackOff",
    "ImagePullBackOff",
    "ErrImagePull",
    "InvalidImageName",
    "CreateContainerConfigError",
    "CreateContainerError",
    "RunContainerError",
}

# Terminated reasons that mean failure even before a BackOff shows up.
FAILURE_TERMINATED_REASONS = {
    "OOMKilled",
    "Error",
    "ContainerCannotRun",
    "DeadlineExceeded",
}


@dataclass(frozen=True)
class Failure:
    namespace: str
    pod: str
    container: str
    reason: str
    restarts: int
    exit_code: int | None = None
    message: str | None = None
    last_termination_reason: str | None = None
    init_container: bool = False
    workload_kind: str = "Pod"
    workload_name: str = ""
    extra: dict = field(default_factory=dict, compare=False, hash=False)

    @property
    def key(self) -> tuple[str, str, str, str]:
        """Per-observation identity used by the watcher's restart-count filter."""
        return (self.namespace, self.pod, self.container, self.reason)


def workload_of(pod) -> tuple[str, str]:
    """Resolve the controlling workload without extra API calls.

    ReplicaSet owners created by a Deployment are named
    <deployment>-<pod-template-hash>, so the Deployment name is derived
    from the pod-template-hash label.
    """
    meta = pod.metadata
    refs = getattr(meta, "owner_references", None) or []
    ref = next((r for r in refs if getattr(r, "controller", None)), refs[0] if refs else None)
    if ref is None:
        return "Pod", meta.name
    if ref.kind == "ReplicaSet":
        h = (getattr(meta, "labels", None) or {}).get("pod-template-hash")
        if h and ref.name.endswith("-" + h):
            return "Deployment", ref.name[: -len(h) - 1]
    return ref.kind, ref.name


def _status_failures(pod, statuses, *, init: bool) -> list[Failure]:
    found: list[Failure] = []
    ns = pod.metadata.namespace
    name = pod.metadata.name
    wk, wn = workload_of(pod)

    for cs in statuses or []:
        state = cs.state
        last = cs.last_state
        last_term = last.terminated if last else None
        restarts = cs.restart_count or 0

        if state and state.waiting and state.waiting.reason in FAILURE_WAITING_REASONS:
            found.append(
                Failure(
                    namespace=ns,
                    pod=name,
                    container=cs.name,
                    reason=state.waiting.reason,
                    restarts=restarts,
                    exit_code=last_term.exit_code if last_term else None,
                    message=state.waiting.message,
                    last_termination_reason=last_term.reason if last_term else None,
                    init_container=init,
                    workload_kind=wk,
                    workload_name=wn,
                )
            )
            continue

        term = state.terminated if state else None
        if term and term.reason in FAILURE_TERMINATED_REASONS and (term.exit_code or 0) != 0:
            found.append(
                Failure(
                    namespace=ns,
                    pod=name,
                    container=cs.name,
                    reason=term.reason,
                    restarts=restarts,
                    exit_code=term.exit_code,
                    message=term.message,
                    last_termination_reason=term.reason,
                    init_container=init,
                    workload_kind=wk,
                    workload_name=wn,
                )
            )

    return found


SELF_LABEL = ("app.kubernetes.io/part-of", "kubelantern")


def ignore_reason(pod) -> str | None:
    """Pods whose container exits are not failures to report."""
    meta = pod.metadata
    if getattr(meta, "deletion_timestamp", None) is not None:
        # Being deleted on purpose (rollout, scale-down, kubectl delete):
        # containers exit 137/143/2 when stopped and must not look like crashes.
        return "terminating"
    if (getattr(meta, "labels", None) or {}).get(SELF_LABEL[0]) == SELF_LABEL[1]:
        return "kubelantern"
    return None


def detect_failures(pod) -> list[Failure]:
    """Return every failing container in the pod (empty list if healthy)."""
    status = pod.status
    if status is None or ignore_reason(pod):
        return []

    failures = _status_failures(pod, status.init_container_statuses, init=True)
    failures += _status_failures(pod, status.container_statuses, init=False)

    # Pod-level eviction (no container status explains it).
    if not failures and status.phase == "Failed" and status.reason == "Evicted":
        wk, wn = workload_of(pod)
        failures.append(
            Failure(
                namespace=pod.metadata.namespace,
                pod=pod.metadata.name,
                container="-",
                reason="Evicted",
                restarts=0,
                message=status.message,
                workload_kind=wk,
                workload_name=wn,
            )
        )

    return failures


def format_failure(f: Failure) -> str:
    lines = [
        "[DETECTED]",
        f"Namespace : {f.namespace}",
        f"Pod       : {f.pod}",
        f"Container : {f.container}{' (init)' if f.init_container else ''}",
        f"Reason    : {f.reason}",
        f"Restarts  : {f.restarts}",
    ]
    if f.exit_code is not None:
        lines.append(f"Exit code : {f.exit_code}")
    if f.last_termination_reason and f.last_termination_reason != f.reason:
        lines.append(f"Last term : {f.last_termination_reason}")
    if f.message:
        lines.append(f"Message   : {f.message[:300]}")
    return "\n".join(lines)
