"""Message formats per channel.

An *event* (sent by the agent) looks like:

    {
      "kind": "diagnosis",            # see notifier.KINDS
      "incident": {...},              # IncidentManager.to_dict()
      "diagnosis": {...} | None,      # category, confidence, summary, probableCause,
                                      # nextSteps, suggestedFix, escalation, runbooks, model
      "previous_cause": str | None,
      "previous_pod_count": int | None,
      "evidence": [str] | None,       # only sent when detail = "full"
      "error": str | None,
    }

`detail = "summary"` (default) sends what an on-call engineer needs to act;
`"full"` adds evidence and log lines (already redacted). Everything is redacted
again here before it leaves the cluster.
"""

from __future__ import annotations

import time
from typing import Any

from kubelantern_common.redaction import redact

TITLES = {
    "opened": "Incident opened",
    "diagnosis": "Incident diagnosed",
    "diagnosis_failed": "Incident opened (diagnosis unavailable)",
    "cause_changed": "Incident cause changed",
    "scope_changed": "Incident spreading",
    "ongoing": "Incident still failing",
    "resolved": "Incident resolved",
}
# Adaptive Card colours: attention = red, warning = amber, good = green
TEAMS_COLOR = {"resolved": "good", "ongoing": "warning", "scope_changed": "warning"}
SLACK_EMOJI = {"resolved": ":white_check_mark:", "ongoing": ":hourglass:",
               "scope_changed": ":warning:"}

MAX_TEXT = 1200
MAX_STEPS_SUMMARY = 3
MAX_EVIDENCE = 25


def _clean(v: Any, limit: int = MAX_TEXT) -> str:
    s = redact(str(v)) or ""
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _duration(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def title(event: dict) -> str:
    inc = event["incident"]
    return f"{TITLES.get(event['kind'], 'Incident update')}: {inc.get('workload')} in {inc.get('namespace')}"


def facts(event: dict) -> list[tuple[str, str]]:
    inc, kind = event["incident"], event["kind"]
    d = event.get("diagnosis") or {}
    history = inc.get("cause_history") or []
    if kind == "cause_changed" and event.get("previous_cause"):
        cause = f"{event['previous_cause']} → {inc.get('cause')}"
    elif len(history) > 1:
        cause = " → ".join(history[-3:])
    else:
        cause = str(inc.get("cause"))
    out = [("Namespace", inc.get("namespace")), ("Workload", inc.get("workload")),
           ("Container", inc.get("container")), ("Cause", cause)]
    if kind == "resolved":
        if inc.get("resolved_at") and inc.get("opened_at"):
            out.append(("Duration", _duration(inc["resolved_at"] - inc["opened_at"])))
    else:
        failing = len(inc.get("failing_pods") or {})
        if kind == "scope_changed" and event.get("previous_pod_count") is not None:
            out.append(("Pods failing", f"{event['previous_pod_count']} → {failing}"))
        elif failing:
            out.append(("Pods failing", str(failing)))
    if d.get("category"):
        out.append(("Category", f"{d['category']} ({d.get('confidence', '?')} confidence)"))
    out.append(("Incident", inc.get("id")))
    return [(k, _clean(v, 300)) for k, v in out if v not in (None, "")]


def sections(event: dict, detail: str) -> dict[str, Any]:
    """Text blocks shared by all formats."""
    d = event.get("diagnosis") or {}
    steps = list(d.get("nextSteps") or [])
    if detail != "full":
        steps = steps[:MAX_STEPS_SUMMARY]
    out: dict[str, Any] = {
        "summary": _clean(d["summary"]) if d.get("summary") else None,
        "cause": _clean(d["probableCause"]) if d.get("probableCause") else None,
        "steps": [_clean(s, 500) for s in steps],
        "escalation": _clean(d["escalation"], 300) if d.get("escalation") else None,
        "error": _clean(event["error"], 300) if event.get("error") else None,
        "fix": None, "runbooks": [], "evidence": [],
    }
    if detail == "full":
        out["fix"] = _clean(d["suggestedFix"]) if d.get("suggestedFix") else None
        out["runbooks"] = [_clean(r, 200) for r in d.get("runbooks") or []]
        out["evidence"] = [_clean(e, 300) for e in (event.get("evidence") or [])[:MAX_EVIDENCE]]
    return out


# -- Microsoft Teams (Workflows: "Post to a channel when a webhook request is received")

def teams(event: dict, detail: str = "summary") -> dict:
    s = sections(event, detail)
    body: list[dict] = [
        {"type": "TextBlock", "text": title(event), "weight": "Bolder", "size": "Medium",
         "wrap": True, "color": TEAMS_COLOR.get(event["kind"], "attention")},
        {"type": "FactSet", "facts": [{"title": k, "value": v} for k, v in facts(event)]},
    ]

    def block(heading: str, text: str):
        body.append({"type": "TextBlock", "text": heading, "weight": "Bolder", "wrap": True,
                     "spacing": "Medium"})
        body.append({"type": "TextBlock", "text": text, "wrap": True})

    if s["summary"]:
        block("Summary", s["summary"])
    if s["cause"]:
        block("Probable cause", s["cause"])
    if s["steps"]:
        block("Next steps", "\n".join(f"{i}. {x}" for i, x in enumerate(s["steps"], 1)))
    if s["fix"]:
        block("Suggested fix", s["fix"])
    if s["escalation"]:
        block("Escalate", s["escalation"])
    if s["error"]:
        block("Diagnosis unavailable", s["error"])
    if s["runbooks"]:
        block("Runbooks", ", ".join(s["runbooks"]))
    if s["evidence"]:
        body.append({"type": "TextBlock", "text": "Evidence", "weight": "Bolder",
                     "spacing": "Medium"})
        body.append({"type": "TextBlock", "text": "\n".join(s["evidence"]), "wrap": True,
                     "fontType": "Monospace", "size": "Small"})
    body.append({"type": "TextBlock", "text": "KubeLantern · " + time.strftime(
        "%Y-%m-%d %H:%M UTC", time.gmtime()), "isSubtle": True, "size": "Small",
        "spacing": "Medium"})
    return {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "contentUrl": None,
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard",
                "version": "1.4",
                "msteams": {"width": "Full"},
                "body": body,
            },
        }],
    }


# -- Slack (incoming webhook) ------------------------------------------------------

def slack(event: dict, detail: str = "summary") -> dict:
    s = sections(event, detail)
    emoji = SLACK_EMOJI.get(event["kind"], ":rotating_light:")
    head = f"{emoji} *{title(event)}*"
    lines = [head, "\n".join(f"*{k}:* {v}" for k, v in facts(event))]
    if s["summary"]:
        lines.append(f"*Summary:* {s['summary']}")
    if s["cause"]:
        lines.append(f"*Probable cause:* {s['cause']}")
    if s["steps"]:
        lines.append("*Next steps:*\n" + "\n".join(f"{i}. {x}" for i, x in enumerate(s["steps"], 1)))
    if s["fix"]:
        lines.append(f"*Suggested fix:* {s['fix']}")
    if s["escalation"]:
        lines.append(f"*Escalate:* {s['escalation']}")
    if s["error"]:
        lines.append(f"*Diagnosis unavailable:* {s['error']}")
    if s["runbooks"]:
        lines.append("*Runbooks:* " + ", ".join(s["runbooks"]))
    if s["evidence"]:
        lines.append("*Evidence:*\n```" + "\n".join(s["evidence"]) + "```")
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": part[:2900]}}
              for part in lines]
    return {"text": f"{title(event)}", "blocks": blocks}


# -- generic webhook (JSON for your own tooling) -----------------------------------

def webhook(event: dict, detail: str = "summary") -> dict:
    s = sections(event, detail)
    inc = event["incident"]
    return {
        "source": "kubelantern",
        "schemaVersion": 1,
        "event": event["kind"],
        "title": title(event),
        "incident": {
            "id": inc.get("id"),
            "namespace": inc.get("namespace"),
            "workload": inc.get("workload"),
            "container": inc.get("container"),
            "cause": inc.get("cause"),
            "causeHistory": inc.get("cause_history") or [],
            "status": inc.get("status"),
            "failingPods": len(inc.get("failing_pods") or {}),
            "openedAt": inc.get("opened_at"),
            "resolvedAt": inc.get("resolved_at"),
        },
        "previousCause": event.get("previous_cause"),
        "diagnosis": {
            "category": (event.get("diagnosis") or {}).get("category"),
            "confidence": (event.get("diagnosis") or {}).get("confidence"),
            "summary": s["summary"],
            "probableCause": s["cause"],
            "nextSteps": s["steps"],
            "suggestedFix": s["fix"],
            "escalation": s["escalation"],
            "runbooks": s["runbooks"],
        } if event.get("diagnosis") else None,
        "error": s["error"],
        "evidence": s["evidence"] or None,
    }


FORMATS = {"teams": teams, "slack": slack, "webhook": webhook}
