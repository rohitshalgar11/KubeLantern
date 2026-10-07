---
title: Java heap out of memory (OutOfMemoryError) or JVM sized wrong for the container
---
# Symptoms
- `Exception in thread "main" java.lang.OutOfMemoryError: Java heap space`
- `java.lang.OutOfMemoryError: GC overhead limit exceeded`
- `java.lang.OutOfMemoryError: Metaspace` / `unable to create native thread`
- or the container is OOMKilled (exit 137) although the heap looks small.

# Why it happens
`Java heap space`: the heap (`-Xmx`, or a percentage of the container limit) is too
small for the workload, or memory leaks. OOMKilled instead: the JVM's total memory
(heap + metaspace + threads + direct buffers) is larger than the container limit,
typically because `-Xmx` is set close to or above the limit.

# What to check
1. JVM flags: `JAVA_TOOL_OPTIONS`, `JAVA_OPTS`, `-Xmx`, `-XX:MaxRAMPercentage` in the Deployment.
2. The container memory limit: `kubectl -n <ns> get deploy <name> -o jsonpath='{..resources.limits.memory}'`.
3. Did memory use grow after a release (leak) or with more data/traffic?

# Fix
Size the heap relative to the container: `-XX:MaxRAMPercentage=75` and no fixed
`-Xmx`, leaving about 25% of the limit for non-heap memory. Raise the memory limit
if the workload really needs more. For leaks, capture a heap dump
(`-XX:+HeapDumpOnOutOfMemoryError -XX:HeapDumpPath=/tmp` with an `emptyDir` at /tmp)
and roll back to the last good version.
