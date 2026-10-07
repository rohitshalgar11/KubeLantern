"""Up-front dependency checks: host extraction, scoping, and Service lookups."""

from types import SimpleNamespace as NS

from agent.collector.collector import DiagnosticCollector, describe_dependency
from agent.collector.dependencies import HostRef, check_dependencies, extract_hosts, scope_of
from agent.watcher.detector import Failure


def test_extract_from_real_crashloop_log():
    assert extract_hosts("FATAL: cannot connect to database at db:5432") == [HostRef("db", 5432)]


def test_extract_urls_and_userinfo():
    refs = extract_hosts("connecting to postgres://app:s3cret@orders-db.demo.svc:5432/orders")
    assert refs == [HostRef("orders-db.demo.svc", 5432)]
    assert extract_hosts("GET http://inventory/api failed") == [HostRef("inventory", None)]


def test_extract_go_dns_error():
    log = 'dial tcp: lookup redis on 10.96.0.10:53: no such host'
    refs = extract_hosts(log)
    assert HostRef("redis", None) in refs


def test_extract_ignores_timestamps_and_noise():
    assert extract_hosts("2026-10-05 14:43:12 INFO started at 12:30:45") == []
    assert extract_hosts("") == [] and extract_hosts(None) == []


def test_extract_caps_number_of_hosts():
    log = " ".join(f"svc{i}:80" for i in range(20))
    assert len(extract_hosts(log)) == 5


def test_scope_of():
    assert scope_of("db", "demo") == ("namespace", "db")
    assert scope_of("db.demo", "demo") == ("namespace", "db")
    assert scope_of("db.demo.svc.cluster.local", "demo") == ("namespace", "db")
    assert scope_of("db.payments.svc", "demo") == ("other-namespace", "db")
    assert scope_of("api.stripe.com", "demo") == ("external", None)
    assert scope_of("localhost", "demo") == ("localhost", None)
    assert scope_of("10.0.0.7", "demo") == ("ip", None)


def svc(name, ports, selector=None):
    return NS(metadata=NS(name=name),
              spec=NS(ports=[NS(port=p, target_port=p, protocol="TCP") for p in ports],
                      selector=selector or {"app": name}, type="ClusterIP"))


def slices(ready_counts):
    return [NS(endpoints=[NS(addresses=["10.0.0.1"], conditions=NS(ready=r)) for r in ready_counts])]


def test_missing_service_is_reported():
    [r] = check_dependencies("cannot connect to database at db:5432", "demo",
                             [svc("broken-app", [80])], lambda n: [])
    assert r == {"host": "db", "port": 5432, "scope": "namespace", "service": "db",
                 "service_exists": False}
    assert describe_dependency(r) == "db:5432 — Service 'db' NOT FOUND in namespace"


def test_service_exists_wrong_port_no_endpoints():
    [r] = check_dependencies("db:5432 refused", "demo", [svc("db", [3306])], lambda n: slices([]))
    assert r["service_exists"] and r["port_exposed"] is False and r["ready_endpoints"] == 0
    assert "port 5432 NOT exposed" in describe_dependency(r)


def test_service_healthy_counts_ready_endpoints_only():
    [r] = check_dependencies("db:5432 timeout", "demo", [svc("db", [5432])],
                             lambda n: slices([True, False, True]))
    assert r["port_exposed"] is True and r["ready_endpoints"] == 2


def test_other_namespace_and_external_not_checked():
    calls = []
    out = check_dependencies("db.payments.svc:5432 and api.stripe.com:443", "demo", [],
                             lambda n: calls.append(n) or [])
    assert [r["scope"] for r in out] == ["other-namespace", "external"]
    assert calls == [] and all("service_exists" not in r for r in out)


def test_endpoint_lookup_failure_is_recorded_not_raised():
    class Forbidden(Exception):
        reason = "Forbidden"

    def boom(name):
        raise Forbidden()

    [r] = check_dependencies("db:5432", "demo", [svc("db", [5432])], boom)
    assert r["endpoints_error"] == "Forbidden"


# -- collector integration -------------------------------------------------------

class Core:
    def read_namespaced_pod(self, name, ns):
        c = NS(name="app", image="busybox", command=None, args=None, resources=None)
        return NS(metadata=NS(name=name, namespace=ns, labels={"app": "app"}, owner_references=None),
                  spec=NS(node_name="n", containers=[c], init_containers=None,
                          image_pull_secrets=[NS(name="regcred")]),
                  status=NS(phase="Running", qos_class="BestEffort", start_time=None, conditions=[],
                            container_statuses=[], init_container_statuses=None))

    def read_namespaced_pod_log(self, *a, **kw):
        return NS(data=b"FATAL: cannot connect to database at db:5432\n")

    def list_namespaced_event(self, ns, field_selector):
        return NS(items=[])

    def list_namespaced_service(self, ns):
        return NS(items=[svc("app", [80])])


class Discovery:
    def __init__(self):
        self.calls = []

    def list_namespaced_endpoint_slice(self, ns, label_selector):
        self.calls.append((ns, label_selector))
        return NS(items=[])


def test_collector_adds_dependency_checks():
    d = Discovery()
    b = DiagnosticCollector("demo", Core(), object(), d).collect(
        Failure("demo", "app-1", "app", "Error", 0, 1))
    assert b["dependencies"] == [{"host": "db", "port": 5432, "scope": "namespace",
                                  "service": "db", "service_exists": False}]
    assert b["pod"]["image_pull_secrets"] == ["regcred"]
    assert d.calls == []  # no Service -> no endpoint lookup needed


def test_collector_text_shows_dependency_line():
    from agent.collector.collector import format_evidence

    b = DiagnosticCollector("demo", Core(), object(), Discovery()).collect(
        Failure("demo", "app-1", "app", "Error", 0, 1))
    assert "Depends on: db:5432 — Service 'db' NOT FOUND in namespace" in format_evidence(b)
