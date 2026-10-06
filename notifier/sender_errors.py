"""Shared delivery error (HTTP and SMTP)."""


class SendError(Exception):
    """status None = retryable (network, 5xx…); 4xx-style status = permanent."""

    def __init__(self, message: str, status: int | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.status, self.retry_after = status, retry_after
