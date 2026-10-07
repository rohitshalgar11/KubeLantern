"""Wait until the gateway has loaded the shared runbooks that are in the cluster now.

After a change (helm upgrade, an edited ConfigMap) the kubelet needs up to a
minute or two to update the mounted files, and the gateway then re-embeds what
changed. Searching before that tests the OLD runbooks. This script computes the
version the gateway will log for the current ConfigMaps — the same digest as
gateway.knowledge.scan_shared — and waits for "version <digest>" in its log.

Usage: python3 tests/kb/wait_reload.py [--namespace kubelantern-ai] [--timeout 300]
Runs on your machine; needs only kubectl and Python 3.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time

ROOT = "/etc/kubelantern/runbooks"


def kubectl(*args: str) -> str:
    return subprocess.run(["kubectl", *args], check=True, capture_output=True, text=True).stdout


def expected_version(ns: str) -> tuple[str, int]:
    dep = json.loads(kubectl("-n", ns, "get", "deploy", "kubelantern-gateway", "-o", "json"))
    spec = dep["spec"]["template"]["spec"]
    volumes = {v["name"]: v.get("configMap", {}).get("name") for v in spec.get("volumes", [])}
    gateway = next(c for c in spec["containers"] if c["name"] == "gateway")
    folders = {}
    for m in gateway.get("volumeMounts", []):
        path = m["mountPath"].rstrip("/")
        if path.startswith(ROOT + "/") and volumes.get(m["name"]):
            folders[path[len(ROOT) + 1:]] = volumes[m["name"]]
    h, files = hashlib.sha256(), 0
    for folder in sorted(folders):
        try:
            data = json.loads(kubectl("-n", ns, "get", "configmap", folders[folder], "-o", "json"))
        except subprocess.CalledProcessError:
            continue                                   # optional ConfigMap that doesn't exist
        for key in sorted(k for k in (data.get("data") or {}) if k.endswith(".md")):
            h.update(f"{folder}/{key}".encode() + b"\0" + data["data"][key].encode() + b"\0")
            files += 1
    return h.hexdigest()[:16], files


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--namespace", default="kubelantern-ai")
    p.add_argument("--timeout", type=int, default=300)
    args = p.parse_args()
    version, files = expected_version(args.namespace)
    print(f"Waiting for the gateway to load shared runbooks version {version} ({files} files)...",
          flush=True)
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        try:
            log = kubectl("-n", args.namespace, "logs", "deploy/kubelantern-gateway", "-c", "gateway")
        except subprocess.CalledProcessError:
            log = ""
        if f"version {version}" in log:
            print(f"Gateway is using version {version}.")
            return
        time.sleep(5)
    sys.exit(f"Gateway did not load version {version} within {args.timeout}s: "
             "check `make kb-status` and the gateway log for 'shared runbook' warnings.")


if __name__ == "__main__":
    main()
