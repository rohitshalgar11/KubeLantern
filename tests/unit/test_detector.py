"""Detector + watcher tests.

Fixtures are plain objects with the same attribute shape as the
kubernetes client's V1Pod models, so no cluster or client is needed.
"""

from types import SimpleNamespace as NS

from agent.watcher.detector import detect_failures, format_failure
from agent.watcher.pod_watcher import PodWatcher


def _term(t):
    return NS(exit_code=t[1], reason=t[0], message=None) if t else None


def cs(name="app", waiting=None, terminated=None, running=False, restarts=0, last_term=None):
    state = NS(
        waiting=NS(reason=waiting, message=None) if waiting else None,
        terminated=_term(terminated),
        running=NS() if running else None,
    )
    last = NS(waiting=None, terminated=_term(last_term), running=None) if last_term else None
    return NS(name=name, restart_count=restarts, state=state, last_state=last)


def pod(statuses, ns="demo", name="broken-app-abc", phase="Running", init=None, reason=None):
    return NS(
        metadata=NS(namespace=ns, name=name),
        status=NS(
            phase=phase,
            container_statuses=statuses,
            init_container_statuses=init,
            reason=reason,
            message=None,
        ),
    )


def test_healthy_pod_has_no_failures():
    assert detect_failures(pod([cs(running=True)])) == []


def test_crashloop_detected_with_exit_code():
    p = pod([cs("broken-app", waiting="CrashLoopBackOff", restarts=5, last_term=("Error", 1))])
    [f] = detect_failures(p)
    assert f.reason == "CrashLoopBackOff"
    assert f.restarts == 5
    assert f.exit_code == 1
    out = format_failure(f)
    assert "Namespace : demo" in out and "Restarts  : 5" in out


def test_oom_shows_last_termination_reason():
    p = pod([cs(waiting="CrashLoopBackOff", restarts=2, last_term=("OOMKilled", 137))])
    [f] = detect_failures(p)
    assert f.last_termination_reason == "OOMKilled"
    assert "Last term : OOMKilled" in format_failure(f)


def test_oom_terminated_state_detected_before_backoff():
    [f] = detect_failures(pod([cs(terminated=("OOMKilled", 137))]))
    assert f.reason == "OOMKilled"


def test_image_pull_detected():
    [f] = detect_failures(pod([cs(waiting="ImagePullBackOff")]))
    assert f.reason == "ImagePullBackOff"


def test_container_creating_is_not_failure():
    assert detect_failures(pod([cs(waiting="ContainerCreating")], phase="Pending")) == []


def test_completed_job_is_not_failure():
    assert detect_failures(pod([cs(terminated=("Completed", 0))], phase="Succeeded")) == []


def test_init_container_failure():
    [f] = detect_failures(pod([cs(waiting="PodInitializing")], init=[cs("init", waiting="CrashLoopBackOff")]))
    assert f.init_container and f.container == "init"


def test_evicted_pod():
    [f] = detect_failures(pod(None, phase="Failed", reason="Evicted"))
    assert f.reason == "Evicted"


def test_watcher_reports_only_on_new_restart():
    seen = []
    w = PodWatcher("demo", on_failure=seen.append, core_api=object())
    p3 = pod([cs(waiting="CrashLoopBackOff", restarts=3)])
    w.handle_pod("MODIFIED", p3)
    w.handle_pod("MODIFIED", p3)  # same restart count -> suppressed
    w.handle_pod("MODIFIED", pod([cs(waiting="CrashLoopBackOff", restarts=4)]))
    assert [f.restarts for f in seen] == [3, 4]


def test_watcher_forgets_recovered_and_deleted_pods():
    seen = []
    w = PodWatcher("demo", on_failure=seen.append, core_api=object())
    bad = pod([cs(waiting="CrashLoopBackOff", restarts=1)])
    w.handle_pod("MODIFIED", bad)
    w.handle_pod("MODIFIED", pod([cs(running=True, restarts=1)]))  # recovered
    w.handle_pod("MODIFIED", bad)  # fails again -> reported again
    w.handle_pod("DELETED", bad)
    assert len(seen) == 2 and w._reported == {}


# -- watch event handling (regression: ERROR/BOOKMARK events arrive as dicts) --

import pytest

from agent.watcher.pod_watcher import WatchError, WatchExpired


def _watcher(seen=None):
    return PodWatcher("demo", on_failure=(seen if seen is not None else []).append, core_api=object())


def test_error_410_event_as_dict_raises_watch_expired():
    w = _watcher()
    ev = {"type": "ERROR", "object": {"kind": "Status", "code": 410, "message": "too old"},
          "raw_object": {"kind": "Status", "code": 410, "message": "too old"}}
    with pytest.raises(WatchExpired):
        w.process_event(ev, "100")


def test_other_error_event_raises_watch_error():
    w = _watcher()
    with pytest.raises(WatchError):
        w.process_event({"type": "ERROR", "object": {"code": 500}}, "100")


def test_bookmark_dict_updates_resource_version():
    w = _watcher()
    ev = {"type": "BOOKMARK", "object": {"metadata": {"resourceVersion": "555"}},
          "raw_object": {"metadata": {"resourceVersion": "555"}}}
    assert w.process_event(ev, "100") == "555"


def test_pod_event_is_handled_and_returns_rv():
    seen = []
    w = _watcher(seen)
    p = pod([cs(waiting="CrashLoopBackOff", restarts=2)])
    p.metadata.resource_version = "777"
    assert w.process_event({"type": "MODIFIED", "object": p}, "100") == "777"
    assert len(seen) == 1
