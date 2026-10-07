---
title: Pod evicted: node low on memory or disk
category: node
---
# Symptoms
The pod is in phase Failed with reason Evicted and a message such as:
- `The node was low on resource: memory. Container app was using 900Mi, request is 256Mi.`
- `The node was low on resource: ephemeral-storage.`
- `Pod ephemeral local storage usage exceeds the total limit of containers`

# Why it happens
The kubelet evicts pods when the node runs short of memory or disk. Pods that use
much more than they request are evicted first. Disk pressure usually comes from
container logs, files written inside the container, or `emptyDir` volumes.

# What to check
1. The eviction message: `kubectl -n <ns> get pod <pod> -o jsonpath='{.status.message}'`.
2. Requests vs real usage: `kubectl -n <ns> top pod` and the Deployment's `resources`.
3. Is the app writing large files to its own filesystem or an `emptyDir` (temp files, caches, logs to files)?
4. Repeated evictions on the same node point to a node problem: tell the platform team.

# Fix
Set memory requests close to real usage, and set `ephemeral-storage` requests and
limits (or an `emptyDir.sizeLimit`) if the app writes to disk. Write logs to
stdout instead of files, clean up temporary files, and use a volume for large data.
Evicted pods are left behind for inspection; delete them when done.
