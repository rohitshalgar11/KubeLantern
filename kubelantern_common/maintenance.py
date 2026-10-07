"""Maintenance mode — pause incidents and alerts during planned work (e.g. a
cluster upgrade).

Two switches, either one pauses an agent:
  * cluster-wide: the `kubelantern-ai` chart's `maintenance` values, a ConfigMap
    mounted into the gateway; agents poll GET /v1/maintenance.
  * per namespace: the agent chart's `maintenance` values (env vars).

A pause always ends: at `until`, or after `max_hours` if no end was given — so a
forgotten switch, or a gateway that is down during the upgrade, can't silence
alerts forever.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


def parse_time(value: str | None) -> float | None:
    """RFC 3339 / ISO 8601 ('2026-10-07T12:00:00Z') -> epoch seconds, else None."""
    if not value or not value.strip():
        return None
    v = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def format_time(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Pause:
    paused: bool = False
    until: float | None = None
    reason: str = ""

    def active(self, now: float) -> bool:
        return self.paused and (self.until is None or now < self.until)

    def to_dict(self) -> dict:
        return {"paused": self.paused, "until": format_time(self.until), "reason": self.reason}

    @classmethod
    def from_dict(cls, d: dict | None) -> Pause:
        d = d or {}
        return cls(bool(d.get("paused")), parse_time(d.get("until")), str(d.get("reason") or ""))


def _truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in ("true", "1", "yes", "on")


def read_dir(path: str | Path) -> Pause:
    """Read a mounted ConfigMap: files `paused`, `until`, `reason` (all optional)."""
    p = Path(path)

    def get(name: str) -> str:
        try:
            return (p / name).read_text().strip()
        except OSError:
            return ""

    return Pause(_truthy(get("paused")), parse_time(get("until")), get("reason")[:200])


def from_env(paused: str | None, until: str | None, reason: str | None) -> Pause:
    return Pause(_truthy(paused), parse_time(until), (reason or "")[:200])
