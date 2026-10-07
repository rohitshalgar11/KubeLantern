---
title: Permission denied when running as a non-root user
category: permissions
---
# Symptoms
- `Permission denied` / `EACCES: permission denied, open '/app/data/x'`
- `mkdir: can't create directory '/data': Permission denied`
- `listen tcp :80: bind: permission denied` / `EACCES ... listen 0.0.0.0:443`
- `chown: changing ownership of '/data': Operation not permitted`

# Why it happens
The pod runs as a non-root user (`runAsNonRoot`, `runAsUser`, often enforced by
cluster policy), but files in the image or on the volume belong to root, or the
app binds to a privileged port (below 1024), which needs root or extra capabilities.

# What to check
1. Which user the container runs as: `kubectl -n <ns> get pod <pod> -o jsonpath='{..securityContext}'`.
2. Ownership of the path in the image (`docker run --rm --entrypoint ls <image> -ln <path>`).
3. Volumes: is `fsGroup` set so the pod's group can write to the volume?
4. The port the app listens on.

# Fix
Make the paths writable for the app's user in the Dockerfile (`chown`/`chmod` at
build time), set `securityContext.fsGroup` for volumes, and listen on a port above
1024 (for example 8080) with the Service mapping port 80 to it. Don't switch to
root or add capabilities to work around it.
