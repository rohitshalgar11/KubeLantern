"""Dependency checks — collected up front, inside the namespace.

When an application logs that it cannot reach `db:5432`, the useful question
is not "what does the model think?" but "does a Service called db exist here,
does it expose 5432, and does it have ready endpoints?". The agent can answer
that with its namespace-scoped, read-only access; the gateway never needs
cluster access.

Only hosts that resolve to *this* namespace are checked. Hosts in other
namespaces are reported as out of scope (RBAC forbids looking), and external
hosts are reported as external.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass

MAX_HOSTS = 5

# scheme://[user[:pass]@]host[:port]
_URL = re.compile(
    r"\b(?:tcp|udp|http|https|grpc|postgres|postgresql|mysql|mariadb|redis|rediss|mongodb|"
    r"amqp|amqps|nats|kafka|jdbc:[a-z]+)://(?:[^@/\s]+@)?([A-Za-z0-9][A-Za-z0-9.-]*)(?::(\d{2,5}))?",
    re.IGNORECASE,
)
# host:port (port 2-5 digits); avoids timestamps like 12:30:45 by requiring a letter in host
_HOST_PORT = re.compile(r"(?<![\w./-])([A-Za-z][A-Za-z0-9-]*(?:\.[A-Za-z0-9-]+)*):(\d{2,5})\b")
# Go / glibc resolver errors
_LOOKUP = re.compile(r"\blookup ([A-Za-z0-9][A-Za-z0-9.-]*)(?: on \S+)?:\s*no such host", re.IGNORECASE)
_NAME_RESOLUTION = re.compile(
    r"(?:could not translate host name|getaddrinfo \w+ for|unknown host)\s+\"?([A-Za-z0-9][A-Za-z0-9.-]*)",
    re.IGNORECASE,
)

_IGNORE_HOSTS = {"http", "https", "tcp", "udp", "time", "line", "port", "error", "info", "warn",
                 "debug", "fatal", "trace", "level", "ts", "at", "caused"}


@dataclass(frozen=True)
class HostRef:
    host: str
    port: int | None


def extract_hosts(text: str | None) -> list[HostRef]:
    """Find network endpoints an application mentions in its logs."""
    if not text:
        return []
    found: dict[tuple[str, int | None], None] = {}

    def add(host: str, port: str | None) -> None:
        host = host.strip(".").lower()
        if not host or host in _IGNORE_HOSTS or len(found) >= MAX_HOSTS * 2:
            return
        p = int(port) if port and port.isdigit() else None
        if p is not None and not (1 <= p <= 65535):
            return
        found.setdefault((host, p), None)

    for m in _URL.finditer(text):
        add(m.group(1), m.group(2))
    for m in _LOOKUP.finditer(text):
        add(m.group(1), None)
    for m in _NAME_RESOLUTION.finditer(text):
        add(m.group(1), None)
    for m in _HOST_PORT.finditer(text):
        add(m.group(1), m.group(2))

    # Prefer entries with a port; drop a bare host if the same host has a port.
    with_port = {h for h, p in found if p is not None}
    refs = [HostRef(h, p) for h, p in found if p is not None or h not in with_port]
    return refs[:MAX_HOSTS]


def scope_of(host: str, namespace: str) -> tuple[str, str | None]:
    """Return (scope, service_name).

    scope: namespace | other-namespace | external | localhost | ip
    """
    if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return "localhost", None
    try:
        ipaddress.ip_address(host)
        return "ip", None
    except ValueError:
        pass
    parts = host.split(".")
    if len(parts) == 1:
        return "namespace", parts[0]
    # name.ns | name.ns.svc | name.ns.svc.cluster.local
    if len(parts) == 2 or (len(parts) >= 3 and parts[2] == "svc"):
        if parts[1] == namespace:
            return "namespace", parts[0]
        return "other-namespace", parts[0]
    return "external", None


def check_dependencies(text: str, namespace: str, services: list, slices_for) -> list[dict]:
    """Check each referenced host against the namespace's Services.

    services:   V1Service-like objects in this namespace (already listed)
    slices_for: callable(service_name) -> list of EndpointSlice-like objects
    """
    by_name = {s.metadata.name: s for s in services}
    results = []
    for ref in extract_hosts(text):
        scope, name = scope_of(ref.host, namespace)
        r: dict = {"host": ref.host, "port": ref.port, "scope": scope}
        if scope == "namespace":
            svc = by_name.get(name)
            r["service"] = name
            r["service_exists"] = svc is not None
            if svc is not None:
                ports = [p.port for p in (svc.spec.ports or [])]
                r["service_ports"] = ports
                if ref.port is not None:
                    r["port_exposed"] = ref.port in ports
                r["selector"] = dict(svc.spec.selector or {})
                try:
                    r["ready_endpoints"] = _ready_endpoints(slices_for(name))
                except Exception as e:  # noqa: BLE001
                    r["endpoints_error"] = getattr(e, "reason", None) or type(e).__name__
        results.append(r)
    return results


def _ready_endpoints(slices) -> int:
    n = 0
    for sl in slices or []:
        for ep in getattr(sl, "endpoints", None) or []:
            cond = getattr(ep, "conditions", None)
            ready = getattr(cond, "ready", None) if cond is not None else None
            if ready is None or ready:
                n += len(getattr(ep, "addresses", None) or [])
    return n
