---
title: Liveness or readiness probe failures
category: probe
---
# Symptoms
Events show "Liveness probe failed" (container restarted) or "Readiness probe failed"
(pod removed from Service endpoints).

# What to check
1. Does the probe path/port exist in the app? A 404 from the probe is a misconfiguration.
2. Is the app slow to start? Liveness probes that fire before startup completes cause restart loops.
3. Is the app overloaded (CPU throttling) so the probe times out?

# Fix
Point the probe at a real health endpoint, add a startupProbe or increase
initialDelaySeconds for slow starters, and raise timeoutSeconds/failureThreshold
if the app is healthy but slow. Liveness probes must not depend on downstream services.
