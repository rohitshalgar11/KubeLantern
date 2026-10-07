---
title: Database migration fails at startup
---
# Symptoms
Startup or an init container/Job fails while migrating the schema:
- Flyway: `FlywayException: Validate failed: Migrations have failed validation`,
  `Detected failed migration to version 12`
- Liquibase: `Waiting for changelog lock....` (forever), `ValidationFailedException`
- Alembic: `Can't locate revision identified by 'abc123'`
- Django: `django.db.utils.ProgrammingError: relation "orders_order" does not exist`
- `duplicate column` / `already exists` errors

# Why it happens
A migration was edited after it ran (checksum mismatch), a previous migration failed
half-way, several pods ran migrations at the same time, a stale lock remains after a
crash, or the code expects a schema that wasn't migrated yet.

# What to check
1. The migration tool's history table (`flyway_schema_history`, `databasechangelog`, `alembic_version`).
2. A leftover lock (`databasechangeloglock`) from a crashed pod.
3. Whether migrations run in every replica on startup (race) instead of once.

# Fix
Run migrations once per release (a Job or an init step that only one pod runs, or
an ArgoCD PreSync hook), never edit applied migrations — add a new one, repair the
failed entry with the tool's repair command after checking the schema, and release
stale locks only when no migration is running.
