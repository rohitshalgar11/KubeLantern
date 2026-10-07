---
title: Node.js JavaScript heap out of memory
---
# Symptoms
- `FATAL ERROR: Reached heap limit Allocation failed - JavaScript heap out of memory`
- `FATAL ERROR: Ineffective mark-compacts near heap limit`
- exit code 134 (SIGABRT), or OOMKilled (137) if the container limit is hit first.

# Why it happens
V8's heap limit is reached: memory leak (growing caches, listeners, global arrays),
a large payload loaded at once, or a heap limit that doesn't match the container
(`--max-old-space-size` too small, or larger than the container limit).

# What to check
1. `NODE_OPTIONS` / `--max-old-space-size` in the Deployment vs the container memory limit.
2. Memory over time in monitoring: steady growth means a leak.
3. Recent changes that load more data in memory (big JSON, no pagination, in-memory caches).

# Fix
Set `--max-old-space-size` to about 75% of the container memory limit (in MB), raise
the limit if needed, stream or paginate large data, and bound caches. For leaks,
take a heap snapshot (`--heapsnapshot-near-heap-limit=1`) and roll back meanwhile.
