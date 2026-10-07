"""Agent maintenance mode: quiet during planned work, then a clean restart.

States
  active    normal operation
  paused    a switch is on: no new incidents, no diagnoses, no notifications.
            Incidents that were already open keep being tracked, silently.
  settling  the pause just ended: stay quiet for `settle_seconds` so pods can come
            back, then re-check every pod and open incidents only for what is
            still broken.

The pause comes from the namespace's own switch (agent chart values) or the
cluster-wide switch (gateway, polled). If the gateway is unreachable the last
known state is kept; a pause still ends at its `until`, or after `max_hours`.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from kubelantern_common.maintenance import Pause, format_time

log = logging.getLogger("kubelantern.maintenance")

ACTIVE, PAUSED, SETTLING = "active", "paused", "settling"


class MaintenanceController:
    def __init__(self, local: Pause | None = None, settle_seconds: float = 300,
                 max_hours: float = 12, fetch: Callable[[], dict] | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.local = local or Pause()
        self.remote = Pause()
        self.settle_seconds = settle_seconds
        self.max_seconds = max_hours * 3600
        self.fetch = fetch
        self.clock = clock
        self.state = ACTIVE
        self.reason = ""
        self.until: float | None = None
        self._started: float | None = None
        self._settle_until: float | None = None
        self._capped = False

    @property
    def quiet(self) -> bool:
        return self.state != ACTIVE

    def poll(self) -> None:
        """Ask the gateway for the cluster-wide switch; keep the last answer on failure."""
        if self.fetch is None:
            return
        try:
            self.remote = Pause.from_dict(self.fetch())
        except Exception as e:  # noqa: BLE001
            log.debug("maintenance poll failed (keeping last state): %s", e)

    def _current(self, now: float) -> Pause | None:
        for source in (self.local, self.remote):
            if source.active(now):
                return source
        return None

    def tick(self) -> tuple[str, str] | None:
        """Advance the state; returns (event, message) on a transition."""
        now = self.clock()
        pause = self._current(now)
        if pause is None:
            self._capped, self._started = False, None     # switch off: a new pause starts fresh
        elif pause.until is None and self._started is not None \
                and now - self._started >= self.max_seconds:
            self._capped = True
        if pause is not None and self._capped:
            pause = None                                   # open-ended pause ran too long

        if pause is not None:
            if self.state != PAUSED:
                self.state, self._started = PAUSED, self._started or now
                self.reason, self.until = pause.reason, pause.until
                end = format_time(pause.until) or f"at most {self.max_seconds / 3600:g}h"
                return "paused", (f"[MAINTENANCE] paused until {end}"
                                  + (f" — {pause.reason}" if pause.reason else "")
                                  + ": no new incidents, diagnoses or notifications")
            return None

        if self.state == PAUSED:
            self.state, self._settle_until = SETTLING, now + self.settle_seconds
            return "settling", (f"[MAINTENANCE] over — waiting {self.settle_seconds:g}s for pods "
                                "to settle, then checking all workloads")
        if self.state == SETTLING and now >= (self._settle_until or now):
            self.state = ACTIVE
            return "resumed", "[MAINTENANCE] resumed: checking every pod for real failures"
        return None
