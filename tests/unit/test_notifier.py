"""Notifications: message formats, delivery, the sidecar API and the agent hooks."""

import io
import json
import queue
import threading
import urllib.request
from contextlib import redirect_stdout
from http.server import ThreadingHTTPServer

from agent.incident.manager import IncidentManager
from agent.main import Agent
from agent.notify import NotifyClient
from agent.watcher.detector import Failure
from notifier import formats
from notifier.sender import Sender, SendError
from notifier.server import make_handler, validate

INCIDENT = {
    "id": "payments-api-INC4f2a91", "namespace": "payments", "workload": "Deployment/api",
    "container": "api", "cause": "crash (exit 1)", "status": "open",
    "cause_history": ["crash (exit 1)"], "failing_pods": {"api-1": 3, "api-2": 2},
    "opened_at": 1000.0, "resolved_at": None,
}
DIAG = {"category": "dependency", "confidence": "high",
        "summary": "Cannot reach db; password=hunter2 seen in logs",
        "probableCause": "Service 'db' not found", "suggestedFix": "Deploy db",
        "nextSteps": ["kubectl apply -f db.yaml", "check endpoints", "check port", "four"],
        "escalation": "payments team — Teams 'Payments On-call'",
        "runbooks": ["payments/db"], "model": "qwen2.5:1.5b"}
EVENT = {"kind": "diagnosis", "incident": INCIDENT, "diagnosis": DIAG,
         "evidence": ["Logs: FATAL token=abc123secret", "Depends on: db:5432 NOT FOUND"]}


# -- formats ----------------------------------------------------------------------------

def _card(payload):
    [att] = payload["attachments"]
    assert payload["type"] == "message"
    assert att["contentType"] == "application/vnd.microsoft.card.adaptive"
    card = att["content"]
    assert card["type"] == "AdaptiveCard" and card["version"] == "1.4"
    return card


def _text(obj):
    return json.dumps(obj, ensure_ascii=False)


def test_teams_card_is_a_workflows_adaptive_card_with_the_essentials():
    card = _card(formats.teams(EVENT))
    text = _text(card)
    assert "Incident diagnosed: Deployment/api in payments" in text
    assert "payments-api-INC4f2a91" in text and "dependency (high confidence)" in text
    assert "kubectl apply -f db.yaml" in text and "Payments On-call" in text


def test_summary_detail_keeps_logs_and_evidence_in_the_cluster():
    for fmt in (formats.teams, formats.slack, formats.webhook):
        text = _text(fmt(EVENT, "summary"))
        assert "FATAL" not in text and "Depends on" not in text
        assert "four" not in text                          # at most 3 next steps


def test_full_detail_adds_evidence_runbooks_and_fix():
    text = _text(formats.teams(EVENT, "full"))
    assert "Depends on: db:5432 NOT FOUND" in text and "payments/db" in text
    assert "Deploy db" in text and "four" in text


def test_secrets_are_redacted_before_leaving_the_cluster():
    for fmt in (formats.teams, formats.slack, formats.webhook):
        text = _text(fmt(EVENT, "full"))
        assert "hunter2" not in text and "abc123secret" not in text


def test_resolved_card_is_green_with_duration():
    ev = {"kind": "resolved",
          "incident": {**INCIDENT, "status": "resolved", "resolved_at": 1000.0 + 754}}
    card = _card(formats.teams(ev))
    assert card["body"][0]["color"] == "good"
    assert "12m34s" in _text(card)


def test_cause_change_is_shown_on_the_diagnosis():
    ev = {**EVENT, "incident": {**INCIDENT, "cause": "oom (exit 137)",
                                "cause_history": ["crash (exit 1)", "oom (exit 137)"]}}
    assert "crash (exit 1) → oom (exit 137)" in _text(formats.teams(ev))


def test_slack_and_webhook_shapes():
    s = formats.slack(EVENT)
    assert s["text"].startswith("Incident diagnosed") and s["blocks"]
    w = formats.webhook(EVENT)
    assert w["source"] == "kubelantern" and w["event"] == "diagnosis"
    assert w["incident"]["failingPods"] == 2 and w["diagnosis"]["category"] == "dependency"


def test_huge_input_stays_within_teams_card_limits():
    big = {**DIAG, "summary": "x" * 50_000, "nextSteps": ["y" * 5000] * 10}
    ev = {**EVENT, "diagnosis": big, "evidence": ["z" * 5000] * 200}
    assert len(json.dumps(formats.teams(ev, "full"))) < 28_000


# -- delivery ---------------------------------------------------------------------------

def _sender(tmp_path, posts, **kw):
    for name, url in kw.pop("urls", {"teams": "https://example.webhook.office.com/x"}).items():
        (tmp_path / name).write_text(url + "\n")
    slept = []
    s = Sender(tmp_path, kw.pop("channels", ["teams"]), post=lambda url, p: posts(url, p),
               sleep=slept.append, **kw)
    return s, slept


def test_sends_to_every_configured_channel(tmp_path):
    got = []
    s, _ = _sender(tmp_path, lambda u, p: got.append(u) or 200, channels=["teams", "slack"],
                   urls={"teams": "https://t.example/1", "slack": "https://hooks.slack.com/2"})
    assert s.send(EVENT) == {"teams": "sent", "slack": "sent"}
    assert got == ["https://t.example/1", "https://hooks.slack.com/2"]


def test_http_urls_are_refused_unless_allowed(tmp_path):
    s, _ = _sender(tmp_path, lambda u, p: 200, urls={"teams": "http://sink:8080/t"})
    assert s.send(EVENT) == {"teams": "not-configured"}
    s.allow_insecure = True
    assert s.send(EVENT) == {"teams": "sent"}


def test_missing_secret_key_is_skipped(tmp_path):
    s, _ = _sender(tmp_path, lambda u, p: 200, urls={}, channels=["teams"])
    assert s.send(EVENT) == {"teams": "not-configured"}


def test_retries_honour_retry_after_then_succeed(tmp_path):
    calls = []

    def post(u, p):
        calls.append(1)
        if len(calls) < 3:
            raise SendError("HTTP 429", 429, retry_after=7)
        return 200

    s, slept = _sender(tmp_path, post)
    assert s.send(EVENT) == {"teams": "sent"}
    assert slept == [7, 7]


def test_client_errors_are_not_retried(tmp_path):
    calls = []

    def post(u, p):
        calls.append(1)
        raise SendError("HTTP 400", 400)

    s, _ = _sender(tmp_path, post)
    assert s.send(EVENT) == {"teams": "failed"} and len(calls) == 1


def test_rate_limit_drops_excess_messages(tmp_path):
    s, _ = _sender(tmp_path, lambda u, p: 200, max_per_minute=2)
    results = [s.send(EVENT)["teams"] for _ in range(3)]
    assert results == ["sent", "sent", "rate-limited"]


def test_rotated_secret_is_picked_up_without_restart(tmp_path):
    got = []
    s, _ = _sender(tmp_path, lambda u, p: got.append(u) or 200)
    s.send(EVENT)
    (tmp_path / "teams").write_text("https://new.example/rotated")
    s.send(EVENT)
    assert got[-1] == "https://new.example/rotated"


# -- sidecar API --------------------------------------------------------------------------

def test_validate_rejects_bad_events():
    assert validate({"kind": "nope", "incident": {"id": "x"}})
    assert validate({"kind": "diagnosis", "incident": {}})
    assert validate(["not", "a", "dict"])
    assert validate({"kind": "diagnosis", "incident": {"id": "x"}}) is None


def test_sidecar_accepts_events_on_localhost_only():
    q: queue.Queue = queue.Queue()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(q))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        req = urllib.request.Request(url + "/v1/notify", data=json.dumps(EVENT).encode(),
                                     method="POST", headers={"Content-Type": "application/json"})
        assert urllib.request.urlopen(req, timeout=5).status == 202
        assert q.get(timeout=2)["incident"]["id"] == INCIDENT["id"]
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.shutdown()


# -- agent hooks ----------------------------------------------------------------------------

def F(reason="Error", restarts=1, exit_code=1, pod="api-1"):
    return Failure(namespace="payments", pod=pod, container="api", reason=reason,
                   restarts=restarts, exit_code=exit_code, last_termination_reason=None,
                   workload_kind="Deployment", workload_name="api")


class _Collector:
    def collect(self, f):
        return {"failure": {"pod": f.pod, "reason": f.reason, "restarts": f.restarts,
                            "container": f.container, "namespace": f.namespace},
                "logs": {"current": "FATAL x\n"}}


def _agent(events=("diagnosis", "resolved"), detail="summary"):
    sent = []
    client = NotifyClient("http://127.0.0.1:1/v1/notify", set(events) | {"diagnosis_failed"},
                          detail=detail, post=lambda url, ev: sent.append(ev) or 202)
    a = Agent("payments", IncidentManager(resolve_after_seconds=60), _Collector(),
              notifier=client)
    return a, client, sent


def test_default_events_post_once_per_diagnosis_and_resolution():
    a, client, sent = _agent()
    with redirect_stdout(io.StringIO()):
        a.on_failure(F())
        a.on_diagnosis(a.manager.open_incidents()[0],
                       {"diagnosis": {"category": "dependency", "confidence": "high",
                                      "probable_cause": "db missing", "next_steps": ["fix"]}})
        opened_at = a.manager.open_incidents()[0]["opened_at"]
        a.manager.clear("payments", "api-1", "api", opened_at + 10)
        for u in a.manager.tick(opened_at + 100):     # healthy for > 60s -> RESOLVED
            a.emit(u)
    client.flush()
    kinds = [e["kind"] for e in sent]
    assert "opened" not in kinds                       # diagnosis follows within a minute
    assert kinds == ["diagnosis", "resolved"]
    d = next(e for e in sent if e["kind"] == "diagnosis")
    assert d["diagnosis"]["category"] == "dependency" and d["evidence"] is None


def test_full_detail_sends_evidence_with_the_diagnosis():
    a, client, sent = _agent(detail="full")
    with redirect_stdout(io.StringIO()):
        a.on_failure(F())
        a.on_diagnosis(a.manager.open_incidents()[0], {"diagnosis": {"category": "dependency"}})
    client.flush()
    [d] = [e for e in sent if e["kind"] == "diagnosis"]
    assert any("FATAL x" in line for line in d["evidence"])


def test_failed_diagnosis_is_still_announced():
    a, client, sent = _agent()
    with redirect_stdout(io.StringIO()):
        a.on_failure(F())
        a.on_diagnosis_error(a.manager.open_incidents()[0], "gateway unreachable")
    client.flush()
    assert [e["kind"] for e in sent] == ["diagnosis_failed"]


def test_unreachable_sidecar_never_blocks_or_raises():
    def post(url, ev):
        raise OSError("connection refused")

    client = NotifyClient("http://127.0.0.1:1/v1/notify", {"resolved"}, post=post,
                          sleep=lambda s: None)
    client.notify("resolved", INCIDENT)
    client.flush()                                    # logs and drops; no exception


# -- email channel (e.g. a Teams channel's email address) -----------------------------------

from notifier.mail import EmailConfig, build_message, smtp_send

CFG = EmailConfig(host="smtp.example.com", port=587, sender="kubelantern@example.com",
                  to=["abc123.tenant.onmicrosoft.com@emea.teams.ms"])


def test_email_message_has_subject_text_and_html():
    subject, text, html = formats.email_message(EVENT)
    assert subject == "[KubeLantern] Incident diagnosed: Deployment/api in payments (dependency)"
    assert "Service 'db' not found" in text and "<table" in html
    assert "hunter2" not in text + html                 # redacted
    msg = build_message(CFG, subject, text, html)
    assert msg["To"] == CFG.to[0] and msg.is_multipart()


def test_email_html_is_escaped():
    ev = {**EVENT, "diagnosis": {**DIAG, "summary": "<script>alert(1)</script>"}}
    _, _, html = formats.email_message(ev)
    assert "<script>" not in html and "&lt;script&gt;" in html


def test_email_config_problems_are_reported():
    assert EmailConfig(host="").problem()
    assert EmailConfig(host="h", sender="nope", to=["a@b"]).problem()
    assert EmailConfig(host="h", sender="a@b", to=[]).problem()
    assert EmailConfig(host="h", sender="a@b", to=["c@d"], tls="plain").problem()
    assert CFG.problem() is None


def _email_sender(tmp_path, mail, **kw):
    (tmp_path / "smtp-username").write_text("user\n")
    (tmp_path / "smtp-password").write_text("s3cret\n")
    return Sender(tmp_path, ["email"], email=kw.pop("email", CFG), mail=mail,
                  sleep=lambda s: None, **kw)


def test_email_is_sent_with_credentials_from_the_secret(tmp_path):
    got = []
    s = _email_sender(tmp_path, lambda cfg, msg, u, p: got.append((msg["Subject"], u, p)))
    assert s.send(EVENT) == {"email": "sent"}
    assert got == [("[KubeLantern] Incident diagnosed: Deployment/api in payments (dependency)",
                    "user", "s3cret")]


def test_email_without_tls_is_refused_unless_insecure_allowed(tmp_path):
    plain = EmailConfig(host="sink", port=2525, sender="a@b.c", to=["x@y.z"], tls="none")
    s = _email_sender(tmp_path, lambda *a: None, email=plain)
    assert s.send(EVENT) == {"email": "not-configured"}
    s.allow_insecure = True
    assert s.send(EVENT) == {"email": "sent"}


def test_email_auth_failure_is_not_retried(tmp_path):
    calls = []

    def mail(*a):
        calls.append(1)
        raise SendError("SMTP refused", status=400)

    assert _email_sender(tmp_path, mail).send(EVENT) == {"email": "failed"} and len(calls) == 1


def test_smtp_send_uses_starttls_and_login():
    log = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            log.append(("connect", host, port))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            log.append(("quit",))

        def ehlo(self):
            log.append(("ehlo",))

        def starttls(self, context):
            log.append(("starttls",))

        def login(self, u, p):
            log.append(("login", u))

        def send_message(self, msg):
            log.append(("send", msg["To"]))

    smtp_send(CFG, build_message(CFG, "s", "t", "<p>h</p>"), "user", "pw", smtp_factory=FakeSMTP)
    steps = [x[0] for x in log]
    assert steps == ["connect", "ehlo", "starttls", "ehlo", "login", "send", "quit"]
