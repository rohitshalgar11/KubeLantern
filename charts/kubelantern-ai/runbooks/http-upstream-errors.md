---
title: Calls to another service fail with 401, 403, 404, 429 or 5xx
---
# Symptoms
The application crashes or fails health checks because an HTTP call to an API fails:
- `401 Unauthorized` / `403 Forbidden` — credentials, token or permissions
- `404 Not Found` — wrong URL, path or API version
- `429 Too Many Requests` — rate limited
- `502 Bad Gateway`, `503 Service Unavailable`, `504 Gateway Timeout` — the upstream or a proxy in between is failing

# What to check
1. Which URL is called (from the error or configuration) and from which environment.
2. 401/403: is the API key/token in the Secret current, not expired, for the right environment?
3. 404: base URL, path prefix and API version after the other team's release.
4. 429: request volume, retries without backoff, many replicas sharing one key.
5. 5xx: is the upstream itself healthy? Check its status page or owning team.

# Fix
Correct credentials or URLs in configuration, add retries with exponential backoff
and jitter (respect `Retry-After`), add timeouts on every outgoing call, and don't
crash the whole application on a failing optional dependency: degrade and report
not-ready instead.
