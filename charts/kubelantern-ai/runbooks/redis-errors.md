---
title: Redis errors (READONLY, OOM, MOVED, NOAUTH)
---
# Symptoms
- `READONLY You can't write against a read only replica.`
- `OOM command not allowed when used memory > 'maxmemory'.`
- `MOVED 3999 10.0.0.5:6379` / `CROSSSLOT Keys in request don't hash to the same slot`
- `NOAUTH Authentication required.` / `WRONGPASS`
- `LOADING Redis is loading the dataset in memory`

# Why it happens
- READONLY: the client is connected to a replica, usually after a failover, with a
  connection that was not refreshed.
- OOM: Redis reached `maxmemory` with an eviction policy that doesn't evict (`noeviction`).
- MOVED / CROSSSLOT: a cluster-mode Redis used with a non-cluster client or multi-key commands.
- NOAUTH / WRONGPASS: password missing or wrong (see the database login runbook).
- LOADING: Redis restarted and is still loading its data; retry shortly.

# Fix
Connect through the primary endpoint or Sentinel/cluster-aware client and reconnect
on failover; set TTLs and an eviction policy (`allkeys-lru`) for caches or add memory;
use a cluster-aware client and hash tags for multi-key operations; fix credentials.
