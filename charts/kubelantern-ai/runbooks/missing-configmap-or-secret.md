---
title: CreateContainerConfigError: missing Secret, ConfigMap or key
category: configuration
---
# Symptoms
The pod stays in CreateContainerConfigError and the container never starts (no logs).
Events show messages such as:
- `Error: secret "db-credentials" not found`
- `Error: configmap "app-config" not found`
- `Error: couldn't find key DB_PASSWORD in Secret <ns>/db-credentials`

# Why it happens
An `env.valueFrom`, `envFrom` or volume refers to a Secret or ConfigMap (or a key
inside it) that does not exist in this namespace. Typical causes: the Secret is
created by another tool (External Secrets, Sealed Secrets, a pipeline) that has
not synced yet; a key was renamed; the object was created in another namespace.

# What to check
1. `kubectl -n <ns> get secret,configmap` — is the object there, in this namespace?
2. `kubectl -n <ns> describe pod <pod>` — the event names the exact object and key.
3. For External Secrets: `kubectl -n <ns> get externalsecret` — is it Ready, or failing to read the vault?
4. Compare the key names in the object with the keys the Deployment references (case matters).

# Fix
Create the missing object or key, fix the reference, or fix the secret sync. If a
value is genuinely optional, mark it `optional: true` in the reference. The pod
starts on its own once the object exists; no restart is needed.
