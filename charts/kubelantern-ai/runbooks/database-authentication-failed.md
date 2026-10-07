---
title: Database login failed (wrong user, password or access rules)
---
# Symptoms
- PostgreSQL: `FATAL: password authentication failed for user "app"`,
  `no pg_hba.conf entry for host "10.x.x.x", user "app", database "orders", SSL off`
- MySQL: `Access denied for user 'app'@'10.x.x.x' (using password: YES)`
- SQL Server: `Login failed for user 'app'.`
- MongoDB: `Authentication failed.` / `bad auth`
- Redis: `NOAUTH Authentication required.` / `WRONGPASS invalid username-password pair`

# Why it happens
The credentials in the Secret are wrong or were rotated, the user does not exist
or has no access to that database, the server only accepts SSL (`SSL off` in the
message), or the server's access rules don't include the pod's IP range.

# What to check
1. Which Secret and keys feed the credentials, and when they last changed (rotation?).
2. Whether a newly rotated password reached the pod: env vars from Secrets only update on restart.
3. The database user, database name and host in the connection string.
4. SSL requirement: add `sslmode=require` (PostgreSQL) or the driver's equivalent.

# Fix
Correct the Secret (or fix the secret sync), then restart the pods so they read the
new value: `kubectl -n <ns> rollout restart deploy/<name>`. Grant the user access to
the database, enable SSL in the client, or have the DBA allow the cluster's network.
