---
title: Crash loop that started with a new rollout
---
# Symptoms
The workload was healthy, then the new ReplicaSet's pods crash or never become
Ready right after a deployment (new image tag, changed config, new env vars).
Old pods may still be running if the rollout is stuck.

# What to check
1. What changed: `kubectl -n <ns> rollout history deploy/<name>` and
   `kubectl -n <ns> get rs -l app=<name>` (compare the image and config between ReplicaSets).
2. The new pods' previous logs: `kubectl -n <ns> logs <new-pod> --previous`.
3. Config changes deployed at the same time (ConfigMaps, Secrets, env vars, feature flags).
4. Schema or API changes in dependencies released together with this version.

# Fix
Roll back first to restore service: `kubectl -n <ns> rollout undo deploy/<name>`
(with GitOps, revert the commit instead — ArgoCD would undo a manual rollback).
Then reproduce and fix the new version. A readiness probe and `maxUnavailable: 0`
keep old pods serving while a bad version fails to start.
