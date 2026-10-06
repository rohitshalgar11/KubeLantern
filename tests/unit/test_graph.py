"""Diagnosis graph tests (Stage 6).

The key regression replays the exact answer qwen2.5:1.5b gave on the live
cluster in Stage 5 and checks the graph turns it into a grounded diagnosis.
"""

import json

from gateway.graph import MAX_ANALYZE_ATTEMPTS, build_graph, run_diagnosis
from gateway.rules import classify, fallback_fields, verified_facts

# Verbatim from the Stage 5 live run
STAGE5_ANSWER = json.dumps({
    "summary": "The most likely root cause is that the container is unable to connect to the database "
               "at the specified endpoint (db:5432). This is indicated by the 'FATAL: cannot connect to "
               "database at db:5432' message in the logs.",
    "category": "unknown",
    "probable_cause": "Network connectivity issue",
    "confidence": "low",
    "evidence": [
        "Logs showing the error message 'FATAL: cannot connect to database at db:5432'",
        "Container image 'busybox:1.36' is present on the machine and can be accessed by the pod",
        "Container image 'busybox:1.36' is present on the machine and can be accessed by the pod",
    ],
    "next_steps": ["Verify network connectivity to the database at db:5432.",
                   "Check if the pod has the necessary credentials or environment variables."],
    "suggested_fix": "Ensure the pod has the correct credentials or environment variables to connect "
                     "to the database at db:5432.",
})

GOOD_ANSWER = json.dumps({
    "summary": "broken-app cannot start because its database Service 'db' does not exist.",
    "category": "dependency",
    "probable_cause": "Service 'db' is missing in namespace demo, so db:5432 is unreachable.",
    "confidence": "high",
    "evidence": ["FATAL: cannot connect to database at db:5432"],
    "next_steps": ["kubectl -n demo get svc"],
    "suggested_fix": "Deploy the db Service in demo or point the app at the right host.",
})


class ScriptedLLM:
    model = "fake"

    def __init__(self, *replies):
        self.replies = list(replies)
        self.prompts = []

    def chat(self, system, user, schema):
        self.prompts.append(user)
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


def request(cause="crash (exit 1)", family="crash", exit_code=1, reason="CrashLoopBackOff",
            log="FATAL: cannot connect to database at db:5432\n", deps=None, events=None,
            limits=None, last=None, image_pull_secrets=None):
    return {
        "incident": {"id": "demo-broken-app-INCabc123", "namespace": "demo",
                     "workload": "Deployment/broken-app", "container": "broken-app",
                     "cause": cause, "cause_family": family, "exit_code": exit_code,
                     "cause_history": [cause], "failing_pods": {"p": 1}, "affected_pods": {"p": 1}},
        "evidence": {
            "failure": {"namespace": "demo", "reason": reason, "exit_code": exit_code, "restarts": 2,
                        "last_termination_reason": last},
            "container": {"image": "busybox:1.36"},
            "resources": {"broken-app": {"requests": {}, "limits": limits or {"memory": "32Mi"}}},
            "owners": [{"kind": "Deployment", "name": "broken-app", "replicas": 1, "available_replicas": 0}],
            "services": [{"name": "broken-app"}],
            "events": events or [],
            "logs": {"current": log} if log is not None else {"skipped": "container never started"},
            "pod": {"image_pull_secrets": image_pull_secrets or []},
            "dependencies": deps if deps is not None else
                [{"host": "db", "port": 5432, "scope": "namespace", "service": "db", "service_exists": False}],
        },
    }


# -- rules ------------------------------------------------------------------------

def test_classify_missing_service_is_dependency_high():
    c = classify(request())
    assert c.category == "dependency" and c.confidence == "high"
    assert "Service 'db' not found" in c.signals


def test_classify_log_pattern_without_checks_is_medium():
    c = classify(request(deps=[], log="dial tcp 10.0.0.5:6379: connect: connection refused"))
    assert (c.category, c.confidence) == ("dependency", "medium")


def test_classify_network_timeouts_are_dependency_medium():
    # regression (live run): "wget: download timed out" was classified as an app error
    for line in ["wget: download timed out",
                 "curl: (28) Connection timed out after 5001 milliseconds",
                 'Get "http://api:8080/": context deadline exceeded',
                 "requests.exceptions.ReadTimeout: read timed out"]:
        c = classify(request(deps=[], log=line))
        assert (c.category, c.confidence) == ("dependency", "medium"), line


def test_timeout_words_alone_are_not_dependency():
    for line in ["job finished, waiting for timeout=30", "set timeout to 5s"]:
        assert classify(request(deps=[], log=line)).category != "dependency", line


def test_classify_oom_image_probe_permissions_config():
    assert classify(request(cause="oom (exit 137)", family="oom", deps=[])).category == "resources"
    assert classify(request(cause="image-pull", family="image-pull", deps=[], log=None)).category == "image"
    probe = request(deps=[], log="ok", events=[{"type": "Warning", "message": "Liveness probe failed: 503"}])
    assert classify(probe).category == "probe"
    assert classify(request(deps=[], log="open /data/x: permission denied")).category == "permissions"
    assert classify(request(deps=[], log="FATAL: env DATABASE_URL not set")).category == "configuration"


def test_classify_crash_signatures_are_application_error_medium():
    for log in ["panic: runtime error: index out of range [3] with length 3\n\ngoroutine 1 [running]:",
                "Traceback (most recent call last):\n  File \"app.py\", line 3\nKeyboardInterrupt",
                "Exception in thread \"main\" java.lang.NullPointerException\n\tat App.main(App.java:5)",
                "Segmentation fault (core dumped)"]:
        c = classify(request(deps=[], log=log))
        assert (c.category, c.confidence) == ("application-error", "medium"), log


def test_classify_unexplained_exit_is_application_error_low():
    c = classify(request(deps=[], log="shutting down after fatal condition"))
    assert (c.category, c.confidence) == ("application-error", "low")


def test_verified_facts_for_missing_service():
    facts = verified_facts(request())
    assert any("no Service named 'db' exists in namespace demo" in f for f in facts)
    assert "The container exited with code 1." in facts
    assert "Deployment broken-app: 0/1 replicas available." in facts


def test_verified_facts_for_unhealthy_service():
    deps = [{"host": "db", "port": 5432, "scope": "namespace", "service": "db", "service_exists": True,
             "service_ports": [3306], "port_exposed": False, "ready_endpoints": 0, "selector": {"app": "db"}}]
    facts = " ".join(verified_facts(request(deps=deps)))
    assert "does not expose port 5432" in facts and "0 ready endpoints" in facts


def test_verified_facts_oom_and_image():
    oom = verified_facts(request(cause="oom (exit 137)", family="oom", exit_code=137, reason="OOMKilled",
                                 deps=[], limits={"memory": "32Mi"}))
    assert any("OOMKilled" in f for f in oom) and "Memory limit: 32Mi." in oom
    img = verified_facts(request(cause="image-pull", family="image-pull", exit_code=None, deps=[], log=None,
                                 events=[{"type": "Warning", "message": 'failed to pull "nginx:nope": not found'}]))
    assert any("Registry error" in f for f in img) and "The pod has no imagePullSecrets." in img


# -- the graph -----------------------------------------------------------------------

def test_stage5_answer_is_corrected():
    """The live Stage 5 answer must come out grounded: right category, no dupes, no credentials fix."""
    llm = ScriptedLLM(STAGE5_ANSWER)  # model keeps giving the same weak answer
    out = run_diagnosis(build_graph(llm, prefer_langgraph=False), request())
    d = out["diagnosis"]

    assert out["attempts"] == MAX_ANALYZE_ATTEMPTS  # validation rejected it and retried once
    assert "Service 'db' does not exist" in llm.prompts[1]  # feedback reached the model
    assert d["category"] == "dependency"
    assert d["confidence"] in ("medium", "high")
    assert "Service 'db' does not exist in namespace demo" in d["probable_cause"]
    assert "credential" not in d["suggested_fix"].lower() and "db" in d["suggested_fix"]
    assert len(d["evidence"]) == len({e.lower() for e in d["evidence"]})  # no duplicates
    assert d["evidence"][0].startswith("The application tries to reach db:5432")
    assert out["corrections"]


def test_good_answer_passes_first_time_unchanged():
    llm = ScriptedLLM(GOOD_ANSWER)
    out = run_diagnosis(build_graph(llm, prefer_langgraph=False), request())
    d = out["diagnosis"]
    assert out["attempts"] == 1 and len(llm.prompts) == 1
    assert d["category"] == "dependency" and d["confidence"] == "high"
    assert d["probable_cause"] == json.loads(GOOD_ANSWER)["probable_cause"]
    assert [c for c in out["corrections"] if "replaced" in c or "category" in c] == []


def test_retry_succeeds_on_second_attempt():
    llm = ScriptedLLM(STAGE5_ANSWER, GOOD_ANSWER)
    out = run_diagnosis(build_graph(llm, prefer_langgraph=False), request())
    assert out["attempts"] == 2
    assert out["diagnosis"]["probable_cause"] == json.loads(GOOD_ANSWER)["probable_cause"]


def test_unparseable_model_output_falls_back_to_rules():
    llm = ScriptedLLM("I think something is wrong with the database maybe")
    out = run_diagnosis(build_graph(llm, prefer_langgraph=False), request())
    d = out["diagnosis"]
    assert d["structured"] and d["category"] == "dependency"
    assert "Service 'db' does not exist" in d["probable_cause"]
    assert any("rules" in c for c in out["corrections"])


def test_prompt_contains_classification_and_verified_facts():
    llm = ScriptedLLM(GOOD_ANSWER)
    run_diagnosis(build_graph(llm, prefer_langgraph=False), request())
    p = llm.prompts[0]
    assert "## Pre-classification (rules)" in p and "category: dependency (confidence high)" in p
    assert "## Verified facts" in p and "no Service named 'db'" in p


def test_ungrounded_evidence_dropped():
    answer = json.loads(GOOD_ANSWER)
    answer["evidence"] = ["FATAL: cannot connect to database at db:5432",
                          "The Kafka cluster in eu-west-3 is degraded"]
    out = run_diagnosis(build_graph(ScriptedLLM(json.dumps(answer)), prefer_langgraph=False), request())
    assert not any("Kafka" in e for e in out["diagnosis"]["evidence"])


def test_high_confidence_capped_without_facts():
    answer = dict(json.loads(GOOD_ANSWER), category="application-error", confidence="high",
                  probable_cause="index bug", suggested_fix="fix the code")
    out = run_diagnosis(build_graph(ScriptedLLM(json.dumps(answer)), prefer_langgraph=False),
                        request(deps=[], log="shutting down after fatal condition"))
    # exit-code fact exists, but nothing verifies the claimed bug -> capped
    assert out["diagnosis"]["category"] == "application-error"


def test_trace_records_every_node_in_order():
    out = run_diagnosis(build_graph(ScriptedLLM(STAGE5_ANSWER, GOOD_ANSWER), prefer_langgraph=False),
                        request())
    assert [t["node"] for t in out["trace"]] == [
        "classify", "gather_facts", "retrieve", "analyze", "validate", "analyze", "validate", "finalize"]


def test_fallback_fields_cover_categories():
    from gateway.rules import Classification
    for cat in ("dependency", "resources", "image", "unknown"):
        fb = fallback_fields(request(), Classification(cat, "high"), [])
        assert fb["probable_cause"] and fb["suggested_fix"] and fb["next_steps"]


def test_langgraph_build_matches_builtin_when_installed():
    try:
        import langgraph  # noqa: F401
    except ImportError:
        return  # not installed in this environment; CI and the gateway image have it
    a = run_diagnosis(build_graph(ScriptedLLM(STAGE5_ANSWER), prefer_langgraph=True), request())
    b = run_diagnosis(build_graph(ScriptedLLM(STAGE5_ANSWER), prefer_langgraph=False), request())
    assert a["engine"] == "langgraph"
    assert a["diagnosis"] == b["diagnosis"] and a["attempts"] == b["attempts"]
    assert [t["node"] for t in a["trace"]] == [t["node"] for t in b["trace"]]


def test_agent_display_shows_attempts_and_corrections():
    from agent.diagnosis.client import format_diagnosis

    out = run_diagnosis(build_graph(ScriptedLLM(STAGE5_ANSWER), prefer_langgraph=False), request())
    text = format_diagnosis({"id": "demo-broken-app-INCabc123"},
                            {"model": "qwen2.5:1.5b", "latency_seconds": 61.2, **out})
    assert "Model     : qwen2.5:1.5b (61.2s, 2 attempts)" in text
    assert "Category  : dependency" in text
    assert "Validated : category unknown -> dependency (rules)" in text



def test_model_disputing_medium_rule_gets_feedback_then_loses_with_low_confidence():
    """Stage 7 live regression: app-bug (Go panic) came back as 'configuration'."""
    answer = json.dumps({"summary": "config issue", "category": "configuration",
                         "probable_cause": "misconfigured", "confidence": "medium", "evidence": [],
                         "next_steps": ["check config"], "suggested_fix": "fix the config"})
    llm = ScriptedLLM(answer)
    req = request(deps=[], exit_code=2, cause="crash (exit 2)",
                  log="panic: runtime error: index out of range [3] with length 3\n\ngoroutine 1 [running]:")
    out = run_diagnosis(build_graph(llm, prefer_langgraph=False), req)
    assert out["attempts"] == 2 and "application-error" in llm.prompts[1]
    assert out["diagnosis"]["category"] == "application-error"
    assert out["diagnosis"]["confidence"] == "low"
    assert "category configuration -> application-error (rules; disputed)" in out["corrections"]


def test_model_agreeing_after_feedback_keeps_its_answer():
    bad = json.dumps({"summary": "x", "category": "configuration", "probable_cause": "c",
                      "confidence": "medium", "evidence": [], "next_steps": ["s"], "suggested_fix": "f"})
    good = json.dumps({"summary": "index bug", "category": "application-error",
                       "probable_cause": "computePrice indexes past the end of a 3-item slice",
                       "confidence": "medium", "evidence": [], "next_steps": ["roll back"],
                       "suggested_fix": "bounds-check the slice in computePrice"})
    req = request(deps=[], exit_code=2, cause="crash (exit 2)", log="panic: runtime error: index out of range")
    out = run_diagnosis(build_graph(ScriptedLLM(bad, good), prefer_langgraph=False), req)
    d = out["diagnosis"]
    assert d["category"] == "application-error" and d["confidence"] == "medium"
    assert "computePrice" in d["probable_cause"]


# -- Stage 7 live regression: team runbook retrieved but model gave a generic fix --

def test_team_runbook_actions_added_when_model_ignores_them():
    from pathlib import Path

    import yaml

    from agent.diagnosis.runbooks import to_payload
    from gateway.knowledge import InMemoryStore, KnowledgeBase
    from tests.unit.test_knowledge import HashEmbedder

    root = Path(__file__).resolve().parents[2]
    k = KnowledgeBase(HashEmbedder(), InMemoryStore(), min_score=0.05)
    k.load_shared(root / "runbooks/shared")
    team = yaml.safe_load((root / "examples/runbooks/demo-broken-app.yaml").read_text())
    k.sync_namespace("demo", to_payload([team]))
    live_answer = json.dumps({  # verbatim shape of the live qwen2.5:1.5b answer
        "summary": "The application is trying to connect to a database that is not available.",
        "category": "dependency", "confidence": "high",
        "probable_cause": "Service 'db' not found",
        "evidence": ["The application tries to reach db:5432, but no Service named 'db' exists in namespace demo.",
                     "The application logs 'cannot connect to database at db:5432'",
                     "No Service named 'db' exists in namespace demo"],
        "next_steps": ["Verify the existence of the database service in the namespace."],
        "suggested_fix": "Ensure that the Service 'db' exists in the namespace and that the application "
                         "is correctly configured to connect to it.",
    })
    out = run_diagnosis(build_graph(ScriptedLLM(live_answer), prefer_langgraph=False, knowledge=k), request())
    d = out["diagnosis"]
    assert d["next_steps"][0] == "kubectl apply -f examples/demo-db.yaml   (runbook demo/broken-app-database)"
    assert d["escalation"] == "demo team — Slack #demo-team-oncall"
    assert "added team runbook steps" in out["corrections"]
    # near-duplicate of the verified fact is not listed twice
    assert sum("No Service named 'db'" in e or "no Service named 'db'" in e for e in d["evidence"]) == 1


def test_runbook_steps_not_duplicated_when_model_used_them():
    from gateway.graph import runbook_actions

    refs = [{"source": "demo/rb", "text": "Fix it:\n\n    kubectl apply -f examples/demo-db.yaml\n\nOwner: #team"}]
    steps, esc = runbook_actions(refs)
    assert steps == [{"command": "kubectl apply -f examples/demo-db.yaml", "source": "demo/rb"}]
    assert esc == "#team"
    answer = json.dumps(dict(json.loads(GOOD_ANSWER),
                             next_steps=["kubectl apply -f examples/demo-db.yaml"]))

    class K:
        def retrieve(self, *a, **kw):
            return [dict(refs[0], title="t", score=0.9, category="dependency")]

    out = run_diagnosis(build_graph(ScriptedLLM(answer), prefer_langgraph=False, knowledge=K()), request())
    assert out["diagnosis"]["next_steps"] == ["kubectl apply -f examples/demo-db.yaml"]


def test_shared_runbook_placeholders_are_not_turned_into_steps():
    from gateway.graph import runbook_actions

    steps, _ = runbook_actions([{"source": "shared/oomkilled",
                                 "text": "kubectl -n <ns> top pod\nkubectl -n demo get pods"}])
    assert steps == []
