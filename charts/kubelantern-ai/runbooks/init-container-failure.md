---
title: Init container failing (Init:CrashLoopBackOff, Init:Error)
---
# Symptoms
The pod status is `Init:CrashLoopBackOff`, `Init:Error` or stuck at `Init:0/1`,
and the main container never starts. The failing container is an init container
(a database migration, a wait-for-dependency script, a config downloader).

# Why it happens
Init containers run one after another before the app and must all succeed. Typical
failures: a migration error, a dependency that is not reachable yet, a missing
Secret for the init step, or a wait script that times out.

# What to check
1. Which init container fails: `kubectl -n <ns> get pod <pod> -o jsonpath='{.status.initContainerStatuses[*].name}'`.
2. Its logs: `kubectl -n <ns> logs <pod> -c <init-container> --previous`.
3. Then use the runbook that matches the error in those logs (dependency, configuration, migration).

# Fix
Fix the cause shown in the init container's logs. Make wait-for scripts time out
with a clear message, and make migrations safe to re-run (idempotent).
