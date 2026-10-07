---
title: Application killed while still starting (slow start, CPU limits)
category: probe
---
# Symptoms
The container is restarted again and again during startup. Logs stop in the middle
of initialisation (loading caches, warming up, migrating), and events show
`Liveness probe failed` or `Startup probe failed` before the app is ready. Exit code
137 without OOMKilled.

# Why it happens
The liveness probe begins checking before the app is ready to answer and kills it.
Low CPU limits make startup much slower (JVM, .NET and Python apps load and compile
a lot at startup), so a probe that worked on a developer machine is too strict here.

# What to check
1. Probe settings: `kubectl -n <ns> get deploy <name> -o jsonpath='{..livenessProbe}'`.
2. How long a normal start takes (time between start and "ready" log line).
3. CPU limit and throttling during startup.

# Fix
Add a `startupProbe` with enough time (e.g. `failureThreshold: 30`, `periodSeconds: 10`
for up to 5 minutes); liveness checks only begin after it succeeds. Raise the CPU
limit or request if startup is CPU-bound. Don't just remove the liveness probe.
