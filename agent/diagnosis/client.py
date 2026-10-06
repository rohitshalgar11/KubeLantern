"""Agent -> Gateway diagnosis client.

Runs in a background worker so the pod watch loop is never blocked by the
model (CPU inference can take tens of seconds). Rate-limited or busy
responses are retried with backoff instead of being dropped.
"""

from __future__ import annotations

import heapq
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from kubelantern_common.redaction import redact_obj

log = logging.getLogger("kubelantern.diagnosis")

DEFAULT_TOKEN_PATH = "/var/run/secrets/kubelantern/token"


RETRYABLE_STATUS = {429, 502, 503, 504}


class DiagnosisError(Exception):
    def __init__(self, message: str, retryable: bool = False, retry_after: float | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


class GatewayClient:
    def __init__(self, url: str, token_path: str = DEFAULT_TOKEN_PATH, timeout: float = 240) -> None:
        self.url = url.rstrip("/") + "/v1/diagnose"
        self.token_path = Path(token_path)
        self.timeout = timeout

    def _token(self) -> str:
        # Re-read every call: the kubelet rotates projected tokens.
        return self.token_path.read_text().strip()

    def diagnose(self, incident: dict, evidence: dict | None) -> dict:
        body = json.dumps({"incident": incident, "evidence": redact_obj(evidence or {})},
                          default=str).encode()
        req = urllib.request.Request(
            self.url, data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self._token()}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            detail, retry_after = "", None
            try:
                payload = json.loads(e.read())
                detail = payload.get("error", "")
                retry_after = payload.get("retry_after_seconds")
            except (ValueError, OSError):
                log.debug("gateway error body was not JSON")
            header = e.headers.get("Retry-After") if e.headers else None
            if header and header.isdigit():
                retry_after = float(header)
            raise DiagnosisError(f"gateway returned {e.code}: {detail}".rstrip(": "),
                                 retryable=e.code in RETRYABLE_STATUS,
                                 retry_after=retry_after) from e
        except (urllib.error.URLError, OSError) as e:
            raise DiagnosisError(f"gateway unreachable: {e}", retryable=True) from e


@dataclass(order=True)
class _Job:
    ready_at: float
    seq: int
    incident: dict = field(compare=False)
    evidence: dict | None = field(compare=False)
    attempt: int = field(default=1, compare=False)


class DiagnosisWorker:
    """Background diagnosis with retry.

    * 429 / 503 / gateway unreachable -> retried with exponential backoff
      (base 15s, doubling, max 5 min), honouring the gateway's Retry-After.
    * Gives up after `max_attempts`; only then is an error printed.
    * A newer request for the same incident (e.g. CAUSE CHANGED) replaces
      a pending retry of the older one.
    * Bounded: when full, the oldest pending job is dropped.
    """

    def __init__(self, client, on_result: Callable[[dict, dict], None],
                 on_error: Callable[[dict, str], None], max_queue: int = 20,
                 max_attempts: int = 5, base_backoff: float = 15, max_backoff: float = 300,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.client = client
        self.on_result = on_result
        self.on_error = on_error
        self.max_queue = max_queue
        self.max_attempts = max_attempts
        self.base_backoff = base_backoff
        self.max_backoff = max_backoff
        self.clock = clock
        self._heap: list[_Job] = []
        self._seq = 0
        self._cv = threading.Condition()
        self._thread = threading.Thread(target=self._run, name="diagnosis", daemon=True)

    def start(self) -> DiagnosisWorker:
        self._thread.start()
        return self

    # -- queue management -----------------------------------------------------

    def submit(self, incident: dict, evidence: dict | None) -> None:
        self._push(incident, evidence, attempt=1, ready_at=self.clock())

    def _push(self, incident, evidence, attempt: int, ready_at: float) -> None:
        with self._cv:
            # newer request for the same incident supersedes a pending one
            self._heap = [j for j in self._heap if j.incident.get("id") != incident.get("id")]
            heapq.heapify(self._heap)
            if len(self._heap) >= self.max_queue:
                oldest = min(self._heap, key=lambda j: j.seq)
                self._heap.remove(oldest)
                heapq.heapify(self._heap)
                log.warning("diagnosis queue full; dropped %s", oldest.incident.get("id"))
            self._seq += 1
            heapq.heappush(self._heap, _Job(ready_at, self._seq, incident, evidence, attempt))
            self._cv.notify()

    def pending(self) -> list[tuple[str, int, float]]:
        with self._cv:
            return sorted((j.incident.get("id"), j.attempt, j.ready_at) for j in self._heap)

    def _pop_ready(self, now: float) -> _Job | None:
        with self._cv:
            if self._heap and self._heap[0].ready_at <= now:
                return heapq.heappop(self._heap)
            return None

    # -- processing ---------------------------------------------------------------

    def drain(self) -> None:
        """Process every job that is ready now, synchronously (used by tests)."""
        while (job := self._pop_ready(self.clock())) is not None:
            self._process(job)

    def _run(self) -> None:
        while True:
            with self._cv:
                while not self._heap:
                    self._cv.wait()
                wait = self._heap[0].ready_at - self.clock()
                if wait > 0:
                    self._cv.wait(timeout=wait)
                    continue
            job = self._pop_ready(self.clock())
            if job is not None:
                self._process(job)

    def _backoff(self, attempt: int, retry_after: float | None) -> float:
        delay = min(self.max_backoff, self.base_backoff * (2 ** (attempt - 1)))
        return max(delay, retry_after or 0)

    def _process(self, job: _Job) -> None:
        try:
            result = self.client.diagnose(job.incident, job.evidence)
        except DiagnosisError as e:
            if e.retryable and job.attempt < self.max_attempts:
                delay = self._backoff(job.attempt, e.retry_after)
                log.info("diagnosis for %s deferred (%s); retry %d/%d in %.0fs",
                         job.incident.get("id"), e, job.attempt + 1, self.max_attempts, delay)
                self._push(job.incident, job.evidence, job.attempt + 1, self.clock() + delay)
                return
            suffix = f" (after {job.attempt} attempts)" if job.attempt > 1 else ""
            self.on_error(job.incident, f"{e}{suffix}")
            return
        except Exception as e:
            log.exception("diagnosis failed")
            self.on_error(job.incident, f"{type(e).__name__}: {e}")
            return
        self.on_result(job.incident, result)


def format_diagnosis(incident: dict, result: dict) -> str:
    d = result.get("diagnosis") or {}
    attempts = result.get("attempts") or 1
    timing = f"{result.get('latency_seconds')}s" + (f", {attempts} attempts" if attempts > 1 else "")
    lines = [
        f"[INCIDENT DIAGNOSIS] {incident['id']}",
        f"Model     : {result.get('model')} ({timing})",
    ]
    if not d.get("structured", True):
        lines.append("Summary   : " + (d.get("summary") or "").strip()[:1500])
        return "\n".join(lines)
    lines += [
        f"Summary   : {d.get('summary')}",
        f"Category  : {d.get('category')}   Confidence: {d.get('confidence')}",
        f"Cause     : {d.get('probable_cause')}",
    ]
    if d.get("evidence"):
        lines.append("Evidence  :")
        lines += [f"  - {e}" for e in d["evidence"]]
    if d.get("next_steps"):
        lines.append("Next steps:")
        lines += [f"  {n}. {s}" for n, s in enumerate(d["next_steps"], 1)]
    if d.get("suggested_fix"):
        lines.append(f"Fix       : {d['suggested_fix']}")
    if d.get("escalation"):
        lines.append(f"Escalate  : {d['escalation']}")
    refs = result.get("references") or []
    if refs:
        lines.append("Runbooks  : " + ", ".join(r["source"] for r in refs))
    for note in result.get("notes") or []:
        lines.append(f"Note      : {note}")
    corrections = [c for c in result.get("corrections") or [] if "dropped" not in c]
    if corrections:
        lines.append("Validated : " + "; ".join(corrections))
    return "\n".join(lines)


def format_diagnosis_error(incident: dict, error: str) -> str:
    return f"[INCIDENT DIAGNOSIS] {incident['id']}\nUnavailable: {error}"
