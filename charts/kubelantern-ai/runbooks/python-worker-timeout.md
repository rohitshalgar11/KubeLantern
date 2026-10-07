---
title: Gunicorn / Uvicorn workers killed (WORKER TIMEOUT)
---
# Symptoms
- `[CRITICAL] WORKER TIMEOUT (pid:42)`
- `Worker (pid:42) was sent SIGKILL! Perhaps out of memory?`
- `Worker exiting` loops, 502/504 errors from the ingress, readiness probe timeouts.

# Why it happens
A worker took longer than gunicorn's `--timeout` (30 s by default) on one request
(slow database query, slow external API, CPU-heavy work), or it was killed for using
too much memory. CPU throttling from a low CPU limit makes everything slower.

# What to check
1. Slow requests: application logs and traces around the timeout.
2. Memory: is the container near its limit, or OOMKilled? Each worker is a full process.
3. CPU limit and throttling (monitoring: `container_cpu_cfs_throttled_periods_total`).
4. Number of workers vs CPU/memory requested.

# Fix
Fix or move the slow work (timeouts on outgoing calls, background jobs for long
tasks), raise `--timeout` only if long requests are expected, size workers to the
resources (`workers = 2 x CPU + 1` is an upper bound), and raise the memory limit if
workers are OOM-killed.
