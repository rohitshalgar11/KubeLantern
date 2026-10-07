"""Deterministic knowledge used by the diagnosis graph.

* classify(): rule-based category from the incident cause, events and logs.
  Obvious cases (OOM, image pull, missing dependency) never depend on the
  model getting them right.
* verified_facts(): plain-language statements derived from evidence the
  agent collected. These are facts, not guesses — the model is told to trust
  them, and validation checks the answer against them.
* fallback_fields(): rule-based text used when the model's answer is
  missing a field or fails validation twice.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_DEP_PATTERNS = re.compile(
    r"connection refused|cannot connect|could not connect|can't connect|unable to connect|"
    r"failed to connect|connect: connection|econnrefused|no such host|name or service not known|"
    r"could not translate host name|getaddrinfo|temporary failure in name resolution|"
    r"dial tcp|i/o timeout|connection timed out|connection reset|host unreachable|"
    r"no route to host|upstream connect error|"
    # timeouts reaching something over the network (wget, curl, HTTP clients, Go)
    r"(?:connection|connect|download|request|read|operation|handshake) timed out|"
    r"context deadline exceeded|timeout (?:awaiting|waiting for|while connecting)",
    re.IGNORECASE,
)
_PERM_PATTERNS = re.compile(
    r"permission denied|access denied|forbidden|unauthori[sz]ed|eacces|operation not permitted|"
    r"read-only file system",
    re.IGNORECASE,
)
_CONFIG_PATTERNS = re.compile(
    r"missing (?:required )?(?:env|environment|config|configuration|variable|argument)|"
    r"(?:env|environment variable) .{0,40}(?:not set|is required|missing)|keyerror|"
    r"invalid (?:config|configuration|value|argument)|no such file or directory|"
    r"unknown (?:flag|option|argument)|failed to (?:load|parse) config",
    re.IGNORECASE,
)
_PROBE = re.compile(r"(liveness|readiness|startup) probe failed", re.IGNORECASE)
# Signatures of a code-level crash (any language): strong evidence of an app bug.
_APP_CRASH = re.compile(
    r"^panic: |goroutine \d+ \[running\]|Traceback \(most recent call last\)|"
    r"Exception in thread|\b\w+(?:Exception|Error): .+\n\s+at |NullPointerException|"
    r"segmentation fault|SIGSEGV|core dumped|unhandled (?:exception|rejection)|"
    r"fatal error: |stack overflow|assertion failed",
    re.IGNORECASE | re.MULTILINE,
)


@dataclass
class Classification:
    category: str
    confidence: str  # low | medium | high
    signals: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"category": self.category, "confidence": self.confidence, "signals": self.signals}


def _texts(ev: dict) -> tuple[str, str]:
    logs = ev.get("logs") or {}
    log_text = "\n".join(filter(None, [logs.get("previous"), logs.get("current")]))
    msgs = [(ev.get("failure") or {}).get("message") or ""]
    msgs += [e.get("message") or "" for e in (ev.get("events") or [])]
    return log_text, "\n".join(msgs)


def classify(req: dict) -> Classification:
    inc, ev = req["incident"], req.get("evidence") or {}
    family = inc.get("cause_family") or _family_from_label(inc.get("cause", ""))
    reason = (ev.get("failure") or {}).get("reason") or ""
    log_text, msg_text = _texts(ev)
    deps = ev.get("dependencies") or []

    if family == "oom":
        return Classification("resources", "high", ["container OOMKilled (exit 137)"])
    if family == "image-pull":
        return Classification("image", "high", [f"container state {reason or 'image pull failure'}"])
    if family == "config":
        return Classification("configuration", "high", [f"container state {reason}"])
    if family == "evicted":
        return Classification("node", "medium", ["pod evicted by the kubelet"])

    probe = _PROBE.search(msg_text)
    if probe:
        return Classification("probe", "medium", [f"event: {probe.group(0)}"])

    missing = [d for d in deps if d.get("scope") == "namespace" and d.get("service_exists") is False]
    broken = [d for d in deps if d.get("service_exists") and
              (d.get("port_exposed") is False or d.get("ready_endpoints") == 0)]
    dep_log = _DEP_PATTERNS.search(log_text)
    if missing or broken:
        sig = [f"log: {dep_log.group(0)}"] if dep_log else []
        sig += [f"Service '{d['service']}' not found" for d in missing]
        sig += [f"Service '{d['service']}' unhealthy" for d in broken]
        return Classification("dependency", "high", sig)
    if dep_log:
        return Classification("dependency", "medium", [f"log: {dep_log.group(0)}"])

    perm = _PERM_PATTERNS.search(log_text)
    if perm:
        return Classification("permissions", "medium", [f"log: {perm.group(0)}"])
    cfg = _CONFIG_PATTERNS.search(log_text)
    if cfg:
        return Classification("configuration", "medium", [f"log: {cfg.group(0)}"])

    crash = _APP_CRASH.search(log_text)
    if crash and family == "crash":
        sig = crash.group(0).strip().splitlines()[0][:80]
        return Classification("application-error", "medium", [f"log: crash signature '{sig}'"])

    if family == "crash":
        code = inc.get("exit_code")
        if code is not None and code < 128:
            return Classification("application-error", "low",
                                  [f"application exited with code {code}; no known pattern in logs"])
    return Classification("unknown", "low", [])


def _family_from_label(label: str) -> str:
    return (label or "").split(" ", 1)[0] if label else ""


def verified_facts(req: dict) -> list[str]:
    inc, ev = req["incident"], req.get("evidence") or {}
    f = ev.get("failure") or {}
    facts: list[str] = []   # most diagnostic first
    context: list[str] = []  # supporting facts, appended at the end

    code = f.get("exit_code")
    if code == 137 and (f.get("reason") == "OOMKilled" or f.get("last_termination_reason") == "OOMKilled"):
        facts.append("The container was OOMKilled: it exceeded its memory limit (exit 137).")
    elif code is not None and code != 0:
        context.append(f"The container exited with code {code}.")

    limits = ((ev.get("resources") or {}).get(inc.get("container")) or {}).get("limits") or {}
    if inc.get("cause_family") == "oom" or "oom" in (inc.get("cause") or ""):
        facts.append(f"Memory limit: {limits.get('memory', 'none set')}.")

    for d in ev.get("dependencies") or []:
        target = f"{d['host']}:{d['port']}" if d.get("port") else d["host"]
        ns = inc.get("namespace")
        scope = d.get("scope")
        if scope == "namespace":
            if d.get("service_exists") is False:
                facts.append(f"The application tries to reach {target}, but no Service named "
                             f"'{d['service']}' exists in namespace {ns}.")
            else:
                facts.append(f"Service '{d['service']}' exists in namespace {ns} "
                             f"(ports {d.get('service_ports')}).")
                if d.get("port_exposed") is False:
                    facts.append(f"Service '{d['service']}' does not expose port {d['port']}.")
                if d.get("ready_endpoints") == 0:
                    facts.append(f"Service '{d['service']}' has 0 ready endpoints "
                                 f"(no healthy pods match its selector {d.get('selector')}).")
                elif d.get("ready_endpoints"):
                    facts.append(f"Service '{d['service']}' has {d['ready_endpoints']} ready endpoint(s).")
        elif scope == "other-namespace":
            facts.append(f"{target} is in another namespace; KubeLantern did not check it.")
        elif scope == "external":
            facts.append(f"{target} is an external host; KubeLantern did not check it.")

    logs = ev.get("logs") or {}
    if "skipped" in logs:
        facts.append(f"No logs: {logs['skipped']}.")

    for e in (ev.get("events") or []):
        msg = (e.get("message") or "")
        if e.get("type") == "Warning" and re.search(r"not found|manifest unknown|pull access denied|"
                                                    r"unauthorized|repository does not exist", msg, re.IGNORECASE):
            facts.append(f"Registry error while pulling the image: {msg[:200]}")
            break

    secrets = (ev.get("pod") or {}).get("image_pull_secrets")
    if inc.get("cause_family") == "image-pull":
        facts.append("The pod has imagePullSecrets: " + ", ".join(secrets) + "."
                     if secrets else "The pod has no imagePullSecrets.")

    deployment = next((o for o in ev.get("owners") or [] if o.get("kind") == "Deployment"), None)
    if deployment and deployment.get("replicas"):
        context.append(f"Deployment {deployment['name']}: {deployment.get('available_replicas', 0)}/"
                       f"{deployment['replicas']} replicas available.")
    return facts + context


def fallback_fields(req: dict, cls: Classification, facts: list[str]) -> dict:
    """Rule-based answer used to fill gaps when the model can't produce a valid one."""
    inc, ev = req["incident"], req.get("evidence") or {}
    deps = ev.get("dependencies") or []
    missing = next((d for d in deps if d.get("service_exists") is False), None)
    no_eps = next((d for d in deps if d.get("ready_endpoints") == 0), None)
    bad_port = next((d for d in deps if d.get("port_exposed") is False), None)
    ns = inc.get("namespace")
    limit = (((ev.get("resources") or {}).get(inc.get("container")) or {}).get("limits") or {}).get("memory")

    if cls.category == "dependency" and missing:
        return {
            "probable_cause": f"The application depends on '{missing['host']}', but Service "
                              f"'{missing['service']}' does not exist in namespace {ns}.",
            "suggested_fix": f"Deploy the '{missing['service']}' service in {ns}, or point the "
                             f"application at the correct host.",
            "next_steps": [f"kubectl -n {ns} get svc", "Check the application's database/host configuration"],
        }
    if cls.category == "dependency" and bad_port:
        return {
            "probable_cause": f"Service '{bad_port['service']}' exists but does not expose port {bad_port['port']}.",
            "suggested_fix": f"Expose port {bad_port['port']} on Service '{bad_port['service']}' "
                             f"or use one of its ports {bad_port.get('service_ports')}.",
            "next_steps": [f"kubectl -n {ns} get svc {bad_port['service']} -o yaml"],
        }
    if cls.category == "dependency" and no_eps:
        return {
            "probable_cause": f"Service '{no_eps['service']}' has no ready endpoints; the pods behind it "
                              f"are missing or not Ready.",
            "suggested_fix": f"Fix the pods selected by {no_eps.get('selector')} so they become Ready.",
            "next_steps": [f"kubectl -n {ns} get endpointslices -l kubernetes.io/service-name={no_eps['service']}"],
        }
    if cls.category == "dependency":
        return {"probable_cause": "The application cannot reach a network dependency.",
                "suggested_fix": "Verify the dependency's host, port and availability.",
                "next_steps": [f"kubectl -n {ns} get svc"]}
    if cls.category == "resources":
        return {"probable_cause": f"The container exceeds its memory limit ({limit or 'unset'}).",
                "suggested_fix": "Raise the memory limit or reduce the application's memory use.",
                "next_steps": [f"kubectl -n {ns} top pod", f"kubectl -n {ns} describe pod"]}
    if cls.category == "image":
        return {"probable_cause": "The image cannot be pulled (wrong name/tag or missing registry credentials).",
                "suggested_fix": "Correct the image reference or add imagePullSecrets for the registry.",
                "next_steps": [f"kubectl -n {ns} describe pod"]}
    return {"probable_cause": "Insufficient evidence for a definitive cause.",
            "suggested_fix": "Inspect the application logs and recent changes.",
            "next_steps": [f"kubectl -n {ns} logs <pod> --previous"]}
