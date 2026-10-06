---
title: Container OOMKilled (exit 137)
category: resources
---
# Symptoms
Container terminated with reason OOMKilled and exit code 137, usually followed
by CrashLoopBackOff. The process used more memory than its memory limit.

# What to check
1. Current limit: `kubectl -n <ns> get deploy <name> -o jsonpath='{..resources}'`.
2. Actual usage before the kill: `kubectl -n <ns> top pod` (needs metrics-server) or your monitoring.
3. Did a recent release change memory behaviour (bigger cache, larger batch, memory leak)?
4. JVM / Node / Python runtimes: is the heap size configured below the container limit?

# Fix
Raise `resources.limits.memory` to observed peak plus about 25% headroom, and set the
request close to normal usage. If usage grows without bound, treat it as a memory
leak and roll back. Never remove the limit entirely on a shared cluster.
