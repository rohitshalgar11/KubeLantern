---
title: Database refuses connections - too many clients / connections, or the connection pool is exhausted
---
# Symptoms
- PostgreSQL: `FATAL: sorry, too many clients already`, `remaining connection slots are reserved`
- MySQL: `Too many connections`
- Java/Hikari: `HikariPool-1 - Connection is not available, request timed out after 30000ms`
- Python/SQLAlchemy: `QueuePool limit of size 5 overflow 10 reached, connection timed out`
- requests slow down and time out while CPU is low.

# Why it happens
Every pod opens its own pool, so total connections = replicas x pool size (x workers
per pod). Scaling out, an autoscaler, or a rollout (old and new pods at once) can
exceed the database's limit. Connection leaks (not returned to the pool) and slow
queries holding connections also exhaust a pool.

# What to check
1. Replicas (and HPA max) x pool size vs the database's `max_connections`.
2. Long-running queries or locks on the database side.
3. Code paths that don't close connections/sessions (leaks show as a pool that only grows).

# Fix
Lower the pool size per pod so the total fits (leaving room for rollouts), use a
connection pooler (PgBouncer) for many replicas, fix leaks, and add timeouts on
queries. Raising the database limit is a last resort: each connection uses server memory.
