"""Maintenance mode: pause incidents and alerts during planned work (no cluster needed)."""

import io
from contextlib import redirect_stdout

from agent.incident.manager import IncidentManager
from agent.main import Agent
from agent.maintenance import ACTIVE, PAUSED, SETTLING, MaintenanceController
from agent.notify import NotifyClient
from agent.watcher.detector import Failure
from gateway.core import Authenticator, Gateway, GatewayConfig
from kubelantern_common.maintenance import Pause, format_time, from_env, parse_time, read_dir

T0 = 1_791_000_000.0     # 2026-10-03T...Z


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


# -- switch parsing -----------------------------------------------------------------

def test_parse_and_format_time():
    t = parse_time("2026-10-07T22:00:00Z")
    assert format_time(t) == "2026-10-07T22:00:00Z"
    assert parse_time("2026-10-07T22:00:00+00:00") == t
    assert parse_time("2026-10-08T03:30:00+05:30") == t
    assert parse_time("2026-10-07T22:00:00") == t          # no zone = UTC
    assert parse_time("") is None and parse_time(None) is None
    assert parse_time("tomorrow") is None


def test_pause_is_active_until_its_end():
    p = Pause(True, T0 + 60, "upgrade")
    assert p.active(T0) and not p.active(T0 + 60)
    assert Pause(True).active(T0 + 10**6)                     # no end (capped by the agent)
    assert not Pause(False, T0 + 60).active(T0)
    assert Pause.from_dict(p.to_dict()) == p
    assert Pause.from_dict(None) == Pause()


def test_read_dir_reads_a_mounted_configmap(tmp_path):
    assert read_dir(tmp_path / "missing") == Pause()
    (tmp_path / "paused").write_text("true\n")
    (tmp_path / "until").write_text("2026-10-07T22:00:00Z")
    (tmp_path / "reason").write_text("AKS upgrade")
    assert read_dir(tmp_path) == Pause(True, parse_time("2026-10-07T22:00:00Z"), "AKS upgrade")
    (tmp_path / "paused").write_text("false")
    assert not read_dir(tmp_path).paused


def test_from_env():
    assert from_env("true", "", "x") == Pause(True, None, "x")
    assert from_env(None, None, None) == Pause()
    assert not from_env("no", None, None).paused


# -- controller -----------------------------------------------------------------------

def test_local_pause_settles_then_resumes():
    clock = Clock()
    m = MaintenanceController(Pause(True, T0 + 600, "upgrade"), settle_seconds=60, clock=clock)
    event, msg = m.tick()
    assert event == "paused" and m.state == PAUSED and m.quiet
    assert "2026-" in msg and "upgrade" in msg
    assert m.tick() is None
    clock.t = T0 + 600                                        # `until` reached
    assert m.tick()[0] == "settling" and m.state == SETTLING and m.quiet
    clock.t = T0 + 630
    assert m.tick() is None and m.quiet
    clock.t = T0 + 660
    assert m.tick()[0] == "resumed" and m.state == ACTIVE and not m.quiet


def test_cluster_switch_is_polled_and_can_be_turned_off():
    clock, answer = Clock(), {"paused": True, "until": None, "reason": "cluster upgrade"}
    m = MaintenanceController(settle_seconds=0, fetch=lambda: answer, clock=clock)
    assert m.tick() is None                                   # not polled yet
    m.poll()
    assert m.tick()[0] == "paused"
    answer = {"paused": False}
    m.poll()
    assert m.tick()[0] == "settling"
    assert m.tick()[0] == "resumed"


def test_unreachable_gateway_keeps_the_last_state():
    calls = {"n": 0}

    def fetch():
        calls["n"] += 1
        if calls["n"] > 1:
            raise OSError("gateway down")                    # e.g. its node is being upgraded
        return {"paused": True}

    m = MaintenanceController(fetch=fetch, clock=Clock())
    m.poll()
    m.tick()
    m.poll()
    assert m.tick() is None and m.state == PAUSED


def test_open_ended_pause_is_capped():
    clock = Clock()
    m = MaintenanceController(fetch=lambda: {"paused": True}, settle_seconds=0, max_hours=2,
                              clock=clock)
    m.poll()
    assert m.tick()[0] == "paused"
    clock.t = T0 + 2 * 3600
    assert m.tick()[0] == "settling"                          # forgotten switch can't silence forever
    assert m.tick()[0] == "resumed"
    clock.t += 60
    m.poll()
    assert m.tick() is None and not m.quiet                   # stays capped while the switch is on
    m.fetch = lambda: {"paused": False}
    m.poll()
    m.tick()
    m.fetch = lambda: {"paused": True}                        # a NEW pause works again
    m.poll()
    assert m.tick()[0] == "paused"


def test_pause_with_end_is_not_capped():
    clock = Clock()
    m = MaintenanceController(Pause(True, T0 + 5 * 3600), max_hours=2, clock=clock)
    m.tick()
    clock.t = T0 + 3 * 3600
    assert m.tick() is None and m.state == PAUSED


# -- agent -----------------------------------------------------------------------------

def F(pod="api-1", workload="api", reason="Error"):
    return Failure(namespace="payments", pod=pod, container="app", reason=reason, restarts=1,
                   exit_code=1, workload_kind="Deployment", workload_name=workload)


class _Collector:
    def collect(self, f):
        return {"failure": {"pod": f.pod, "reason": f.reason, "restarts": f.restarts,
                            "container": f.container, "namespace": f.namespace},
                "logs": {"current": "boom\n"}}


class _Diag:
    def __init__(self):
        self.submitted = []

    def submit(self, incident, bundle):
        self.submitted.append(incident["id"])


def _agent(clock, failing=()):
    sent = []
    notify = NotifyClient("http://127.0.0.1:1/v1/notify", {"diagnosis", "resolved", "opened"},
                          post=lambda url, ev: sent.append(ev) or 202)
    m = MaintenanceController(Pause(True, T0 + 600, "upgrade"), settle_seconds=60, clock=clock)
    a = Agent("payments", IncidentManager(resolve_after_seconds=60), _Collector(),
              diagnosis=_Diag(), notifier=notify, maintenance=m, resync=lambda: list(failing))
    return a, notify, sent


def test_no_new_incidents_diagnoses_or_alerts_while_paused():
    clock = Clock()
    a, notify, sent = _agent(clock)
    out = io.StringIO()
    with redirect_stdout(out):
        a.tick()
        a.on_failure(F())
        a.on_failure(F(pod="api-2"))
    notify.flush()
    assert a.manager.open_incidents() == []
    assert a.diagnosis.submitted == [] and sent == []
    assert "[MAINTENANCE] paused" in out.getvalue()


def test_after_the_pause_only_still_failing_workloads_become_incidents():
    clock = Clock()
    still_broken = [F(pod="worker-1", workload="worker")]
    a, notify, _ = _agent(clock, failing=still_broken)
    out = io.StringIO()
    with redirect_stdout(out):
        a.tick()
        a.on_failure(F())                                   # api: flapped during the upgrade
        a.on_failure(F(pod="worker-1", workload="worker"))  # worker: really broken
        clock.t = T0 + 600
        a.tick()                                            # settling
        a.on_failure(F())                                   # still quiet while settling
        clock.t = T0 + 660
        a.tick()                                            # resumed -> re-check
    notify.flush()
    ids = [i["id"] for i in a.manager.open_incidents()]
    assert len(ids) == 1 and "worker" in ids[0]
    assert a.diagnosis.submitted == ids                     # diagnosed now, once
    text = out.getvalue()
    assert "3 failure event(s) ignored" in text
    assert "re-check done: 1 failing container(s)" in text


def test_open_incidents_are_tracked_silently_and_resolved_is_still_sent():
    clock = Clock()
    a, notify, sent = _agent(clock)
    a.maintenance = MaintenanceController(settle_seconds=0, clock=clock)   # not paused yet
    with redirect_stdout(io.StringIO()):
        a.on_failure(F())                                   # incident opened normally
        assert len(a.diagnosis.submitted) == 1
        a.maintenance.local = Pause(True, None, "upgrade")
        a.tick()                                            # paused
        a.on_failure(F(reason="OOMKilled"))                 # cause change: tracked, silent
        assert len(a.diagnosis.submitted) == 1
        inc = a.manager.open_incidents()[0]
        assert "oom" in inc["cause"]
        a.manager.clear("payments", "api-1", "app", T0 + 10)
        for u in a.manager.tick(T0 + 200):
            a.emit(u)
    notify.flush()
    assert [e["kind"] for e in sent] == ["opened", "resolved"]


def test_no_maintenance_controller_means_normal_behaviour():
    a = Agent("payments", IncidentManager(), _Collector(), diagnosis=_Diag())
    with redirect_stdout(io.StringIO()):
        a.on_failure(F())
    assert not a.quiet and len(a.diagnosis.submitted) == 1


# -- gateway endpoint ----------------------------------------------------------------

class _Reviewer:
    def review(self, token, audience):
        users = {"agent": "system:serviceaccount:payments:kubelantern-agent"}
        return (True, users[token], None) if token in users else (False, None, "invalid token")


def test_gateway_endpoint_requires_an_agent_token():
    state = {"pause": Pause(True, T0 + 3600, "cluster upgrade")}
    gw = Gateway(GatewayConfig(), Authenticator(_Reviewer(), "kubelantern-gateway"), llm=None,
                 maintenance=lambda: state["pause"], clock=lambda: T0)
    assert gw.maintenance(None)[0] == 401
    assert gw.maintenance("Bearer nope")[0] in (401, 403)
    status, body = gw.maintenance("Bearer agent")
    assert status == 200 and body["paused"] is True and body["reason"] == "cluster upgrade"
    assert body["until"] == format_time(T0 + 3600)
    state["pause"] = Pause()
    assert gw.maintenance("Bearer agent")[1]["paused"] is False


def test_gateway_without_switch_reports_not_paused():
    gw = Gateway(GatewayConfig(), Authenticator(_Reviewer(), "kubelantern-gateway"), llm=None,
                 clock=lambda: T0)
    assert gw.maintenance("Bearer agent") == (200, {"paused": False, "until": None, "reason": ""})
