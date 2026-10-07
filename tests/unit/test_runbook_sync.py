"""Agent runbook sync: change detection, CRD-missing tolerance, and an
end-to-end run through the real gateway HTTP server."""

import tempfile
import threading
from pathlib import Path

from agent.diagnosis.runbooks import RunbookSync, digest, to_payload
from gateway.server import serve
from tests.unit.test_knowledge import DEMO_RB, QUERY, gateway, kb


def cr(name, title="broken-app database dependency", content=DEMO_RB["content"], **spec):
    return {"metadata": {"name": name, "namespace": "demo"},
            "spec": {"title": title, "content": content, **spec}}


class CustomApi:
    def __init__(self, items=None, status=None):
        self.items = items or []
        self.status = status
        self.calls = 0

    def list_namespaced_custom_object(self, group, version, ns, plural):
        self.calls += 1
        assert (group, version, ns, plural) == ("kubelantern.io", "v1alpha1", "demo", "runbooks")
        if self.status:
            e = Exception("err")
            e.status = self.status
            raise e
        return {"items": self.items}


def token_file(tok="demo-agent"):
    p = Path(tempfile.mkdtemp()) / "token"
    p.write_text(tok)
    return str(p)


def test_payload_is_stable_and_sorted():
    a = to_payload([cr("b"), cr("a", category="dependency", workloads=["x"])])
    assert [r["name"] for r in a] == ["a", "b"] and a[0]["workloads"] == ["x"]
    assert digest(a) == digest(to_payload([cr("a", category="dependency", workloads=["x"]), cr("b")]))


def test_pushes_only_on_change_or_periodic_resync():
    now = [0.0]
    pushed = []
    s = RunbookSync("demo", "http://gw", token_file(), custom_api=CustomApi([cr("a")]),
                    resync_every=600, clock=lambda: now[0])
    s.push = lambda payload: pushed.append(payload) or {"runbooks": len(payload), "chunks": 1}
    assert s.sync_once() == "pushed"
    now[0] = 30
    assert s.sync_once() == "unchanged"
    s.api.items.append(cr("b"))
    assert s.sync_once() == "pushed"
    now[0] = 700
    assert s.sync_once() == "pushed"  # periodic re-push for convergence
    assert len(pushed) == 3


def test_crd_not_installed_disables_quietly():
    s = RunbookSync("demo", "http://gw", token_file(), custom_api=CustomApi(status=404))
    assert s.sync_once() == "disabled" and s.sync_once() == "disabled"


def test_forbidden_list_is_reported():
    s = RunbookSync("demo", "http://gw", token_file(), custom_api=CustomApi(status=403))
    assert s.sync_once().startswith("error: list failed (403")


def test_end_to_end_agent_sync_through_gateway_http():
    k = kb()
    gw = gateway(k)
    httpd = serve(gw, lambda: (True, "ok"), "127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        api = CustomApi([cr("broken-app-db", workloads=["broken-app"], category="dependency")])
        s = RunbookSync("demo", url, token_file("demo-agent"), custom_api=api)
        assert s.sync_once() == "pushed"
        refs = k.retrieve(QUERY, "demo", "dependency", workload="broken-app")
        assert "demo/broken-app-db" in [r["source"] for r in refs]

        # the same CRs pushed with the payments token land in payments, never demo
        s2 = RunbookSync("demo", url, token_file("payments-agent"),
                         custom_api=CustomApi([cr("other-rb")]))
        assert s2.sync_once() == "pushed"
        assert "demo/other-rb" not in [r["source"] for r in k.retrieve(QUERY, "demo", limit=10)]

        # a non-agent token is refused
        s3 = RunbookSync("demo", url, token_file("demo-default"), custom_api=CustomApi([cr("x1")]))
        assert s3.sync_once().startswith("error: gateway 403")
    finally:
        httpd.shutdown()


def test_unreachable_gateway_is_an_error_not_a_crash():
    s = RunbookSync("demo", "http://127.0.0.1:1", token_file(), custom_api=CustomApi([cr("a")]))
    assert s.sync_once().startswith("error: gateway unreachable")
    assert s.last_digest is None  # will retry next interval
