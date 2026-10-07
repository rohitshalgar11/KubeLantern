---
title: Image built for the wrong CPU architecture (exec format error)
category: image
---
# Symptoms
The container exits immediately, often with exit code 1, 126 or 255, and the only
log line is similar to:
- `exec /app/server: exec format error`
- `standard_init_linux.go: exec user process caused: exec format error`
- `exec /usr/local/bin/docker-entrypoint.sh: exec format error`

# Why it happens
The image (or the binary inside it) was built for another CPU architecture, for
example arm64 on an Apple Silicon laptop, and the node runs amd64 (or the
opposite). A shell script without a `#!` line on its first line gives the same error.

# What to check
1. Architecture of the nodes: `kubectl get nodes -L kubernetes.io/arch`.
2. Architectures in the image: `docker buildx imagetools inspect <image>:<tag>`.
3. If the entrypoint is a script: does it start with `#!/bin/sh` (and use LF line endings, not CRLF)?

# Fix
Rebuild for the node architecture (`docker buildx build --platform linux/amd64 ...`)
or publish a multi-arch image (`--platform linux/amd64,linux/arm64`). Add the
missing shebang line to entrypoint scripts. Until then, roll back to the last image that ran.
