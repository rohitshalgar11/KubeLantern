"""Shared knowledge base: the built-in runbook library, loading from mounted
ConfigMap folders with hot reload, and the runbook search used by diagnoses."""

import json
from pathlib import Path

from gateway.core import CATEGORIES
from gateway.graph import retrieval_query, runbook_actions
from gateway.kbcheck import build_request, check
from gateway.knowledge import (
    MAX_CONTENT_BYTES,
    SHARED,
    InMemoryStore,
    KnowledgeBase,
    scan_shared,
)
from gateway.rules import classify
from tests.unit.test_knowledge import HashEmbedder

ROOT = Path(__file__).resolve().parents[2]
LIBRARY = ROOT / "charts/kubelantern-ai/runbooks"
CASES = json.loads((ROOT / "tests/kb/cases.json").read_text())
# Reference runbooks without a single log signature of their own.
GENERIC = {"container-exit-codes", "crashloop-after-rollout"}


def rb(title="A runbook", category=None, body="# Symptoms\nboom\n# Fix\nfix it\n"):
    front = f"---\ntitle: {title}\n" + (f"category: {category}\n" if category else "") + "---\n"
    return front + body


class CountingEmbedder(HashEmbedder):
    def __init__(self):
        self.documents = 0

    def embed(self, texts, kind):
        if kind == "document":
            self.documents += len(texts)
        return super().embed(texts, kind)


# -- the built-in library -------------------------------------------------------------

def test_library_is_valid_and_complete():
    scan = scan_shared(LIBRARY)
    assert scan.skipped == [] and scan.overridden == []
    assert len(scan.runbooks) >= 40
    for r in scan.runbooks:
        assert "# Symptoms" in r.content, r.name
        assert "# Fix" in r.content or r.name in GENERIC, r.name
        assert r.category is None or r.category in CATEGORIES, r.name
        assert len(r.content.encode()) < MAX_CONTENT_BYTES, r.name


def test_library_fits_in_one_configmap():
    total = sum(p.stat().st_size for p in LIBRARY.glob("*.md"))
    assert total < 900 * 1024     # ConfigMaps are limited to 1 MiB


def test_library_has_no_commands_or_contacts_that_would_reach_the_answer():
    kb = KnowledgeBase(HashEmbedder(), InMemoryStore())
    kb.load_shared(LIBRARY)
    for _, payload in kb.store.points.values():
        assert payload["escalation"] is None, payload["name"]


def test_every_runbook_has_a_test_case():
    names = {p.stem for p in LIBRARY.glob("*.md")}
    expected = {c["expect"] for c in CASES}
    assert expected <= names, expected - names
    assert names - expected <= GENERIC, names - expected - GENERIC


def test_category_filter_never_hides_the_expected_runbook():
    """When the rules are confident, runbooks of another category are dropped.
    A runbook must carry the category the rules give its own symptoms (or none)."""
    categories = {r.name: r.category for r in scan_shared(LIBRARY).runbooks}
    for case in CASES:
        cls = classify(build_request(case))
        expected_cat = categories[case["expect"]]
        if cls.confidence in ("high", "medium") and cls.category != "unknown" and expected_cat:
            assert expected_cat == cls.category, (case["name"], cls.category, expected_cat)


def test_search_finds_the_expected_runbook_for_most_cases():
    # A crude bag-of-words embedder; `make test-kb` checks the real embeddings live.
    kb = KnowledgeBase(HashEmbedder(), InMemoryStore(), min_score=0.05)
    kb.load_shared(LIBRARY)
    results = check(kb, CASES)
    found = sum(r["rank"] is not None for r in results)
    assert found >= 0.85 * len(results), [r for r in results if r["rank"] is None]


# -- loading from mounted ConfigMaps ------------------------------------------------------

def test_folders_load_in_order_and_later_ones_override(tmp_path):
    (tmp_path / "00-builtin").mkdir()
    (tmp_path / "20-platform").mkdir()
    (tmp_path / "00-builtin" / "redis-errors.md").write_text(rb("Built-in Redis"))
    (tmp_path / "00-builtin" / "oomkilled.md").write_text(rb("OOM", "resources"))
    (tmp_path / "20-platform" / "redis-errors.md").write_text(rb("Our Redis"))
    scan = scan_shared(tmp_path)
    titles = {r.name: r.title for r in scan.runbooks}
    assert titles == {"redis-errors": "Our Redis", "oomkilled": "OOM"}
    assert scan.overridden == ["redis-errors"]
    assert all(r.namespace == SHARED for r in scan.runbooks)


def test_kubelet_data_folders_are_not_read_twice(tmp_path):
    cm = tmp_path / "00-builtin"
    data = cm / "..2026_10_07_10_00_00.123"
    data.mkdir(parents=True)
    (data / "oomkilled.md").write_text(rb("OOM"))
    (cm / "..data").symlink_to(data.name)
    (cm / "oomkilled.md").symlink_to("..data/oomkilled.md")
    scan = scan_shared(tmp_path)
    assert [r.name for r in scan.runbooks] == ["oomkilled"] and scan.overridden == []


def test_bad_files_are_skipped_not_fatal(tmp_path):
    d = tmp_path / "10-custom"
    d.mkdir()
    (d / "good.md").write_text(rb("Good one"))
    (d / "Bad_Name.md").write_text(rb("Bad name"))
    (d / "huge.md").write_text(rb("Huge", body="x" * (MAX_CONTENT_BYTES + 1)))
    (d / "empty.md").write_text("")
    (d / "odd-category.md").write_text(rb("Odd", category="databases"))
    (d / "notes.txt").write_text("ignored")
    scan = scan_shared(tmp_path)
    assert sorted(r.name for r in scan.runbooks) == ["good", "odd-category"]
    assert next(r for r in scan.runbooks if r.name == "odd-category").category is None
    reasons = " ".join(scan.skipped)
    assert "Bad_Name.md" in reasons and "huge.md" in reasons and "empty.md" in reasons
    assert "unknown category 'databases'" in reasons


def test_secrets_in_shared_runbooks_are_redacted(tmp_path):
    (tmp_path / "db.md").write_text(rb("Database", body="# Symptoms\npassword=hunter2 fails\n# Fix\nx\n"))
    [r] = scan_shared(tmp_path).runbooks
    assert "hunter2" not in r.content


def test_missing_folder_means_no_shared_runbooks(tmp_path):
    assert scan_shared(tmp_path / "nope").runbooks == []


def test_reload_only_when_something_changed_and_only_new_chunks_are_embedded(tmp_path):
    d = tmp_path / "20-platform"
    d.mkdir()
    (d / "a.md").write_text(rb("Runbook A", body="# Symptoms\nalpha failure\n# Fix\nfix alpha\n"))
    (d / "b.md").write_text(rb("Runbook B", body="# Symptoms\nbeta failure\n# Fix\nfix beta\n"))
    emb = CountingEmbedder()
    kb = KnowledgeBase(emb, InMemoryStore(), min_score=0.05)
    assert kb.load_shared(tmp_path) == 6      # 2 sections + 1 symptom line, per runbook
    first = emb.documents
    assert kb.load_shared(tmp_path, force=False) is None            # unchanged: nothing to do
    assert emb.documents == first

    (d / "b.md").write_text(rb("Runbook B", body="# Symptoms\nbeta failure\n# Fix\nnew fix\n"))
    (d / "a.md").unlink()
    (d / "c.md").write_text(rb("Runbook C", body="# Symptoms\ngamma\n# Fix\nfix gamma\n"))
    assert kb.load_shared(tmp_path, force=False) == 5
    assert emb.documents == first + 3          # b's changed Fix + c's two sections
    #                                            (c's symptom "gamma" is too short to index alone)
    names = {p["name"] for _, p in kb.store.points.values()}
    assert names == {"b", "c"}                 # a is gone
    assert kb.shared_names == ["b", "c"]


def test_replacing_a_namespace_never_leaves_it_empty():
    store = InMemoryStore()
    store.replace_namespace("*", [("1", [1.0], {"namespace": "*"}), ("2", [1.0], {"namespace": "*"})])
    store.replace_namespace("demo", [("9", [1.0], {"namespace": "demo"})])
    store.replace_namespace("*", [("2", [0.5], {"namespace": "*"}), ("3", [1.0], {"namespace": "*"})])
    assert sorted(store.points) == ["2", "3", "9"]
    assert store.points["2"][0] == [0.5]


# -- the search query and runbook actions -------------------------------------------------

def test_query_uses_the_error_line_above_a_stack_trace_and_pod_messages():
    req = build_request({
        "name": "java", "expect": "x",
        "log": "INFO start\n" + 'Exception in thread "main" java.lang.OutOfMemoryError: Java heap space\n'
               + "".join(f"\tat com.example.F{i}.run(F.java:{i})\n" for i in range(30)),
        "message": "back-off restarting failed container",
        "events": ["Liveness probe failed: timeout"],
    })
    q = retrieval_query(req, classify(req).as_dict(), [])
    assert "OutOfMemoryError: Java heap space" in q
    assert "back-off restarting failed container" in q and "Liveness probe failed" in q


def test_query_leaves_out_boilerplate_when_the_rules_are_unsure():
    req = build_request({"name": "pg", "expect": "x", "log": "FATAL: sorry, too many clients already\n"})
    cls = classify(req).as_dict()
    assert cls["confidence"] == "low"
    q = retrieval_query(req, cls, [])
    assert "no known pattern" not in q and "crash (exit 1)" not in q
    req = build_request({"name": "seg", "expect": "x", "exit_code": 139, "log": "Segmentation fault\n"})
    assert "exit 139" in retrieval_query(req, classify(req).as_dict(), [])


def test_escalation_from_a_shared_runbook_is_a_fallback():
    shared = {"source": "shared/platform-postgres", "commands": ["kubectl get pods"],
              "escalation": "Platform team"}
    team = {"source": "payments/db", "commands": ["kubectl -n payments get svc db"],
            "escalation": "#payments-oncall"}
    steps, esc = runbook_actions([shared])
    assert steps == [] and esc == "Platform team"              # no commands from shared runbooks
    steps, esc = runbook_actions([shared, team])
    assert esc == "#payments-oncall" and [s["command"] for s in steps] == ["kubectl -n payments get svc db"]


# -- symptom lines and the reload version ---------------------------------------------------

def test_every_symptom_line_is_indexed_on_its_own():
    from gateway.knowledge import index_chunks, parse_markdown

    r = parse_markdown("db", rb("Database limit", body=(
        "# Symptoms\n- `FATAL: sorry, too many clients already`\n- `Too many connections`\n"
        "# Fix\nLower the pool size.\n")))
    pieces = index_chunks(r)
    embedded = [e for e, _ in pieces]
    assert "Database limit\n`FATAL: sorry, too many clients already`" in embedded
    shown = next(s for e, s in pieces if "too many clients" in e and e.count("\n") == 1)
    assert "Lower the pool size." in shown                  # a symptom hit still shows the fix


def test_wait_reload_computes_the_version_the_gateway_logs(tmp_path, monkeypatch):
    import importlib.util

    spec = importlib.util.spec_from_file_location("wait_reload", ROOT / "tests/kb/wait_reload.py")
    wr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wr)
    cms = {"kubelantern-runbooks-builtin": {"a.md": rb("Runbook A"), "b.md": rb("Runbook B"),
                                            "notes.txt": "x"},
           "kubelantern-runbooks-platform": {"a.md": rb("Our A")}}
    for folder, cm in (("00-builtin", "kubelantern-runbooks-builtin"),
                       ("20-kubelantern-runbooks-platform", "kubelantern-runbooks-platform")):
        (tmp_path / folder).mkdir()
        for k, v in cms[cm].items():
            (tmp_path / folder / k).write_text(v)
    deploy = {"spec": {"template": {"spec": {
        "volumes": [{"name": "runbooks-builtin", "configMap": {"name": "kubelantern-runbooks-builtin"}},
                    {"name": "runbooks-cm-0", "configMap": {"name": "kubelantern-runbooks-platform"}},
                    {"name": "runbooks-cm-1", "configMap": {"name": "not-created-yet"}}],
        "containers": [{"name": "gateway", "volumeMounts": [
            {"name": "runbooks-builtin", "mountPath": "/etc/kubelantern/runbooks/00-builtin"},
            {"name": "runbooks-cm-0", "mountPath": "/etc/kubelantern/runbooks/20-kubelantern-runbooks-platform"},
            {"name": "runbooks-cm-1", "mountPath": "/etc/kubelantern/runbooks/20-not-created-yet"}]}]}}}}

    def fake_kubectl(*args):
        if args[3] == "deploy":
            return json.dumps(deploy)
        name = args[4]
        if name not in cms:
            raise wr.subprocess.CalledProcessError(1, "kubectl")
        return json.dumps({"data": cms[name]})

    monkeypatch.setattr(wr, "kubectl", fake_kubectl)
    version, files = wr.expected_version("kubelantern-ai")
    assert files == 3 and version == scan_shared(tmp_path).digest
