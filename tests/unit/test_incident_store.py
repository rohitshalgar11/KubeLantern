"""Incident persistence: Incident objects in the agent's namespace (no cluster needed)."""

import copy
import io
from contextlib import redirect_stdout

from agent.incident.manager import IncidentManager
from agent.incident.store import (
    IncidentStore,
    from_object,
    object_name,
    to_object,
)
from agent.main import Agent
from agent.watcher.detector import Failure


class ApiError(Exception):
    def __init__(self, status):
        self.status, self.reason = status, "x"


def _merge(a, b):
    out = copy.deepcopy(a)
    for k, v in b.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


class FakeCustomApi:
    """In-memory CustomObjectsApi for kubelantern.io incidents."""

    def __init__(self, crd_installed=True):
        self.crd = crd_installed
        self.objects: dict[tuple[str, str], dict] = {}
        self.calls: list[str] = []

    def _check(self, group, plural):
        assert (group, plural) == ("kubelantern.io", "incidents")
        if not self.crd:
            raise ApiError(404)

    def list_namespaced_custom_object(self, group, version, ns, plural, label_selector=None,
                                      limit=None):
        self._check(group, plural)
        self.calls.append("list")
        items = [o for (n, _), o in self.objects.items() if n == ns]
        if label_selector:
            k, v = label_selector.split("=")
            items = [o for o in items if o["metadata"]["labels"].get(k) == v]
        return {"items": copy.deepcopy(items)}

    def patch_namespaced_custom_object(self, group, version, ns, plural, name, body):
        self._check(group, plural)
        self.calls.append("patch")
        if (ns, name) not in self.objects:
            raise ApiError(404)
        self.objects[(ns, name)] = _merge(self.objects[(ns, name)], body)

    def create_namespaced_custom_object(self, group, version, ns, plural, body):
        self._check(group, plural)
        self.calls.append("create")
        self.objects[(ns, body["metadata"]["name"])] = copy.deepcopy(body)

    def delete_namespaced_custom_object(self, group, version, ns, plural, name):
        self._check(group, plural)
        self.calls.append("delete")
        self.objects.pop((ns, name))


def F(reason="Error", restarts=1, exit_code=1, pod="broken-app-66ff-a", last=None):
    return Failure(namespace="demo", pod=pod, container="broken-app", reason=reason,
                   restarts=restarts, exit_code=exit_code, last_termination_reason=last,
                   workload_kind="Deployment", workload_name="broken-app")


RESULT = {"model": "qwen2.5:1.5b",
          "diagnosis": {"category": "dependency", "confidence": "high",
                        "summary": "db missing", "probable_cause": "Service 'db' not found",
                        "next_steps": ["kubectl apply -f examples/demo-db.yaml"],
                        "escalation": "demo team"},
          "references": [{"source": "demo/broken-app-database"}]}


def _store(api, **kw):
    return IncidentStore("demo", api=api, clock=lambda: 1_800_000_000.0, **kw)


# -- object format ---------------------------------------------------------------

def test_object_name_is_a_valid_kubernetes_name():
    assert object_name("demo-broken-app-INC393794") == "demo-broken-app-inc393794"


def test_round_trip_keeps_what_restore_needs():
    m = IncidentManager()
    m.observe(F(pod="p1"), 1000.0)
    m.observe(F(pod="p2"), 1005.0)
    rec = m.open_incidents()[0]
    obj = to_object(rec, None, 1010.0)
    assert obj["kind"] == "Incident" and obj["metadata"]["name"] == object_name(rec["id"])
    assert obj["metadata"]["labels"]["kubelantern.io/status"] == "open"
    assert "app.kubernetes.io/instance" not in obj["metadata"]["labels"]  # not tracked by ArgoCD
    back = from_object(obj, "demo")
    for k in ("id", "workload_kind", "workload_name", "container", "cause", "cause_family",
              "exit_code", "opened_at", "affected_pods", "peak_failing"):
        assert back[k] == rec[k], k


# -- writes ------------------------------------------------------------------------

def test_save_creates_then_updates_and_diagnosis_is_attached():
    api = FakeCustomApi()
    s = _store(api)
    m = IncidentManager()
    [u] = m.observe(F(), 1000.0)
    s.save(u.incident)
    s.flush()
    s.save_diagnosis(u.incident["id"], RESULT)
    s.flush()
    [obj] = api.objects.values()
    assert api.calls.count("create") == 1
    assert obj["status"]["state"] == "Open"
    assert obj["status"]["diagnosis"]["category"] == "dependency"
    assert obj["status"]["diagnosis"]["runbooks"] == ["demo/broken-app-database"]


def test_resolved_updates_label_and_is_forgotten_from_memory():
    api = FakeCustomApi()
    s = _store(api)
    m = IncidentManager(resolve_after_seconds=60)
    [u] = m.observe(F(), 1000.0)
    s.save(u.incident)
    m.clear("demo", "broken-app-66ff-a", "broken-app", 1010.0)
    [r] = m.tick(1100.0)
    s.save(r.incident)
    s.flush()
    [obj] = api.objects.values()
    assert obj["metadata"]["labels"]["kubelantern.io/status"] == "resolved"
    assert obj["status"]["state"] == "Resolved" and obj["status"]["resolvedAt"]
    assert s._records == {} and s._diagnoses == {}


def test_missing_crd_disables_persistence_quietly_and_rechecks():
    api = FakeCustomApi(crd_installed=False)
    now = [1000.0]
    s = IncidentStore("demo", api=api, clock=lambda: now[0], recheck_seconds=300)
    assert s.load_open() == []
    s.save(IncidentManager().observe(F(), 1000.0)[0].incident)
    s.flush()                                   # no exception, nothing written
    assert s.enabled is False and api.objects == {}
    api.crd = True
    now[0] += 301                               # CRD installed later -> picked up
    assert s.available() is True


def test_prune_keeps_newest_history_and_drops_expired():
    api = FakeCustomApi()
    s = _store(api, history=2, retention_days=1)
    now = 1_800_000_000.0
    for i, age_hours in enumerate([1, 2, 3, 30]):
        rec = {"id": f"demo-app{i}-INC{i:06d}", "status": "resolved", "cause": "crash (exit 1)",
               "container": "c", "workload_kind": "Deployment", "workload_name": f"app{i}",
               "opened_at": now - age_hours * 3600 - 60, "resolved_at": now - age_hours * 3600}
        api.create_namespaced_custom_object("kubelantern.io", "v1alpha1", "demo", "incidents",
                                            to_object(rec, None, now))
    assert s.prune() == 2
    remaining = sorted(o["spec"]["id"] for o in api.objects.values())
    assert remaining == ["demo-app0-INC000000", "demo-app1-INC000001"]


# -- restore after an agent restart ------------------------------------------------------

def _persisted(api, with_diagnosis=True):
    """Simulate a previous agent: one open incident (3 pods), optionally diagnosed."""
    s = _store(api)
    m = IncidentManager(scope_window_seconds=20)
    m.observe(F(pod="p1"), 1000.0)
    m.observe(F(pod="p2"), 1001.0)
    m.observe(F(pod="p3"), 1002.0)
    m.tick(1030.0)                               # SCOPE CHANGED 1 -> 3
    [rec] = m.open_incidents()
    s.save(rec)
    if with_diagnosis:
        s.save_diagnosis(rec["id"], RESULT)
    s.flush()
    return rec["id"]


def test_restart_continues_the_same_incident_without_opened():
    api = FakeCustomApi()
    inc_id = _persisted(api)

    m = IncidentManager(scope_window_seconds=20, resolve_after_seconds=120)
    restored = m.restore(_store(api).load_open(), 2000.0)
    assert [r["id"] for r in restored] == [inc_id]
    for pod in ("p1", "p2", "p3"):               # still failing after the restart
        assert m.observe(F(pod=pod, restarts=9), 2005.0) == []
    assert m.tick(2100.0) == []                  # no scope change: same 3 pods
    [u] = m.observe(F("OOMKilled", 9, 137, pod="p1", last="OOMKilled"), 2200.0)
    assert u.kind == "cause_changed" and u.incident["id"] == inc_id


def test_incident_that_recovered_while_agent_was_down_resolves():
    api = FakeCustomApi()
    inc_id = _persisted(api)
    m = IncidentManager(resolve_after_seconds=120)
    m.restore(_store(api).load_open(), 2000.0)
    assert m.tick(2060.0) == []
    [u] = m.tick(2121.0)
    assert u.kind == "resolved" and u.incident["id"] == inc_id


class _Collector:
    calls = 0

    def collect(self, f):
        _Collector.calls += 1
        return {"failure": {"pod": f.pod}}


class _Diagnosis:
    def __init__(self):
        self.submitted = []

    def submit(self, incident, bundle):
        self.submitted.append(incident["id"])


def _restarted_agent(api):
    diag = _Diagnosis()
    a = Agent("demo", IncidentManager(), _Collector(), diagnosis=diag, store=_store(api))
    with redirect_stdout(io.StringIO()) as out:
        a.restore()
        for i in range(3):
            a.on_failure(F(pod="p1", restarts=10 + i))
    return a, diag, out.getvalue()


def test_agent_restart_does_not_rediagnose_a_diagnosed_incident():
    api = FakeCustomApi()
    inc_id = _persisted(api, with_diagnosis=True)
    _, diag, out = _restarted_agent(api)
    assert "Restored open incident(s): " + inc_id in out
    assert "[INCIDENT OPENED]" not in out
    assert diag.submitted == []


def test_agent_restart_diagnoses_once_if_the_diagnosis_never_arrived():
    api = FakeCustomApi()
    inc_id = _persisted(api, with_diagnosis=False)
    _, diag, out = _restarted_agent(api)
    assert "[INCIDENT OPENED]" not in out
    assert diag.submitted == [inc_id]            # exactly once, same ID
