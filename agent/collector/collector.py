"""Diagnostic collector — gathers evidence for one detected failure.

Every section is collected independently: if one call fails (RBAC, 404,
container never started), that section records an error and the rest of
the bundle is still returned. All calls are namespaced and read-only.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

UTC = timezone.utc  # datetime.UTC needs Python 3.11
from typing import Any

from agent.collector.dependencies import check_dependencies
from agent.watcher.detector import Failure
from kubelantern_common.redaction import redact

log = logging.getLogger("kubelantern.collector")

LOG_TAIL_LINES = 100
LOG_LIMIT_BYTES = 16 * 1024
MAX_EVENTS = 20

# Reasons where the container never started, so there are no logs to fetch.
NO_LOG_REASONS = {
    "ImagePullBackOff",
    "ErrImagePull",
    "InvalidImageName",
    "CreateContainerConfigError",
    "CreateContainerError",
    "Evicted",
}

def _ts(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.isoformat()
    return str(value)


def _quantities(d) -> dict[str, str]:
    return {k: str(v) for k, v in (d or {}).items()}


def _labels_match(selector: dict | None, labels: dict | None) -> bool:
    if not selector:
        return False
    labels = labels or {}
    return all(labels.get(k) == v for k, v in selector.items())


class DiagnosticCollector:
    def __init__(self, namespace: str, core_api=None, apps_api=None, discovery_api=None) -> None:
        self.namespace = namespace
        if core_api is None or apps_api is None:
            from kubernetes import client

            core_api = core_api or client.CoreV1Api()
            apps_api = apps_api or client.AppsV1Api()
        self.core = core_api
        self.apps = apps_api
        self._discovery = discovery_api

    @property
    def discovery(self):
        if self._discovery is None:  # created only when a dependency check needs it
            from kubernetes import client

            self._discovery = client.DiscoveryV1Api()
        return self._discovery

    # -- public ------------------------------------------------------------

    def collect(self, failure: Failure) -> dict[str, Any]:
        if failure.namespace != self.namespace:
            # Defence in depth: RBAC already forbids this.
            raise ValueError(
                f"collector for {self.namespace} refused failure in {failure.namespace}"
            )

        bundle: dict[str, Any] = {
            "collected_at": datetime.now(UTC).isoformat(),
            "failure": {
                "namespace": failure.namespace,
                "pod": failure.pod,
                "container": failure.container,
                "reason": failure.reason,
                "restarts": failure.restarts,
                "exit_code": failure.exit_code,
                "last_termination_reason": failure.last_termination_reason,
                "message": redact(failure.message),
            },
            "errors": {},
        }

        all_services = self._section(
            bundle, "services", lambda: self.core.list_namespaced_service(self.namespace).items)

        pod = self._section(bundle, "pod", lambda: self._read_pod(failure.pod))
        if pod is not None:
            bundle["pod"] = self._pod_summary(pod)
            bundle["container"] = self._container_detail(pod, failure.container)
            bundle["resources"] = self._resources(pod)
            bundle["owners"] = self._section(bundle, "owners", lambda: self._owner_chain(pod))
            if all_services is not None:
                bundle["services"] = self._services(pod, all_services)

        bundle["events"] = self._section(bundle, "events", lambda: self._events(failure.pod))
        bundle["logs"] = self._logs(bundle, failure, pod)

        # Stage 6: targeted checks for endpoints the application mentions.
        if all_services is not None:
            deps = self._section(bundle, "dependencies",
                                 lambda: self._dependencies(bundle, all_services))
            if deps:
                bundle["dependencies"] = deps

        if not bundle["errors"]:
            del bundle["errors"]
        return bundle

    # -- sections ----------------------------------------------------------

    def _section(self, bundle, name, fn):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 — one section must not kill the bundle
            status = getattr(e, "status", None)
            reason = getattr(e, "reason", None) or type(e).__name__
            bundle["errors"][name] = f"{status} {reason}".strip() if status else str(reason)
            log.warning("collect %s failed: %s", name, bundle["errors"][name])
            return None

    def _read_pod(self, name):
        return self.core.read_namespaced_pod(name, self.namespace)

    def _pod_summary(self, pod) -> dict:
        s = pod.status
        return {
            "phase": s.phase,
            "node": pod.spec.node_name,
            "qos_class": getattr(s, "qos_class", None),
            "start_time": _ts(getattr(s, "start_time", None)),
            "labels": dict(pod.metadata.labels or {}),
            "image_pull_secrets": [s.name for s in (getattr(pod.spec, "image_pull_secrets", None) or [])],
            "conditions": [
                {"type": c.type, "status": c.status, "reason": c.reason, "message": c.message}
                for c in (s.conditions or [])
            ],
        }

    def _container_detail(self, pod, container_name) -> dict | None:
        spec = next((c for c in (pod.spec.containers or []) + (pod.spec.init_containers or [])
                     if c.name == container_name), None)
        statuses = (pod.status.container_statuses or []) + (pod.status.init_container_statuses or [])
        st = next((c for c in statuses if c.name == container_name), None)
        if spec is None and st is None:
            return None

        detail: dict[str, Any] = {"name": container_name}
        if spec is not None:
            detail["image"] = spec.image
            detail["command"] = spec.command
            detail["args"] = spec.args
        if st is not None:
            detail["ready"] = st.ready
            detail["restart_count"] = st.restart_count
            last = st.last_state.terminated if st.last_state else None
            if last is not None:
                detail["last_termination"] = {
                    "reason": last.reason,
                    "exit_code": last.exit_code,
                    "started_at": _ts(getattr(last, "started_at", None)),
                    "finished_at": _ts(getattr(last, "finished_at", None)),
                }
        return detail

    def _resources(self, pod) -> dict:
        out = {}
        for c in (pod.spec.containers or []) + (pod.spec.init_containers or []):
            r = c.resources
            out[c.name] = {
                "requests": _quantities(r.requests if r else None),
                "limits": _quantities(r.limits if r else None),
            }
        return out

    def _owner_chain(self, pod) -> list[dict]:
        chain = []
        for ref in pod.metadata.owner_references or []:
            if ref.kind == "ReplicaSet":
                rs = self.apps.read_namespaced_replica_set(ref.name, self.namespace)
                chain.append({
                    "kind": "ReplicaSet",
                    "name": rs.metadata.name,
                    "replicas": rs.spec.replicas,
                    "ready_replicas": rs.status.ready_replicas or 0,
                })
                for rs_ref in rs.metadata.owner_references or []:
                    if rs_ref.kind == "Deployment":
                        chain.append(self._deployment(rs_ref.name))
            else:
                chain.append({"kind": ref.kind, "name": ref.name})
        return chain

    def _deployment(self, name) -> dict:
        d = self.apps.read_namespaced_deployment(name, self.namespace)
        return {
            "kind": "Deployment",
            "name": d.metadata.name,
            "generation": d.metadata.generation,
            "replicas": d.spec.replicas,
            "available_replicas": d.status.available_replicas or 0,
            "unavailable_replicas": d.status.unavailable_replicas or 0,
            "strategy": d.spec.strategy.type if d.spec.strategy else None,
            "images": [c.image for c in d.spec.template.spec.containers],
            "conditions": [
                {"type": c.type, "status": c.status, "reason": c.reason, "message": c.message}
                for c in (d.status.conditions or [])
            ],
        }

    def _dependencies(self, bundle: dict, services: list) -> list[dict]:
        logs = bundle.get("logs") or {}
        text = "\n".join(filter(None, [
            logs.get("previous"), logs.get("current"),
            (bundle.get("failure") or {}).get("message"),
            *[(e.get("message") or "") for e in (bundle.get("events") or [])],
        ]))

        def slices_for(name: str):
            return self.discovery.list_namespaced_endpoint_slice(
                self.namespace, label_selector=f"kubernetes.io/service-name={name}").items

        return check_dependencies(text, self.namespace, services, slices_for)

    def _services(self, pod, services: list) -> list[dict]:
        labels = pod.metadata.labels or {}
        out = []
        for svc in services:
            if _labels_match(svc.spec.selector, labels):
                out.append({
                    "name": svc.metadata.name,
                    "type": svc.spec.type,
                    "ports": [
                        {"port": p.port, "target_port": str(p.target_port), "protocol": p.protocol}
                        for p in (svc.spec.ports or [])
                    ],
                })
        return out

    def _events(self, pod_name) -> list[dict]:
        evs = self.core.list_namespaced_event(
            self.namespace, field_selector=f"involvedObject.name={pod_name}"
        ).items

        def when(e):
            t = e.last_timestamp or getattr(e, "event_time", None) or e.first_timestamp
            return _ts(t) or ""

        evs = sorted(evs, key=when)[-MAX_EVENTS:]
        return [
            {
                "time": when(e),
                "type": e.type,
                "reason": e.reason,
                "count": e.count or 1,
                "message": redact(e.message),
            }
            for e in evs
        ]

    def _logs(self, bundle, failure: Failure, pod) -> dict:
        if failure.reason in NO_LOG_REASONS or failure.container == "-":
            return {"skipped": f"container never started ({failure.reason})"}

        out: dict[str, Any] = {}

        def fetch(previous: bool) -> str | None:
            resp = self.core.read_namespaced_pod_log(
                failure.pod,
                self.namespace,
                container=failure.container,
                previous=previous,
                tail_lines=LOG_TAIL_LINES,
                limit_bytes=LOG_LIMIT_BYTES,
                _preload_content=False,
            )
            return _clean_log(_decode_log(resp))

        current = self._section(bundle, "logs.current", lambda: fetch(False))
        out["current"] = redact(current)

        # A previous container only exists after at least one restart. The watch
        # event can be stale: by the time we read logs the kubelet may already have
        # started the next attempt, so use the live restart count, and if the
        # current container has nothing yet, the error is in the previous one.
        restarts = max(failure.restarts, _live_restarts(pod, failure.container))
        if restarts > 0 or not (current or "").strip():
            previous = None
            try:
                previous = fetch(True)
            except Exception as e:  # noqa: BLE001
                status = getattr(e, "status", None)
                # 400 = "previous terminated container not found": benign race.
                if status != 400:
                    reason = getattr(e, "reason", None) or type(e).__name__
                    bundle["errors"]["logs.previous"] = f"{status} {reason}" if status else reason
                    log.warning("collect logs.previous failed: %s", bundle["errors"]["logs.previous"])
            out["previous"] = redact(previous)
        return out


def _live_restarts(pod, container: str) -> int:
    """Restart count of `container` in the freshly read pod (0 if unknown)."""
    status = getattr(pod, "status", None)
    if status is None:
        return 0
    for st in (status.container_statuses or []) + (status.init_container_statuses or []):
        if st.name == container:
            return st.restart_count or 0
    return 0


# -- log decoding ------------------------------------------------------------

_UNAVAILABLE_LOG_PREFIXES = (
    "unable to retrieve container logs",
    "failed to try resolving symlinks",
)


def _decode_log(resp) -> str | None:
    """Normalise whatever the client returns into text.

    With _preload_content=False we get a urllib3 response (.data is bytes).
    Some client versions instead return str(bytes) like "b'...'"; handle all.
    """
    if resp is None:
        return None
    data = getattr(resp, "data", resp)
    if isinstance(data, (bytes, bytearray)):
        return data.decode("utf-8", errors="replace")
    text = str(data)
    if len(text) >= 3 and text[:2] in ("b'", 'b"') and text[-1] == text[1]:
        import ast

        try:
            return ast.literal_eval(text).decode("utf-8", errors="replace")
        except (ValueError, SyntaxError):
            pass
    return text


def _clean_log(text: str | None) -> str | None:
    if text is None:
        return None
    stripped = text.strip()
    if not stripped:
        return None
    if stripped.startswith(_UNAVAILABLE_LOG_PREFIXES):
        return None  # container already garbage-collected; not real log content
    return text


# -- console rendering ----------------------------------------------------

def describe_dependency(d: dict) -> str:
    target = f"{d['host']}:{d['port']}" if d.get("port") else d["host"]
    scope = d.get("scope")
    if scope == "namespace":
        if not d.get("service_exists"):
            return f"{target} — Service '{d.get('service')}' NOT FOUND in namespace"
        parts = [f"Service '{d['service']}' exists"]
        if d.get("port_exposed") is False:
            parts.append(f"port {d['port']} NOT exposed (ports {d.get('service_ports')})")
        if "ready_endpoints" in d:
            parts.append(f"{d['ready_endpoints']} ready endpoints")
        return f"{target} — " + ", ".join(parts)
    return {
        "other-namespace": f"{target} — in another namespace (not checked)",
        "external": f"{target} — external host (not checked)",
        "localhost": f"{target} — localhost (same pod)",
        "ip": f"{target} — IP address (not checked)",
    }.get(scope, target)


def _tail(text: str | None, n: int) -> list[str]:
    if not text:
        return []
    return text.rstrip("\n").splitlines()[-n:]


def format_evidence(b: dict, log_lines: int = 5, header: bool = True) -> str:
    """Render a bundle. header=False omits identity lines (used under an incident)."""
    f = b["failure"]
    if header:
        lines = [
            "[DETECTED]",
            f"Namespace : {f['namespace']}",
            f"Pod       : {f['pod']}",
            f"Container : {f['container']}",
            f"Reason    : {f['reason']}",
            f"Restarts  : {f['restarts']}",
        ]
    else:
        lines = [f"Evidence  : pod {f['pod']} — state {f['reason']}, restarts {f['restarts']}"]
    if f.get("exit_code") is not None:
        lines.append(f"Exit code : {f['exit_code']}")
    if f.get("last_termination_reason") and f["last_termination_reason"] != f["reason"]:
        lines.append(f"Last term : {f['last_termination_reason']}")

    c = b.get("container") or {}
    if c.get("image"):
        lines.append(f"Image     : {c['image']}")

    res = (b.get("resources") or {}).get(f["container"])
    if res:
        lim = ", ".join(f"{k}={v}" for k, v in res["limits"].items()) or "none"
        req = ", ".join(f"{k}={v}" for k, v in res["requests"].items()) or "none"
        lines.append(f"Resources : requests[{req}] limits[{lim}]")

    owners = b.get("owners") or []
    if owners:
        lines.append("Owner     : " + " <- ".join(f"{o['kind']}/{o['name']}" for o in reversed(owners)))
        dep = next((o for o in owners if o["kind"] == "Deployment"), None)
        if dep:
            lines.append(
                f"Replicas  : {dep['available_replicas']}/{dep['replicas']} available"
            )

    svcs = b.get("services")
    if svcs is not None:
        lines.append("Services  : " + (", ".join(s["name"] for s in svcs) or "none"))

    for d in b.get("dependencies") or []:
        lines.append("Depends on: " + describe_dependency(d))

    events = b.get("events") or []
    if events:
        lines.append("Events    :")
        for e in events[-5:]:
            lines.append(f"  {e['type']:<7} {e['reason']:<18} x{e['count']:<3} {(e['message'] or '')[:110]}")

    logs = b.get("logs") or {}
    if "skipped" in logs:
        lines.append(f"Logs      : skipped — {logs['skipped']}")
    else:
        for key, title in (("previous", "Prev logs"), ("current", "Logs")):
            tail = _tail(logs.get(key), log_lines)
            if tail:
                lines.append(f"{title:<10}:")
                lines.extend(f"  | {line[:160]}" for line in tail)

    if b.get("errors"):
        lines.append("Collect errors: " + ", ".join(f"{k}={v}" for k, v in b["errors"].items()))
    return "\n".join(lines)
