---
title: Container command or entrypoint not found
category: configuration
---
# Symptoms
The container never starts or exits with code 127 (or 126). Pod state shows
RunContainerError, CreateContainerError or StartError, with a message such as:
- `exec: "app": executable file not found in $PATH`
- `OCI runtime create failed: ... no such file or directory`
- `/bin/sh: 1: ./start.sh: not found` or `sh: start.sh: Permission denied` (exit 126)

# Why it happens
The `command`/`args` in the pod spec (or the image ENTRYPOINT/CMD) points to a
file that is not in the image, not on PATH, or not executable. Common after
switching to a slim or distroless base image (no shell, no bash), or when a
build step that copied the binary was removed.

# What to check
1. The pod's command and args: `kubectl -n <ns> get pod <pod> -o jsonpath='{.spec.containers[*].command} {.spec.containers[*].args}'`.
2. The image's own entrypoint: `docker image inspect <image> --format '{{.Config.Entrypoint}} {{.Config.Cmd}}'`.
3. Does the path exist and is it executable in the image? (`docker run --rm --entrypoint ls <image> -l <path>`)
4. Distroless images have no `/bin/sh`: `command: ["sh", "-c", ...]` cannot work there.

# Fix
Point `command` at a file that exists in the image, add the missing file in the
Dockerfile (and `chmod +x` it), or remove the override so the image's own
ENTRYPOINT is used. Exit 126 means the file exists but is not executable.
