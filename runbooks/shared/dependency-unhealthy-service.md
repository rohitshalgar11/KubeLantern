---
title: Dependency Service exists but has no ready endpoints or wrong port
category: dependency
---
# Symptoms
Connections to a Service time out or are refused although the Service exists.
KubeLantern reports 0 ready endpoints, or that the Service does not expose the port the app uses.

# What to check
1. `kubectl -n <ns> get endpointslices -l kubernetes.io/service-name=<svc>` — any ready addresses?
2. Does the Service selector match the labels on the dependency's pods?
3. Are the dependency's pods Ready? Check their readiness probe and logs.
4. Does the Service `port`/`targetPort` match the port the app connects to and the container listens on?

# Fix
Fix the dependency pods so they become Ready, align the Service selector with
the pod labels, or expose the correct port. Restarting the client app does not help.
