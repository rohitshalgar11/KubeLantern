---
title: Mounted config file missing or empty
category: configuration
---
# Symptoms
The application exits at startup because a file it expects is not there or is a directory:
- `open /etc/app/config.yaml: no such file or directory`
- `FileNotFoundError: [Errno 2] No such file or directory: '/config/settings.json'`
- `java.io.FileNotFoundException: /app/config/application.yml`
- `is a directory` when reading a path that should be a file

# Why it happens
The ConfigMap or Secret is mounted, but under a different path or file name than
the app reads. Each key becomes one file, named after the key. A `subPath` mount
with a wrong key creates an empty directory instead of a file. `items:` in the
volume limits which keys appear.

# What to check
1. Volume and mount: `kubectl -n <ns> get deploy <name> -o yaml` — `volumes`, `volumeMounts`, `subPath`, `items`.
2. Keys in the ConfigMap/Secret: `kubectl -n <ns> get configmap <cm> -o jsonpath='{.data}'`.
3. The path the application reads (env var, flag, framework default).

# Fix
Make the key name match the file name the app expects (or use `items: [{key, path}]`),
fix the `mountPath`/`subPath`, or point the app at the right path. Note that files
mounted with `subPath` are not updated when the ConfigMap changes.
