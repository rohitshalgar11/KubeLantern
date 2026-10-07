---
title: Application exits on startup because of configuration
category: configuration
---
# Symptoms
The container exits quickly with a non-zero code, and the logs mention a missing
environment variable, an invalid value, a missing file, or an unknown flag.
CreateContainerConfigError means a referenced Secret or ConfigMap key does not exist.

# What to check
1. Compare the env vars and mounted files the app expects with the Deployment spec.
2. For CreateContainerConfigError, the event names the missing Secret/ConfigMap or key.
3. Did a recent change rename a variable or remove a key?

# Fix
Add the missing variable or key, correct the value, or roll back the change that removed it.
