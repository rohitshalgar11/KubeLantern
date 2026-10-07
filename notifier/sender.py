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

from notifier.formats import FORMATS, email_message
from notifier.mail import EmailConfig, build_message, smtp_send
from notifier.sender_errors import SendError

log = logging.getLogger("kubelantern.notifier")

RETRYABLE = {408, 425, 429, 500, 502, 503, 504}




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
                 allow_insecure: bool = False, max_per_minute: int = 20, retries: int = 3,
                 email: EmailConfig | None = None,
                 post: Callable[[str, dict], int] = http_post,
                 mail: Callable = smtp_send,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.secret_dir = Path(secret_dir)
        self.channels = [c for c in channels if c in FORMATS or c == "email"]
        self.email = email
        self.mail = mail
        self.detail = detail
        self.allow_insecure = allow_insecure
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
        if not (url.startswith("https://") or (self.allow_insecure and url.startswith("http://"))):
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
            if channel == "email":
                results[channel] = self._deliver_email(event)
                continue
            url = self._url(channel)
            if url is None:
                results[channel] = "not-configured"
                continue
            payload = FORMATS[channel](event, self.detail)
            results[channel] = self._deliver(channel, url, payload, event["kind"], inc_id)
        return results

    def _secret(self, key: str) -> str | None:
        try:
            return (self.secret_dir / key).read_text().strip() or None
        except OSError:
            return None

    def _deliver_email(self, event: dict) -> str:
        cfg = self.email
        problem = cfg.problem() if cfg else "email settings missing"
        if problem:
            if "email" not in self._warned:
                log.error("email channel not configured: %s", problem)
                self._warned.add("email")
            return "not-configured"
        if cfg.tls == "none" and not self.allow_insecure:
            log.error("refusing to send email without TLS (tls: none is for testing only)")
            return "not-configured"
        subject, text, html = email_message(event, self.detail)
        msg = build_message(cfg, subject, text, html)
        user, password = self._secret("smtp-username"), self._secret("smtp-password")
        return self._retry("email", lambda: self.mail(cfg, msg, user, password),
                           event["kind"], event["incident"].get("id"))

    def _deliver(self, channel, url, payload, kind, inc_id) -> str:
        return self._retry(channel, lambda: self.post(url, payload), kind, inc_id)

    def _retry(self, channel, attempt_fn, kind, inc_id) -> str:
        delay = 2.0
        for attempt in range(1, self.retries + 1):
            try:
                status = attempt_fn()
                log.info("sent %s for %s to %s%s", kind, inc_id, channel,
                         f" (HTTP {status})" if isinstance(status, int) else "")
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
