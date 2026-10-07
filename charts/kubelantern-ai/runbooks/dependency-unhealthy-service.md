---
title: Dependency Service exists, but on another port or with no ready endpoints
category: dependency
---
# Symptoms
The Service the application connects to exists, yet connections are refused or time out:
- `dial tcp redis:6380: connect: connection refused` while the Service `redis` exposes port 6379
- KubeLantern reports "Service 'redis' exists (ports [6379])" and "Service 'redis' does not expose port 6380": the app uses the wrong port
- KubeLantern reports "Service 'db' has 0 ready endpoints": no healthy pod behind the Service

# What to check
1. Wrong port: compare the port in the app's configuration (env vars such as REDIS_URL, DB_PORT) with `kubectl -n <ns> get svc <svc> -o jsonpath='{.spec.ports}'`.
2. No endpoints: `kubectl -n <ns> get endpointslices -l kubernetes.io/service-name=<svc>` — any ready addresses?
3. Does the Service selector match the labels on the dependency's pods?
4. Are the dependency's pods Ready? Check their readiness probe and logs.

# Fix
Use the port the Service exposes (or expose the port the app needs, with `targetPort`
matching the container port), fix the dependency pods so they become Ready, or align
the Service selector with the pod labels. Restarting the client app does not help.
