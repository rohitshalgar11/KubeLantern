"""Runbook knowledge base (Stage 7): chunking, validation, retrieval, and —
most importantly — namespace isolation of team runbooks."""

import hashlib
import json
import math
import re
import tempfile
from pathlib import Path

from gateway.core import Authenticator, Gateway, GatewayConfig
from gateway.graph import build_graph, run_diagnosis
from gateway.knowledge import (
    SHARED,
    Hit,
    InMemoryStore,
    KnowledgeBase,
    KnowledgeError,
    QdrantStore,
    Runbook,
    chunk_runbook,
    parse_markdown_runbook,
    validate_team_runbooks,
)


class HashEmbedder:
    """Deterministic bag-of-words embedder: texts sharing words are similar."""

    dim = 256

    def embed(self, texts, kind):
        out = []
        for t in texts:
            v = [0.0] * self.dim
            for w in re.findall(r"[a-z0-9]{3,}", t.lower()):
                v[int(hashlib.md5(w.encode()).hexdigest(), 16) % self.dim] += 1.0
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / n for x in v])
        return out


SECRET_PAYMENTS = {
    "name": "payments-db-unreachable",
    "title": "payments-api cannot reach its database",
    "category": "dependency",
    "content": "# Database\nOur db is Azure PostgreSQL pg-payments-prod.internal via ExternalName "
               "Service db. If db is missing apply k8s/db-service.yaml. Escalate #payments-oncall.",
}
DEMO_RB = {
    "name": "broken-app-db",
    "title": "broken-app database dependency",
    "category": "dependency",
    "workloads": ["broken-app"],
    "content": "# Database\nbroken-app needs Service db on port 5432. Deploy it with "
               "kubectl apply -f examples/demo-db.yaml. Owner: #demo-team.",
}
QUERY = "dependency failure cannot connect to database db 5432 Service db missing"


def shared_dir():
    d = Path(tempfile.mkdtemp())
    (d / "missing-service.md").write_text(
        "---\ntitle: Missing Service for a dependency\ncategory: dependency\n---\n"
        "# Symptoms\nApp logs cannot connect to host:port and Service does not exist.\n"
        "# Fix\nCreate the Service or correct the host the app uses.\n")
    (d / "oomkilled.md").write_text(
        "---\ntitle: OOMKilled containers\ncategory: resources\n---\n"
        "# Fix\nRaise the memory limit or reduce memory use. Check exit 137.\n")
    return d


def kb():
    k = KnowledgeBase(HashEmbedder(), InMemoryStore(), min_score=0.05, relative_margin=1.0)
    k.load_shared(shared_dir())
    return k


# -- parsing & chunking -----------------------------------------------------------

def test_parse_markdown_front_matter():
    rb = parse_markdown_runbook(shared_dir() / "oomkilled.md")
    assert (rb.namespace, rb.name, rb.title, rb.category) == (SHARED, "oomkilled", "OOMKilled containers",
                                                              "resources")
    assert rb.source == "shared/oomkilled" and rb.content.startswith("# Fix")


def test_chunks_split_on_headings_and_size_and_keep_title():
    body = "# A\n" + "x " * 50 + "\n# B\n" + ("line of text\n" * 300)
    chunks = chunk_runbook(Runbook("demo", "r", "My Title", body), size=500)
    assert len(chunks) > 3
    assert all(c.startswith("My Title\n\n") for c in chunks)
    assert all(len(c) <= 500 + len("My Title\n\n") for c in chunks)


# -- validation of team payloads ------------------------------------------------------

def test_validate_rejects_bad_payloads():
    good = dict(DEMO_RB)
    assert validate_team_runbooks([good])[0]["name"] == "broken-app-db"
    for bad, msg in [
        ("not a list", "must be a list"),
        ([dict(good, name="Bad Name")], "invalid runbook name"),
        ([good, good], "duplicate"),
        ([dict(good, title="x")], "title"),
        ([dict(good, content="a" * (16 * 1024 + 1))], "content"),
        ([dict(good, workloads="broken-app")], "workloads"),
        ([dict(good, name=f"r{i}") for i in range(51)], "at most 50"),
    ]:
        try:
            validate_team_runbooks(bad)
            raise AssertionError(f"accepted: {msg}")
        except KnowledgeError as e:
            assert msg in str(e), (msg, str(e))


# -- retrieval & isolation ---------------------------------------------------------------

def test_shared_runbooks_retrieved_for_any_namespace():
    refs = kb().retrieve(QUERY, "demo", "dependency")
    assert refs[0]["source"] == "shared/missing-service"


def test_team_runbook_never_retrieved_by_another_namespace():
    k = kb()
    k.sync_namespace("payments", [SECRET_PAYMENTS])
    # A query that matches the payments runbook almost word for word:
    q = SECRET_PAYMENTS["content"]
    for ns in ("demo", "orders"):
        refs = k.retrieve(q, ns, "dependency", limit=10)
        assert all(not r["source"].startswith("payments/") for r in refs), ns
        assert all("pg-payments-prod" not in r["text"] for r in refs), ns
    own = k.retrieve(q, "payments", "dependency")
    assert own[0]["source"] == "payments/payments-db-unreachable"


def test_store_bug_cannot_leak_other_namespace():
    """Defence in depth: even if the vector store ignored the filter, the
    knowledge base drops foreign chunks."""

    class LeakyStore(InMemoryStore):
        def search(self, vector, namespaces, limit):
            return [Hit(0.99, {"namespace": "payments", "source": "payments/x", "title": "t",
                               "text": "secret", "workloads": []})]

    k = KnowledgeBase(HashEmbedder(), LeakyStore(), min_score=0.0)
    assert k.retrieve("anything", "demo") == []


def test_workload_scoped_runbook_only_for_that_workload():
    k = kb()
    k.sync_namespace("demo", [DEMO_RB])
    hit = k.retrieve(QUERY, "demo", "dependency", workload="broken-app")
    miss = k.retrieve(QUERY, "demo", "dependency", workload="other-app")
    assert "demo/broken-app-db" in [r["source"] for r in hit]
    assert "demo/broken-app-db" not in [r["source"] for r in miss]


def test_sync_replaces_namespace_set_and_cannot_touch_shared():
    k = kb()
    k.sync_namespace("demo", [DEMO_RB])
    k.sync_namespace("demo", [])  # team deleted their runbooks
    assert all(not r["source"].startswith("demo/") for r in k.retrieve(QUERY, "demo", limit=10))
    assert k.retrieve(QUERY, "demo")  # shared still there
    try:
        k.sync_namespace(SHARED, [DEMO_RB])
        raise AssertionError("team sync overwrote shared runbooks")
    except KnowledgeError:
        pass


def test_secrets_in_team_runbooks_are_redacted_at_ingestion():
    k = kb()
    leaky = dict(DEMO_RB, content="# DB\nconnect with password=hunter2 to db:5432 Service db")
    k.sync_namespace("demo", [leaky])
    texts = " ".join(r["text"] for r in k.retrieve(QUERY, "demo", limit=10))
    assert "hunter2" not in texts and "password=[REDACTED]" in texts


# -- gateway sync endpoint: namespace comes from the token ----------------------------------

TOKENS = {"demo-agent": "system:serviceaccount:demo:kubelantern-agent",
          "payments-agent": "system:serviceaccount:payments:kubelantern-agent",
          "demo-default": "system:serviceaccount:demo:default"}


class Reviewer:
    def review(self, token, audience):
        u = TOKENS.get(token)
        return (True, u, None) if u else (False, None, "bad")


class LLM:
    model = "fake"

    def __init__(self):
        self.prompts = []

    def chat(self, system, user, schema):
        self.prompts.append(user)
        return json.dumps({"summary": "db missing", "category": "dependency",
                           "probable_cause": "Service db does not exist", "confidence": "high",
                           "evidence": [], "next_steps": ["kubectl apply -f examples/demo-db.yaml"],
                           "suggested_fix": "Deploy Service db"})


def gateway(knowledge=None, llm=None):
    return Gateway(GatewayConfig(), Authenticator(Reviewer(), "kubelantern-gateway"), llm or LLM(),
                   clock=lambda: 1000.0, knowledge=knowledge)


def sync(gw, token, body):
    return gw.sync_runbooks(f"Bearer {token}", json.dumps(body).encode())


def test_sync_endpoint_stamps_namespace_from_token():
    k = kb()
    gw = gateway(k)
    # demo agent tries to plant a runbook "for payments" — body namespace is ignored
    status, out = sync(gw, "demo-agent", {"namespace": "payments", "runbooks": [SECRET_PAYMENTS]})
    assert status == 200 and out["namespace"] == "demo"
    assert all(not r["source"].startswith("payments/")
               for r in k.retrieve(SECRET_PAYMENTS["content"], "payments", limit=10))


def test_sync_endpoint_auth_and_validation():
    gw = gateway(kb())
    assert gw.sync_runbooks(None, b"{}")[0] == 401
    assert sync(gw, "demo-default", {"runbooks": []})[0] == 403
    assert sync(gw, "demo-agent", {"runbooks": [{"name": "Bad Name"}]})[0] == 400
    assert gateway(None).sync_runbooks("Bearer demo-agent", b'{"runbooks": []}')[0] == 503


def test_sync_endpoint_rate_limited():
    gw = gateway(kb())
    codes = [sync(gw, "demo-agent", {"runbooks": []})[0] for _ in range(3)]
    assert codes == [200, 200, 429]


# -- graph integration ------------------------------------------------------------------------

def incident_request(ns="demo"):
    return {
        "incident": {"id": f"{ns}-broken-app-INCabc123", "namespace": ns,
                     "workload": "Deployment/broken-app", "container": "broken-app",
                     "cause": "crash (exit 1)", "cause_family": "crash", "exit_code": 1},
        "evidence": {
            "failure": {"namespace": ns, "reason": "CrashLoopBackOff", "exit_code": 1},
            "logs": {"current": "FATAL: cannot connect to database at db:5432\n"},
            "dependencies": [{"host": "db", "port": 5432, "scope": "namespace", "service": "db",
                              "service_exists": False}],
        },
    }


def test_graph_puts_runbooks_in_prompt_and_cites_them():
    k = kb()
    k.sync_namespace("demo", [DEMO_RB])
    llm = LLM()
    out = run_diagnosis(build_graph(llm, prefer_langgraph=False, knowledge=k), incident_request())
    sources = [r["source"] for r in out["references"]]
    assert "demo/broken-app-db" in sources and "shared/missing-service" in sources
    assert "<<<RUNBOOK demo/broken-app-db" in llm.prompts[0]
    assert "examples/demo-db.yaml" in llm.prompts[0]
    assert "retrieve" in [t["node"] for t in out["trace"]]


def test_diagnose_response_never_cites_other_namespace():
    k = kb()
    k.sync_namespace("payments", [SECRET_PAYMENTS])
    gw = gateway(k)
    status, out = gw.diagnose("Bearer demo-agent", json.dumps(incident_request("demo")).encode())
    assert status == 200
    assert out["references"] and all(not r["source"].startswith("payments/") for r in out["references"])
    status, out = gw.diagnose("Bearer payments-agent",
                              json.dumps(incident_request("payments")).encode())
    assert "payments/payments-db-unreachable" in [r["source"] for r in out["references"]]


def test_knowledge_base_down_still_diagnoses():
    class Broken:
        def retrieve(self, *a, **kw):
            raise ConnectionError("qdrant down")

    out = run_diagnosis(build_graph(LLM(), prefer_langgraph=False, knowledge=Broken()), incident_request())
    assert out["diagnosis"]["category"] == "dependency" and out["references"] == []
    assert any("runbooks unavailable" in n for n in out["notes"])


# -- Qdrant adapter request shapes ---------------------------------------------------------------

def test_qdrant_requests_filter_by_namespace():
    calls = []

    class Q(QdrantStore):
        def _req(self, method, path, body=None):
            calls.append((method, path, body))
            if path.endswith("/points/search"):
                return {"result": [{"score": 0.9, "payload": {"namespace": "*", "source": "shared/x"}}]}
            return {}

    q = Q("http://qdrant:6333")
    q._ready = True
    q.replace_namespace("demo", [("id1", [0.1, 0.2], {"namespace": "demo"})])
    hits = q.search([0.1, 0.2], ["*", "demo"], 5)
    upsert = next(i for i, c in enumerate(calls) if c[0] == "PUT" and c[1].endswith("/points?wait=true"))
    delete = next(i for i, c in enumerate(calls) if c[1].endswith("/points/delete?wait=true"))
    assert upsert < delete                     # new points first: never an empty namespace
    assert calls[delete][2] == {"filter": {
        "must": [{"key": "namespace", "match": {"value": "demo"}}],
        "must_not": [{"has_id": ["id1"]}]}}
    search = next(c for c in calls if c[1].endswith("/points/search"))
    assert search[2]["filter"] == {"must": [{"key": "namespace", "match": {"any": ["*", "demo"]}}]}
    assert hits[0].payload["source"] == "shared/x"


def test_strict_category_drops_off_topic_runbooks():
    k = kb()
    loose = [r["source"] for r in k.retrieve(QUERY + " memory limit", "demo", "dependency", limit=5)]
    strict = [r["source"] for r in k.retrieve(QUERY + " memory limit", "demo", "dependency", limit=5,
                                              strict_category=True)]
    assert "shared/oomkilled" in loose and "shared/oomkilled" not in strict
    assert strict[0] == "shared/missing-service"


def test_relative_margin_drops_also_rans():
    k = KnowledgeBase(HashEmbedder(), InMemoryStore(), min_score=0.0, relative_margin=0.05)
    k.load_shared(shared_dir())
    refs = k.retrieve("raise the memory limit exit 137 oomkilled reduce memory use", "demo", limit=5)
    assert [r["source"] for r in refs] == ["shared/oomkilled"]
