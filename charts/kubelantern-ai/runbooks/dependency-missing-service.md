---
title: Application cannot reach a dependency — Service missing
category: dependency
---
# Symptoms
The application logs "cannot connect", "connection refused", "no such host" or
"dial tcp ... lookup <name>" for a host like `db:5432`, and KubeLantern reports that
no Service with that name exists in the namespace.

# Why it happens
Inside the cluster an app reaches another workload through a Service name.
If the Service was never created, was deleted, or the app uses the wrong name,
DNS cannot resolve it and every connection fails, so the app exits and restarts.

# What to check
1. `kubectl -n <ns> get svc` — is the Service there under the exact name the app uses?
2. Check the app's configuration (env vars such as DB_HOST, DATABASE_URL) for typos.
3. If the dependency lives in another namespace, the app must use `<svc>.<namespace>.svc`.

# Fix
Deploy the dependency (or its Service), or correct the host name in the app's
configuration. For managed databases outside the cluster, create an
ExternalName Service or use the provider's hostname directly.
