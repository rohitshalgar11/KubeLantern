"""Check which runbooks the knowledge base returns for known failures.

Each case is a small failure description (logs, pod message, events) and the
runbook a diagnosis should find for it. The search is exactly the one a real
diagnosis runs (`gateway.graph.search_runbooks`: rules, verified facts, query,
category filter).

Live (`make test-kb`), inside the gateway pod, against the real embeddings and
the index the gateway built from the mounted runbooks:

    kubectl -n kubelantern-ai exec -i deploy/kubelantern-gateway -- \\
        python -m gateway.kbcheck < tests/kb/cases.json

Cases are JSON: [{"name", "expect", "log"?, "message"?, "events"?, "cause"?,
"family"?, "exit_code"?, "reason"?, "deps"?}, ...]. Searched as namespace
"kb-check", so only shared runbooks can match.
"""

from __future__ import annotations

import json
import os
import sys

NAMESPACE = "kb-check"


def build_request(case: dict, namespace: str = NAMESPACE) -> dict:
    name = case["name"]
    family = case.get("family", "crash")
    exit_code = case.get("exit_code", 1 if family == "crash" else None)
    cause = case.get("cause") or (f"crash (exit {exit_code})" if family == "crash" else family)
    reason = case.get("reason", "CrashLoopBackOff")
    log = case.get("log")
    return {
        "incident": {"id": f"{namespace}-{name}-INC000001", "namespace": namespace,
                     "workload": f"Deployment/{name}", "container": name, "cause": cause,
                     "cause_family": family, "exit_code": exit_code, "cause_history": [cause],
                     "failing_pods": {f"{name}-x": 3}, "affected_pods": {f"{name}-x": 3}},
        "evidence": {
            "failure": {"namespace": namespace, "pod": f"{name}-x", "container": name,
                        "reason": reason, "exit_code": exit_code, "restarts": 3,
                        "message": case.get("message")},
            "events": [{"type": "Warning", "reason": "Failed", "count": 3, "message": m}
                       for m in case.get("events") or []],
            "logs": {"previous": log} if log else {"skipped": "no logs"},
            "dependencies": case.get("deps") or [],
        },
    }


def check(knowledge, cases: list[dict], namespace: str = NAMESPACE) -> list[dict]:
    from gateway.graph import search_runbooks

    results = []
    for case in cases:
        refs = search_runbooks(knowledge, build_request(case, namespace))
        found = [r["source"].split("/", 1)[1] for r in refs]
        expect = case["expect"]
        results.append({"name": case["name"], "expect": expect, "found": found,
                        "rank": found.index(expect) + 1 if expect in found else None,
                        "score": refs[0]["score"] if refs else None})
    return results


def report(results: list[dict]) -> int:
    print(f"{'case':<30} {'expected runbook':<34} {'rank':<5} found (best first)")
    print("-" * 120)
    for r in results:
        rank = r["rank"] or "-"
        print(f"{r['name']:<30} {r['expect']:<34} {rank!s:<5} {', '.join(r['found']) or '(none)'}"
              f"{'' if r['rank'] else '   <-- missed'}")
    print("-" * 120)
    top1 = sum(r["rank"] == 1 for r in results)
    found = sum(r["rank"] is not None for r in results)
    print(f"Found: {found}/{len(results)}   first: {top1}/{len(results)}")
    return 0 if found == len(results) else 1


def main() -> None:
    from gateway.knowledge import KnowledgeBase, OllamaEmbedder, QdrantStore

    cases = json.load(sys.stdin)
    if not os.environ.get("QDRANT_URL"):
        sys.exit("QDRANT_URL is not set: runbook retrieval is off in this gateway")
    kb = KnowledgeBase(
        OllamaEmbedder(os.environ.get("OLLAMA_URL", "http://ollama:11434"),
                       os.environ.get("KUBELANTERN_EMBED_MODEL", "nomic-embed-text")),
        QdrantStore(os.environ["QDRANT_URL"]),
        min_score=float(os.environ.get("KUBELANTERN_RUNBOOK_MIN_SCORE", "0.35")),
    )
    sys.exit(report(check(kb, cases)))


if __name__ == "__main__":
    main()
