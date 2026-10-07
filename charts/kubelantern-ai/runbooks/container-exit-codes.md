---
title: What a container exit code means
---
# Symptoms
A container terminated with an exit code and the logs do not explain it.

# Exit codes
- `0` — exited normally. In a Deployment, the process should not exit at all: a
  command that runs and finishes (a script, `echo`) causes a restart loop.
- `1` — generic application error. Read the logs: the error is usually just above the exit.
- `2` — misuse of a shell command or an invalid argument / flag.
- `126` — the command exists but is not executable (permissions, wrong format).
- `127` — command not found (wrong path in `command`, missing binary, no shell in distroless images).
- `128 + n` — killed by signal n:
  - `137` (SIGKILL): OOMKilled if the reason says so; otherwise killed by the kubelet
    after a failed liveness probe or a missed shutdown deadline.
  - `139` (SIGSEGV): segmentation fault — native crash, bad native library, wrong architecture.
  - `143` (SIGTERM): stopped on purpose (rollout, scale-down, node drain). Not a failure
    unless it repeats without anyone stopping the pod.
  - `134` (SIGABRT): the process aborted itself (assertion, `abort()`, Node.js fatal error).
- `255` — exit status out of range, or the runtime could not start the process at all.

# What to check
`kubectl -n <ns> get pod <pod> -o jsonpath='{.status.containerStatuses[*].lastState.terminated}'`
shows the reason, exit code and when it happened.
