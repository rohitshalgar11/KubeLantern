---
title: Application crash with no infrastructure cause
category: application-error
---
# Symptoms
The container exits with a non-zero code (often 1 or 2), the logs show a stack
trace, panic or unhandled exception, and there is no OOM, image, probe or dependency signal.

# What to check
1. Read the previous container's logs: `kubectl -n <ns> logs <pod> --previous`.
2. Did it start after a deployment? Compare image tags: `kubectl -n <ns> rollout history deploy/<name>`.
3. Does it fail on specific input or data?

# Fix
Roll back to the last good version with `kubectl -n <ns> rollout undo deploy/<name>`,
then fix the bug and redeploy. This is the application team's code, not a cluster problem.
