---
title: Read-only file system errors
category: permissions
---
# Symptoms
The application fails when it writes a file:
- `OSError: [Errno 30] Read-only file system: '/app/cache'`
- `EROFS: read-only file system, open '/usr/src/app/logs/app.log'`
- `java.nio.file.FileSystemException: /tmp/...: Read-only file system`
- nginx: `mkdir() "/var/cache/nginx/client_temp" failed (30: Read-only file system)`

# Why it happens
The container runs with `securityContext.readOnlyRootFilesystem: true` (often
required by cluster policy), so only mounted volumes are writable. The app writes
caches, temp files, PID files or logs inside the image's filesystem.

# What to check
1. Which path the app writes to (in the error).
2. `kubectl -n <ns> get deploy <name> -o jsonpath='{..securityContext}'`.
3. Is there a volume mounted at that path?

# Fix
Mount an `emptyDir` at each path the app needs to write (for example `/tmp`,
`/var/cache/nginx`, `/app/cache`), or configure the app to write to such a path.
Log to stdout instead of files. Keep `readOnlyRootFilesystem` on: it is a security control.
