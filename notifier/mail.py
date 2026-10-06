"""Email delivery — e.g. to a Microsoft Teams channel's email address.

Any SMTP service works: Azure Communication Services Email, a company relay,
SendGrid... Credentials come from the mounted Secret (keys `smtp-username`,
`smtp-password`; both optional for relays without authentication). TLS is
required (STARTTLS or SSL); `none` exists only for the in-cluster test receiver.
"""

from __future__ import annotations

import smtplib
import ssl
from collections.abc import Callable
from dataclasses import dataclass, field
from email.message import EmailMessage

from notifier.sender_errors import SendError


@dataclass
class EmailConfig:
    host: str
    port: int = 587
    sender: str = ""
    to: list[str] = field(default_factory=list)
    tls: str = "starttls"          # starttls | ssl | none (testing only)
    timeout: float = 20

    def problem(self) -> str | None:
        if not self.host:
            return "notifications.email.smtpHost is empty"
        if not self.sender or "@" not in self.sender:
            return "notifications.email.from is not an email address"
        if not self.to or any("@" not in t for t in self.to):
            return "notifications.email.to must list email addresses"
        if self.tls not in ("starttls", "ssl", "none"):
            return "notifications.email.tls must be starttls, ssl or none"
        return None


def build_message(cfg: EmailConfig, subject: str, text: str, html: str) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg.sender
    msg["To"] = ", ".join(cfg.to)
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    return msg


def smtp_send(cfg: EmailConfig, msg: EmailMessage, username: str | None, password: str | None,
              smtp_factory: Callable | None = None, ssl_factory: Callable | None = None) -> None:
    """Send one message. Raises SendError(status=None) for retryable failures and
    SendError(status=400) for permanent ones (bad credentials, refused recipients)."""
    ctx = ssl.create_default_context()
    try:
        if cfg.tls == "ssl":
            factory = ssl_factory or smtplib.SMTP_SSL
            server = factory(cfg.host, cfg.port, timeout=cfg.timeout, context=ctx)
        else:
            factory = smtp_factory or smtplib.SMTP
            server = factory(cfg.host, cfg.port, timeout=cfg.timeout)
        with server:
            server.ehlo()
            if cfg.tls == "starttls":
                server.starttls(context=ctx)
                server.ehlo()
            if username:
                server.login(username, password or "")
            server.send_message(msg)
    except (smtplib.SMTPAuthenticationError, smtplib.SMTPRecipientsRefused,
            smtplib.SMTPSenderRefused, smtplib.SMTPNotSupportedError) as e:
        raise SendError(f"SMTP refused: {type(e).__name__}", status=400) from None
    except (TimeoutError, smtplib.SMTPException, OSError) as e:
        raise SendError(f"SMTP unreachable: {type(e).__name__}") from None
