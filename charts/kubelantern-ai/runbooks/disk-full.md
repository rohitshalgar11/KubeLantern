---
title: No space left on device (disk, volume or emptyDir full)
---
# Symptoms
- `No space left on device` / `ENOSPC` / `java.io.IOException: No space left on device`
- `could not write to file "pg_wal/...": No space left on device`
- `write /data/...: no space left on device`

# Why it happens
The volume (PVC) is full, an `emptyDir` reached its `sizeLimit`, or the container
writes to its own filesystem until the node runs short of disk. Logs written to
files, temporary files, caches and uploads are the usual culprits.

# What to check
1. Which path is full: `kubectl -n <ns> exec <pod> -- df -h`.
2. PVC size and usage; `emptyDir.sizeLimit`; `ephemeral-storage` requests and limits.
3. What grows: log files, temp files, caches, database write-ahead logs.

# Fix
Clean up or rotate what fills the disk, write logs to stdout, and expand the PVC
(if the StorageClass allows volume expansion). Set sizes deliberately so a full
disk is a clear limit, not a surprise.
