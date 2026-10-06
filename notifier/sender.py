"""Delivery: one HTTPS POST per configured channel, with retries and a rate limit.

Webhook URLs are read from files (a mounted Secret, one key per channel) at send
time, so a rotated Secret is picked up without a restart. URLs are never logged.
"""

from __future__ import annotations

import collections
import json
import logging
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

from notifier.formats import FORMATS

log = logging.getLogger("kubelantern.notifier")

RETRYABLE = {408, 425, 429, 500, 502, 503, 504}


class SendError(Exception):
    def __init__(self, message: str, status: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.status, self.retry_after = status, retry_after


def http_post(url: str, payload: dict, timeout: float = 10) -> int:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "kubelantern-notifier"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        ra = e.headers.get("Retry-After") if e.headers else None
        try:
            retry_after = float(ra) if ra else None
        except ValueError:
            retry_after = None
        raise SendError(f"HTTP {e.code}", e.code, retry_after) from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        reason = getattr(e, "reason", None) or type(e).__name__
        raise SendError(f"unreachable: {reason}") from None


class Sender:
    def __init__(self, secret_dir: str | Path, channels: list[str], detail: str = "summary",
                 allow_http: bool = False, max_per_minute: int = 20, retries: int = 3,
                 post: Callable[[str, dict], int] = http_post,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.secret_dir = Path(secret_dir)
        self.channels = [c for c in channels if c in FORMATS]
        self.detail = detail
        self.allow_http = allow_http
        self.max_per_minute = max_per_minute
        self.retries = retries
        self.post, self.sleep, self.clock = post, sleep, clock
        self._sent: collections.deque[float] = collections.deque()
        self._warned: set[str] = set()

    def _url(self, channel: str) -> str | None:
        f = self.secret_dir / channel
        try:
            url = f.read_text().strip()
        except OSError:
            url = ""
        if not url:
            if channel not in self._warned:
                log.warning("no webhook URL for channel %s (key '%s' missing in the Secret)",
                            channel, channel)
                self._warned.add(channel)
            return None
        if not (url.startswith("https://") or (self.allow_http and url.startswith("http://"))):
            log.error("refusing to send to channel %s: URL must use https", channel)
            return None
        self._warned.discard(channel)
        return url

    def _rate_ok(self) -> bool:
        now = self.clock()
        while self._sent and now - self._sent[0] > 60:
            self._sent.popleft()
        if len(self._sent) >= self.max_per_minute:
            return False
        self._sent.append(now)
        return True

    def send(self, event: dict) -> dict[str, str]:
        """Deliver one event to every channel. Returns channel -> outcome."""
        inc_id = event["incident"].get("id")
        if not self._rate_ok():
            log.warning("rate limit (%d/min): dropped %s notification for %s",
                        self.max_per_minute, event["kind"], inc_id)
            return {c: "rate-limited" for c in self.channels}
        results = {}
        for channel in self.channels:
            url = self._url(channel)
            if url is None:
                results[channel] = "not-configured"
                continue
            payload = FORMATS[channel](event, self.detail)
            results[channel] = self._deliver(channel, url, payload, event["kind"], inc_id)
        return results

    def _deliver(self, channel, url, payload, kind, inc_id) -> str:
        delay = 2.0
        for attempt in range(1, self.retries + 1):
            try:
                status = self.post(url, payload)
                log.info("sent %s for %s to %s (HTTP %s)", kind, inc_id, channel, status)
                return "sent"
            except SendError as e:
                retryable = e.status is None or e.status in RETRYABLE
                if not retryable or attempt == self.retries:
                    log.warning("sending %s for %s to %s failed: %s", kind, inc_id, channel, e)
                    return "failed"
                wait = min(e.retry_after or delay, 30)
                log.info("%s to %s: %s, retrying in %.0fs", kind, channel, e, wait)
                self.sleep(wait)
                delay *= 2
        return "failed"
