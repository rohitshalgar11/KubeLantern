"""Incident manager tests — deterministic, time is passed in explicitly."""

from types import SimpleNamespace as NS

from agent.incident.manager import Cause, IncidentManager, classify, format_update
from agent.watcher.detector import Failure, workload_of
from agent.watcher.pod_watcher import PodWatcher


def F(reason="Error", restarts=0, exit_code=1, last=None, pod="broken-app-66ff-a",
      container="broken-app", workload="broken-app", ns="demo"):
    return Failure(namespace=ns, pod=pod, container=container, reason=reason, restarts=restarts,
                   exit_code=exit_code, last_termination_reason=last,
                   workload_kind="Deployment", workload_name=workload)


def kinds(updates):
    return [u.kind for u in updates]


# -- classification ----------------------------------------------------------

def test_classify_state_flips_to_same_cause():
    assert classify(F("Error", exit_code=1)) == Cause("crash", 1)
    assert classify(F("CrashLoopBackOff", exit_code=1, last="Error")) == Cause("crash", 1)
    assert classify(F("OOMKilled", exit_code=137)) == Cause("oom", 137)
    assert classify(F("CrashLoopBackOff", exit_code=137, last="OOMKilled")) == Cause("oom", 137)
    assert classify(F("ErrImagePull", exit_code=None)).family == "image-pull"
    assert classify(F("ImagePullBackOff", exit_code=None)).family == "image-pull"


# -- the exact sequence from the live cluster ----------------------------------

def test_crashloop_flip_sequence_is_one_incident():
    m = IncidentManager()
    seq = [
        F("Error", 0, 1, "Error"),
        F("Error", 1, 1, "Error"),
        F("CrashLoopBackOff", 1, 1, "Error"),
        F("Error", 2, 1, "Error"),
        F("CrashLoopBackOff", 2, 1, "Error"),
        F("Error", 3, 1, "Error"),
    ]
    updates = []
    for t, f in enumerate(seq):
        updates += m.observe(f, now=100 + t * 10)
    assert kinds(updates) == ["opened"]
    [inc] = m.open_incidents()
    assert inc["cause"] == "crash (exit 1)"
    assert inc["observations"] == 6
    assert inc["failing_pods"] == {"broken-app-66ff-a": 3}


def test_image_pull_flip_is_one_incident():
    m = IncidentManager()
    u = []
    for t, r in enumerate(["ErrImagePull", "ImagePullBackOff", "ErrImagePull", "ImagePullBackOff"]):
        u += m.observe(F(r, 0, None, container="bad-image", workload="bad-image"), 100 + t)
    assert kinds(u) == ["opened"]


def test_replicas_share_one_incident():
    m = IncidentManager()
    u = []
    for p in ("a", "b", "c"):
        u += m.observe(F(pod=f"broken-app-66ff-{p}"), 100)
    assert kinds(u) == ["opened"]
    assert len(m.open_incidents()[0]["failing_pods"]) == 3


def test_different_workloads_and_containers_are_separate():
    m = IncidentManager()
    u = m.observe(F(workload="broken-app"), 100)
    u += m.observe(F(workload="oom-app", container="oom-app", pod="oom-app-x"), 100)
    u += m.observe(F(container="sidecar"), 100)
    assert kinds(u) == ["opened", "opened", "opened"]


def test_same_workload_name_in_other_namespace_is_separate():
    m = IncidentManager()
    u = m.observe(F(ns="payments"), 100) + m.observe(F(ns="orders"), 100)
    assert kinds(u) == ["opened", "opened"]
    assert len({x.incident["id"] for x in u}) == 2


# -- cause changes -------------------------------------------------------------

def test_cause_change_is_reported_once():
    m = IncidentManager()
    u = m.observe(F("Error", 1, 1), 100)
    u += m.observe(F("OOMKilled", 2, 137), 110)
    u += m.observe(F("CrashLoopBackOff", 2, 137, "OOMKilled"), 120)
    assert kinds(u) == ["opened", "cause_changed"]
    assert u[1].previous_cause == "crash (exit 1)"
    assert u[1].incident["cause"] == "oom (exit 137)"
    assert u[1].needs_evidence


def test_unknown_exit_code_does_not_count_as_change():
    m = IncidentManager()
    u = m.observe(F("CrashLoopBackOff", 1, None), 100)
    u += m.observe(F("Error", 2, 1), 110)
    assert kinds(u) == ["opened"]
    assert m.open_incidents()[0]["cause"] == "crash (exit 1)"


# -- resolution ------------------------------------------------------------------

def test_brief_running_between_crashes_does_not_resolve():
    m = IncidentManager(resolve_after_seconds=120)
    m.observe(F("Error", 1), 100)
    m.clear("demo", "broken-app-66ff-a", "broken-app", 105)  # Running for a moment
    assert m.tick(120) == []
    m.observe(F("Error", 2), 160)  # crashed again -> healthy timer reset
    assert m.tick(400) == []
    assert len(m.open_incidents()) == 1


def test_resolves_after_quiet_period():
    m = IncidentManager(resolve_after_seconds=120)
    m.observe(F("Error", 1), 100)
    m.clear("demo", "broken-app-66ff-a", "broken-app", 200)
    assert m.tick(300) == []
    [u] = m.tick(321)
    assert u.kind == "resolved" and not u.needs_evidence
    assert m.open_incidents() == []
    assert m.resolved_incidents()[0]["id"] == u.incident["id"]


def test_resolve_needs_all_replicas_healthy():
    m = IncidentManager(resolve_after_seconds=60)
    m.observe(F(pod="p1"), 100)
    m.observe(F(pod="p2"), 100)
    m.clear("demo", "p1", "broken-app", 110)
    assert "resolved" not in kinds(m.tick(1000))
    m.clear("demo", "p2", "broken-app", 1000)
    assert kinds(m.tick(1061)) == ["resolved"]


def test_recurrence_after_resolve_opens_new_incident():
    m = IncidentManager(resolve_after_seconds=60)
    first = m.observe(F(), 100)[0].incident["id"]
    m.clear("demo", "broken-app-66ff-a", "broken-app", 110)
    m.tick(200)
    [u] = m.observe(F(pod="broken-app-77aa-z"), 300)
    assert u.kind == "opened" and u.incident["id"] != first


def test_rollout_to_new_pod_keeps_same_incident():
    m = IncidentManager(resolve_after_seconds=300)
    m.observe(F(pod="broken-app-66ff-a"), 100)
    m.clear("demo", "broken-app-66ff-a", "broken-app", 110)  # old pod deleted
    u = m.observe(F(pod="broken-app-77aa-b"), 130)  # new RS, same Deployment, still broken
    assert u == []
    assert set(m.open_incidents()[0]["affected_pods"]) == {"broken-app-66ff-a", "broken-app-77aa-b"}


# -- reminders ---------------------------------------------------------------------

def test_reminder_when_still_failing():
    m = IncidentManager(reminder_seconds=600)
    m.observe(F(), 100)
    assert m.tick(600) == []
    assert kinds(m.tick(701)) == ["reminder"]
    assert m.tick(800) == []  # not again until another interval
    assert kinds(m.tick(1302)) == ["reminder"]


# -- ids & rendering -------------------------------------------------------------------

def test_incident_id_shape():
    [u] = IncidentManager().observe(F(), 100)
    ns, rest = u.incident["id"].split("-", 1)
    suffix = rest.rsplit("-", 1)[1]
    assert ns == "demo" and rest.startswith("broken-app-INC")
    assert suffix.startswith("INC") and len(suffix) == 9


def test_format_update():
    m = IncidentManager(resolve_after_seconds=60)
    [u] = m.observe(F("Error", 1), 100)
    text = format_update(u, 130)
    assert "[INCIDENT OPENED]" in text and "Workload  : Deployment/broken-app" in text
    assert "Cause     : crash (exit 1)" in text and "Failing   : 30s" in text
    m.clear("demo", "broken-app-66ff-a", "broken-app", 200)
    [r] = m.tick(400)
    assert "[INCIDENT RESOLVED]" in format_update(r, 400) and "Duration  : 5m00s" in format_update(r, 400)


# -- workload resolution -------------------------------------------------------------------

def _pod(owner_kind=None, owner_name=None, labels=None, name="p"):
    refs = [NS(kind=owner_kind, name=owner_name, controller=True)] if owner_kind else None
    return NS(metadata=NS(name=name, labels=labels, owner_references=refs))


def test_workload_of():
    assert workload_of(_pod("ReplicaSet", "broken-app-66ff895865",
                            {"pod-template-hash": "66ff895865"})) == ("Deployment", "broken-app")
    assert workload_of(_pod("ReplicaSet", "standalone-rs", {})) == ("ReplicaSet", "standalone-rs")
    assert workload_of(_pod("StatefulSet", "db")) == ("StatefulSet", "db")
    assert workload_of(_pod(name="bare")) == ("Pod", "bare")


# -- watcher clear signal ----------------------------------------------------------------------

def _wpod(state, restarts=0, name="broken-app-66ff-a"):
    waiting = NS(reason=state, message=None) if state in ("CrashLoopBackOff",) else None
    running = NS() if state == "Running" else None
    term = NS(reason="Error", exit_code=1, message=None) if state == "Error" else None
    cs = NS(name="broken-app", restart_count=restarts,
            state=NS(waiting=waiting, running=running, terminated=term),
            last_state=NS(terminated=NS(reason="Error", exit_code=1)) if restarts else None)
    return NS(metadata=NS(name=name, namespace="demo", labels={}, owner_references=None),
              status=NS(phase="Running", container_statuses=[cs], init_container_statuses=None,
                        reason=None, message=None))


def test_watcher_reason_flip_is_not_a_clear():
    cleared = []
    w = PodWatcher("demo", on_failure=lambda f: None, core_api=object(),
                   on_clear=lambda *a: cleared.append(a))
    w.handle_pod("MODIFIED", _wpod("Error", 1))
    w.handle_pod("MODIFIED", _wpod("CrashLoopBackOff", 1))  # flip: still failing
    assert cleared == []
    w.handle_pod("MODIFIED", _wpod("Running", 1))  # actually running now
    assert cleared == [("demo", "broken-app-66ff-a", "broken-app")]


def test_watcher_delete_clears():
    cleared = []
    w = PodWatcher("demo", on_failure=lambda f: None, core_api=object(),
                   on_clear=lambda *a: cleared.append(a))
    w.handle_pod("MODIFIED", _wpod("Error", 1))
    w.handle_pod("DELETED", _wpod("Error", 1))
    assert cleared == [("demo", "broken-app-66ff-a", "broken-app")]


def test_relist_clears_pods_deleted_while_disconnected():
    cleared = []

    class Core:
        def list_namespaced_pod(self, ns):
            return NS(items=[], metadata=NS(resource_version="9"))

    w = PodWatcher("demo", on_failure=lambda f: None, core_api=Core(),
                   on_clear=lambda *a: cleared.append(a))
    w.handle_pod("MODIFIED", _wpod("Error", 1))
    w._initial_sync()
    assert cleared == [("demo", "broken-app-66ff-a", "broken-app")]


# -- agent glue: evidence only on opened / cause_changed ---------------------------------------

def test_agent_collects_evidence_only_when_needed(capsys=None):
    import io
    from contextlib import redirect_stdout

    from agent.main import Agent

    class Collector:
        calls = 0

        def collect(self, f):
            Collector.calls += 1
            return {"failure": {"pod": f.pod, "reason": f.reason, "restarts": f.restarts,
                                "container": f.container, "namespace": f.namespace},
                    "logs": {"current": "FATAL: boom\n"}}

    a = Agent("demo", IncidentManager(), Collector())
    buf = io.StringIO()
    with redirect_stdout(buf):
        for f in [F("Error", 0), F("CrashLoopBackOff", 1, 1, "Error"), F("Error", 1), F("Error", 2)]:
            a.on_failure(f)
    assert Collector.calls == 1
    out = buf.getvalue()
    assert out.count("[INCIDENT OPENED]") == 1 and "FATAL: boom" in out


# -- regressions from the live run: terminating pods and the agent itself ----

def _failing_pod(deleting=False, labels=None):
    p = _wpod("Error", 1)
    p.metadata.deletion_timestamp = "2026-10-05T13:30:00Z" if deleting else None
    p.metadata.labels = labels or {}
    return p


def test_terminating_pod_is_not_a_failure():
    from agent.watcher.detector import detect_failures
    assert detect_failures(_failing_pod()) != []
    assert detect_failures(_failing_pod(deleting=True)) == []


def test_kubelantern_own_pods_are_ignored():
    from agent.watcher.detector import detect_failures
    assert detect_failures(_failing_pod(labels={"app.kubernetes.io/part-of": "kubelantern"})) == []


def test_pod_starting_to_terminate_clears_instead_of_changing_cause():
    seen, cleared = [], []
    w = PodWatcher("demo", on_failure=seen.append, core_api=object(),
                   on_clear=lambda *a: cleared.append(a))
    w.handle_pod("MODIFIED", _failing_pod())
    w.handle_pod("MODIFIED", _failing_pod(deleting=True))  # rollout kills it: exit 137
    assert len(seen) == 1 and cleared == [("demo", "broken-app-66ff-a", "broken-app")]


def test_signal_exit_labels():
    assert Cause("crash", 137).label == "crash (exit 137 SIGKILL)"
    assert Cause("crash", 143).label == "crash (exit 143 SIGTERM)"
    assert Cause("crash", 1).label == "crash (exit 1)"



# -- scope changes (user request after live run) --------------------

def test_scale_up_reports_one_scope_change():
    m = IncidentManager(scope_window_seconds=20)
    m.observe(F(pod="p1"), 100)
    m.observe(F(pod="p2"), 130)
    m.observe(F(pod="p3"), 133)  # scale to 3: pods appear within seconds
    assert m.tick(140) == []  # still inside coalesce window
    [u] = m.tick(151)
    assert u.kind == "scope_changed" and u.previous_pod_count == 1
    assert len(u.incident["failing_pods"]) == 3 and not u.needs_evidence
    assert "Scope     : 1 -> 3 pods failing" in format_update(u, 151)
    assert m.tick(500) == []  # nothing more


def test_same_pods_restarting_is_not_a_scope_change():
    m = IncidentManager(scope_window_seconds=20)
    for t, r in enumerate(range(5)):
        m.observe(F(restarts=r), 100 + t * 30)
    assert m.tick(1000) == []


def test_cause_change_absorbs_pending_scope():
    m = IncidentManager(scope_window_seconds=20)
    m.observe(F(pod="p1"), 100)
    m.observe(F("OOMKilled", 0, 137, pod="p2"), 130)  # new pod + new cause
    assert kinds(m.tick(200)) == []  # p2 already shown in CAUSE CHANGED


def test_rollout_replacing_pods_is_not_a_scope_change():
    # regression (live run): a rollout swaps failing pods one by one; the number
    # failing at once never grows, so no "4 -> 6 pods" update with "Pods: none".
    m = IncidentManager(scope_window_seconds=20)
    for i, p in enumerate(["a1", "a2", "a3"]):
        m.observe(F(pod=p), 100 + i)
    assert kinds(m.tick(130)) == ["scope_changed"]  # 1 -> 3
    t = 200
    for old, new in [("a1", "b1"), ("a2", "b2"), ("a3", "b3")]:
        m.clear("demo", old, "broken-app", t)
        m.observe(F(pod=new), t + 1)
        t += 10
    assert m.tick(t + 100) == []


def test_scope_change_never_reports_zero_failing_pods():
    m = IncidentManager(scope_window_seconds=20)
    m.observe(F(pod="p1"), 100)
    m.observe(F(pod="p2"), 105)
    m.clear("demo", "p1", "broken-app", 110)
    m.clear("demo", "p2", "broken-app", 111)  # recovered before the window closed
    assert m.tick(130) == []
