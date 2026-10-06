"""Collector tests with fake Kubernetes APIs (no cluster needed)."""

from types import SimpleNamespace as NS

from agent.collector.collector import DiagnosticCollector, format_evidence, redact
from agent.watcher.detector import Failure


class ApiError(Exception):
    def __init__(self, status, reason):
        self.status, self.reason = status, reason


def _pod(restart_count=3):
    container = NS(
        name="broken-app", image="busybox:1.36", command=["sh", "-c", "exit 1"], args=None,
        resources=NS(requests={"memory": "16Mi"}, limits={"cpu": "50m", "memory": "32Mi"}),
    )
    status = NS(
        phase="Running", qos_class="Burstable", start_time=None,
        conditions=[NS(type="Ready", status="False", reason="ContainersNotReady", message=None)],
        container_statuses=[NS(
            name="broken-app", ready=False, restart_count=restart_count,
            last_state=NS(terminated=NS(reason="Error", exit_code=1, started_at=None, finished_at=None)),
        )],
        init_container_statuses=None,
    )
    meta = NS(
        name="broken-app-abc", namespace="demo", labels={"app": "broken-app"},
        owner_references=[NS(kind="ReplicaSet", name="broken-app-66ff")],
    )
    return NS(metadata=meta, spec=NS(node_name="kubelantern-worker", containers=[container],
                                    init_containers=None), status=status)


class FakeCore:
    """log_style: 'bytes' (urllib3 .data), 'repr' (str(bytes) client bug), 'str'."""

    def __init__(self, fail_previous=False, log_style="bytes", previous_text=None,
                 live_restarts=3, current_text="starting\n"):
        self.fail_previous = fail_previous
        self.live_restarts = live_restarts
        self.current_text = current_text
        self.log_style = log_style
        self.previous_text = previous_text
        self.log_calls = []
        self.namespaces_touched = set()

    def read_namespaced_pod(self, name, ns):
        self.namespaces_touched.add(ns)
        return _pod(self.live_restarts)

    def read_namespaced_pod_log(self, name, ns, container, previous, tail_lines, limit_bytes,
                                _preload_content=True):
        self.namespaces_touched.add(ns)
        self.log_calls.append(previous)
        if previous and self.fail_previous:
            raise ApiError(self.fail_previous, "Bad Request" if self.fail_previous == 400 else "x")
        text = (self.previous_text or "starting\nFATAL: cannot connect to db password=hunter2\n"
                ) if previous else self.current_text
        raw = text.encode()
        if self.log_style == "bytes":
            return NS(data=raw)
        if self.log_style == "repr":
            return str(raw)
        return text

    def list_namespaced_event(self, ns, field_selector):
        self.namespaces_touched.add(ns)
        assert field_selector == "involvedObject.name=broken-app-abc"
        return NS(items=[
            NS(type="Warning", reason="BackOff", message="Back-off restarting failed container",
               count=7, last_timestamp="2026-10-05T10:02:00Z", event_time=None, first_timestamp=None),
            NS(type="Normal", reason="Pulled", message="Container image pulled",
               count=4, last_timestamp="2026-10-05T10:00:00Z", event_time=None, first_timestamp=None),
        ])

    def list_namespaced_service(self, ns):
        self.namespaces_touched.add(ns)
        return NS(items=[
            NS(metadata=NS(name="broken-app"),
               spec=NS(selector={"app": "broken-app"}, type="ClusterIP",
                       ports=[NS(port=80, target_port=8080, protocol="TCP")])),
            NS(metadata=NS(name="other"), spec=NS(selector={"app": "other"}, type="ClusterIP", ports=[])),
            NS(metadata=NS(name="headless-no-selector"), spec=NS(selector=None, type="ClusterIP", ports=[])),
        ])


class FakeApps:
    def read_namespaced_replica_set(self, name, ns):
        return NS(metadata=NS(name=name, owner_references=[NS(kind="Deployment", name="broken-app")]),
                  spec=NS(replicas=1), status=NS(ready_replicas=None))

    def read_namespaced_deployment(self, name, ns):
        return NS(
            metadata=NS(name=name, generation=1),
            spec=NS(replicas=1, strategy=NS(type="RollingUpdate"),
                    template=NS(spec=NS(containers=[NS(image="busybox:1.36")]))),
            status=NS(available_replicas=None, unavailable_replicas=1,
                      conditions=[NS(type="Available", status="False",
                                     reason="MinimumReplicasUnavailable", message="")]),
        )


def _failure(**kw):
    base = {"namespace": "demo", "pod": "broken-app-abc", "container": "broken-app",
            "reason": "CrashLoopBackOff", "restarts": 3, "exit_code": 1,
            "last_termination_reason": "Error"}
    base.update(kw)
    return Failure(**base)


def test_full_bundle():
    core = FakeCore()
    b = DiagnosticCollector("demo", core, FakeApps()).collect(_failure())
    assert b["pod"]["node"] == "kubelantern-worker"
    assert b["container"]["image"] == "busybox:1.36"
    assert b["container"]["last_termination"]["exit_code"] == 1
    assert b["resources"]["broken-app"]["limits"] == {"cpu": "50m", "memory": "32Mi"}
    assert [o["kind"] for o in b["owners"]] == ["ReplicaSet", "Deployment"]
    assert [s["name"] for s in b["services"]] == ["broken-app"]
    assert [e["reason"] for e in b["events"]] == ["Pulled", "BackOff"]  # time-sorted
    assert "FATAL" in b["logs"]["previous"]
    assert "errors" not in b
    assert core.namespaces_touched == {"demo"}


def test_previous_logs_are_redacted():
    b = DiagnosticCollector("demo", FakeCore(), FakeApps()).collect(_failure())
    assert "hunter2" not in b["logs"]["previous"]
    assert "password=[REDACTED]" in b["logs"]["previous"]


def test_one_failing_section_does_not_break_bundle():
    b = DiagnosticCollector("demo", FakeCore(fail_previous=500), FakeApps()).collect(_failure())
    assert b["errors"] == {"logs.previous": "500 x"}
    assert b["logs"]["current"] == "starting\n"
    assert b["owners"]


def test_previous_logs_400_is_benign_not_an_error():
    b = DiagnosticCollector("demo", FakeCore(fail_previous=400), FakeApps()).collect(_failure())
    assert "errors" not in b
    assert b["logs"]["previous"] is None


def test_logs_returned_as_bytes_repr_are_decoded():
    # regression: client returned "b'FATAL...\\n'" as a str
    b = DiagnosticCollector("demo", FakeCore(log_style="repr"), FakeApps()).collect(_failure())
    assert b["logs"]["current"] == "starting\n"
    assert "FATAL" in b["logs"]["previous"] and not b["logs"]["previous"].startswith("b'")


def test_plain_str_logs_still_work():
    b = DiagnosticCollector("demo", FakeCore(log_style="str"), FakeApps()).collect(_failure())
    assert b["logs"]["current"] == "starting\n"


def test_unable_to_retrieve_logs_message_treated_as_missing():
    core = FakeCore(previous_text="unable to retrieve container logs for containerd://abc123")
    b = DiagnosticCollector("demo", core, FakeApps()).collect(_failure())
    assert b["logs"]["previous"] is None


def test_no_previous_logs_at_restart_zero_even_if_terminated():
    # regression: Error at restarts=0 asked for previous logs -> 400
    core = FakeCore(live_restarts=0)
    DiagnosticCollector("demo", core, FakeApps()).collect(
        _failure(reason="Error", restarts=0, last_termination_reason="Error"))
    assert core.log_calls == [False]


def test_image_pull_skips_logs():
    core = FakeCore()
    b = DiagnosticCollector("demo", core, FakeApps()).collect(
        _failure(reason="ImagePullBackOff", restarts=0, exit_code=None, last_termination_reason=None))
    assert "skipped" in b["logs"]
    assert core.log_calls == []


def test_no_previous_logs_on_first_failure():
    core = FakeCore(live_restarts=0)
    DiagnosticCollector("demo", core, FakeApps()).collect(
        _failure(restarts=0, last_termination_reason=None))
    assert core.log_calls == [False]


def test_stale_restart_count_uses_live_pod_for_previous_logs():
    # regression: watch said Error/restarts=0, but the kubelet had already started
    # attempt #2 -> the FATAL line was only in the previous container's logs.
    core = FakeCore(live_restarts=1, current_text="",
                    previous_text="FATAL: cannot connect to database at db:5432\n")
    b = DiagnosticCollector("demo", core, FakeApps()).collect(
        _failure(reason="Error", restarts=0, last_termination_reason="Error"))
    assert core.log_calls == [False, True]
    assert "db:5432" in b["logs"]["previous"]
    assert any(d["host"] == "db" for d in b.get("dependencies") or [])


def test_empty_current_logs_fall_back_to_previous():
    core = FakeCore(live_restarts=0, current_text="",
                    previous_text="FATAL: cannot connect to database at db:5432\n")
    b = DiagnosticCollector("demo", core, FakeApps()).collect(
        _failure(reason="Error", restarts=0, last_termination_reason="Error"))
    assert core.log_calls == [False, True]
    assert "db:5432" in b["logs"]["previous"]


def test_refuses_failure_from_other_namespace():
    c = DiagnosticCollector("payments", FakeCore(), FakeApps())
    try:
        c.collect(_failure(namespace="orders"))
    except ValueError:
        return
    raise AssertionError("collector accepted a failure from another namespace")


def test_format_evidence_text():
    b = DiagnosticCollector("demo", FakeCore(), FakeApps()).collect(_failure())
    out = format_evidence(b)
    for expected in ("Reason    : CrashLoopBackOff", "Image     : busybox:1.36",
                     "limits[cpu=50m, memory=32Mi]",
                     "Deployment/broken-app <- ReplicaSet/broken-app-66ff",
                     "Replicas  : 0/1 available", "Services  : broken-app",
                     "BackOff", "Prev logs", "FATAL"):
        assert expected in out, expected


def test_redact_patterns():
    assert redact("token: abc123") == "token: [REDACTED]"
    assert redact("Authorization: Bearer eyJabc") == "Authorization: Bearer [REDACTED]"
    assert redact("postgres://user:s3cret@db:5432/x") == "postgres://user:[REDACTED]@db:5432/x"
    assert redact("nothing here") == "nothing here"
