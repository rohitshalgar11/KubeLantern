"""KubeLantern diagnosis evaluation.

Runs INSIDE the demo agent pod (piped via `make eval`), so it calls the real
gateway with the agent's own projected token, exactly like a real incident.
Each scenario is a realistic evidence bundle with a known correct category.

Output: one row per scenario + a score. Use it to compare models
(`make ai-model MODEL=qwen2.5:3b` then `make eval`) or code changes.
"""

import json
import time
import urllib.error
import urllib.request

GATEWAY = "http://kubelantern-gateway.kubelantern-ai.svc:8080/v1/diagnose"
TOKEN = "/var/run/secrets/kubelantern/token"
NS = "demo"


def scenario(name, expected, cause, family, exit_code, reason, log=None, events=None, deps=None,
             limits=None, last=None, workload=None):
    return expected, {
        "incident": {"id": f"{NS}-eval-{name}-INC000001", "namespace": NS,
                     "workload": f"Deployment/{workload or name}", "container": name, "cause": cause,
                     "cause_family": family, "exit_code": exit_code, "cause_history": [cause],
                     "failing_pods": {f"{name}-x": 3}, "affected_pods": {f"{name}-x": 3}},
        "evidence": {
            "failure": {"namespace": NS, "pod": f"{name}-x", "container": name, "reason": reason,
                        "exit_code": exit_code, "restarts": 3, "last_termination_reason": last},
            "container": {"image": f"example/{name}:1.0"},
            "resources": {name: {"requests": {"memory": "32Mi"}, "limits": limits or {"memory": "64Mi"}}},
            "owners": [{"kind": "Deployment", "name": name, "replicas": 1, "available_replicas": 0}],
            "events": events or [],
            "logs": {"current": log} if log else {"skipped": f"container never started ({reason})"},
            "pod": {"image_pull_secrets": []},
            "dependencies": deps or [],
        },
    }


SCENARIOS = [
    ("missing-service", *scenario(
        "orders-api", "dependency", "crash (exit 1)", "crash", 1, "CrashLoopBackOff",
        log="INFO starting\nFATAL: cannot connect to database at db:5432\n", last="Error",
        deps=[{"host": "db", "port": 5432, "scope": "namespace", "service": "db", "service_exists": False}])),
    ("oom", *scenario(
        "report-worker", "resources", "oom (exit 137)", "oom", 137, "OOMKilled",
        log="loading 2GB dataset into memory\n", limits={"memory": "64Mi"}, last="OOMKilled")),
    ("image-pull", *scenario(
        "web", "image", "image-pull", "image-pull", None, "ImagePullBackOff",
        events=[{"type": "Warning", "reason": "Failed", "count": 3,
                 "message": 'Failed to pull image "nginx:1.99-typo": not found'}])),
    ("missing-env", *scenario(
        "billing", "configuration", "crash (exit 1)", "crash", 1, "CrashLoopBackOff",
        log="FATAL: environment variable DATABASE_URL is not set\n", last="Error")),
    ("app-bug", *scenario(
        "pricing", "application-error", "crash (exit 2)", "crash", 2, "CrashLoopBackOff",
        log="panic: runtime error: index out of range [3] with length 3\n\ngoroutine 1 [running]:\n"
            "main.computePrice(...)\n\t/app/price.go:42\n", last="Error")),
    # -- harder: the obvious reading of the logs is wrong or incomplete ---------------
    ("wrong-port", *scenario(
        "cart", "dependency", "crash (exit 1)", "crash", 1, "CrashLoopBackOff",
        log="ERROR dial tcp redis:6380: connect: connection refused\n", last="Error",
        deps=[{"host": "redis", "port": 6380, "scope": "namespace", "service": "redis",
               "service_exists": True, "service_ports": [6379], "port_exposed": False,
               "ready_endpoints": 1, "selector": {"app": "redis"}}])),
    ("oom-misleading", *scenario(
        "indexer", "resources", "oom (exit 137)", "oom", 137, "OOMKilled",
        log="WARN connection reset by peer while flushing batch 412\n", limits={"memory": "128Mi"},
        last="OOMKilled")),
    ("liveness", *scenario(
        "search", "probe", "crash (exit 137 SIGKILL)", "crash", 137, "CrashLoopBackOff",
        log="INFO warming index cache (this takes ~90s)\n", last="Error",
        events=[{"type": "Warning", "reason": "Unhealthy", "count": 6,
                 "message": "Liveness probe failed: Get \"http://10.0.0.7:8080/healthz\": "
                            "context deadline exceeded"}])),
    ("broken-app-team", *scenario(
        "broken-app", "dependency", "crash (exit 1)", "crash", 1, "CrashLoopBackOff",
        log="FATAL: cannot connect to database at db:5432\n", last="Error",
        deps=[{"host": "db", "port": 5432, "scope": "namespace", "service": "db",
               "service_exists": False}])),
]


def call(body):
    with open(TOKEN) as fh:
        token = fh.read().strip()
    for _ in range(6):
        req = urllib.request.Request(GATEWAY, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json",
                                              "Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=400) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (429, 503):
                time.sleep(int(e.headers.get("Retry-After") or 15) + 1)
                continue
            return {"error": f"{e.code} {e.read()[:200]!r}"}
    return {"error": "gave up after retries"}


def main():
    print(f"{'scenario':<16} {'expected':<18} {'got':<18} {'conf':<7} {'tries':<6} {'secs':<6} "
          f"{'fixes':<6} runbooks")
    print("-" * 110)
    score = 0
    for name, expected, body in SCENARIOS:
        r = call(body)
        if "error" in r:
            print(f"{name:<16} {expected:<18} ERROR {r['error']}")
            continue
        d = r["diagnosis"]
        ok = d.get("category") == expected
        score += ok
        print(f"{name:<16} {expected:<18} {d.get('category', '?'):<18} {d.get('confidence', '?'):<7} "
              f"{r.get('attempts', 1):<6} {r.get('latency_seconds', 0):<6} "
              f"{len(r.get('corrections') or []):<6} "
              f"{', '.join(x['source'] for x in r.get('references') or []) or '-'}"
              f"{'' if ok else '   <-- wrong'}")
    print("-" * 110)
    print(f"Score: {score}/{len(SCENARIOS)} categories correct   (engine: {r.get('engine', '?')}, "
          f"model: {r.get('model', '?')})")
    print("'fixes' = corrections validation applied to the model's answer. Categories are mostly")
    print("decided by rules; fewer fixes and attempts = a better model.")


main()
