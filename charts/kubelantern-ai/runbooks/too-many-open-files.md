---
title: Too many open files (file descriptor or socket leak)
---
# Symptoms
- `Too many open files` / `EMFILE: too many open files`
- `accept tcp [::]:8080: accept4: too many open files; retrying in 1s`
- `java.net.SocketException: Too many open files`
- `OSError: [Errno 24] Too many open files`

# Why it happens
The process opens files or network connections and does not close them (a leak),
so it reaches the per-process limit on open file descriptors. Each HTTP or
database connection counts. Under high load, many short connections without
keep-alive or pooling also reach the limit.

# What to check
1. Open descriptors over time (monitoring metric `process_open_fds`), or
   `kubectl -n <ns> exec <pod> -- sh -c 'ls /proc/1/fd | wc -l'`.
2. Code paths that open files, sockets or HTTP clients without closing them
   (a new HTTP client per request is a common cause).
3. Connection pooling and keep-alive settings for outgoing calls.

# Fix
Close files and responses (use `with` / try-with-resources / `defer`), reuse one
HTTP client and a connection pool, and fix the leak. Raising the limit only
delays the failure.
