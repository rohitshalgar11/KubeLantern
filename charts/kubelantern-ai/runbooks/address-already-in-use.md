---
title: Port already in use or bind failure
---
# Symptoms
- `listen tcp :8080: bind: address already in use`
- `Error: listen EADDRINUSE: address already in use :::3000`
- `OSError: [Errno 98] Address already in use`
- `Port 8080 was already in use.` (Spring)

# Why it happens
Two processes in the same pod listen on the same port. Containers in a pod share
one network namespace, so a sidecar (proxy, metrics exporter) and the app cannot
use the same port. Also: the app starts its server twice (two frameworks, reload
mode left on), or the image starts a process and the command starts another.

# What to check
1. All containers in the pod and their ports: `kubectl -n <ns> get pod <pod> -o jsonpath='{range .spec.containers[*]}{.name} {.ports}{"\n"}{end}'`.
2. The port the app is configured with (`PORT`, `server.port`, `ASPNETCORE_URLS`).
3. Dev settings left on (auto-reload, debug servers).

# Fix
Give each container in the pod its own port, and keep the Service `targetPort` and
probes in sync with it. Turn off reload/debug servers in production images.
