"""Credential redaction shared by the agent (before sending) and the gateway
(defence in depth, before anything reaches the LLM)."""

from __future__ import annotations

import re
from typing import Any

_PATTERNS: list[tuple[re.Pattern, str]] = [
    # key=value / key: value style secrets
    (re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|client[_-]?secret)"
                r"\b(\s*[=:]\s*)(\S+)"), r"\1\2[REDACTED]"),
    # Authorization headers
    (re.compile(r"(?i)(authorization:\s*(?:bearer|basic)\s+)\S+"), r"\1[REDACTED]"),
    # credentials embedded in URLs: scheme://user:pass@host
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^:/\s]+:)[^@\s]+@"), r"\1[REDACTED]@"),
    # JWTs
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"), "[REDACTED_JWT]"),
    # AWS access key ids
    (re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"), "[REDACTED_AWS_KEY]"),
    # PEM private keys
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
     "[REDACTED_PRIVATE_KEY]"),
]


def redact(text: str | None) -> str | None:
    if not text:
        return text
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


def redact_obj(obj: Any) -> Any:
    """Recursively redact every string inside dicts/lists."""
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        return {k: redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_obj(v) for v in obj]
    return obj
