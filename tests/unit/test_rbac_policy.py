"""Static guard for the agent's RBAC: the diagnostic read profile is fixed.

Fails CI if anyone widens the agent Role with sensitive resources, write
verbs, wildcards, or cluster scope — whatever the reason. Works on the chart
sources directly, so it runs without Helm or a cluster. When `helm` is on PATH
(as in CI), the rendered chart is checked too.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
AGENT_CHART = ROOT / "charts/kubelantern-agent"
RULES = AGENT_CHART / "files/role-rules.yaml"

ALLOWED_VERBS = {"get", "list", "watch"}
NEVER_RESOURCES = {
    "secrets", "configmaps",
    "pods/exec", "pods/attach", "pods/portforward", "pods/proxy", "pods/eviction",
    "services/proxy", "nodes/proxy", "serviceaccounts/token",
    "roles", "rolebindings", "clusterroles", "clusterrolebindings",
}


def _rules():
    return yaml.safe_load(RULES.read_text())


def _templates() -> dict[str, str]:
    return {p.name: p.read_text() for p in (AGENT_CHART / "templates").glob("*")}


def test_agent_chart_never_creates_cluster_scoped_rbac():
    for name, text in _templates().items():
        assert not re.search(r"kind:\s*Cluster(Role|RoleBinding)", text), name


def test_agent_role_rules_come_only_from_the_fixed_file():
    rbac = _templates()["rbac.yaml"]
    assert re.search(r"kind:\s*Role\n", rbac)
    # the Role's rules are exactly the file — no values can add to them
    assert re.search(r'rules:\n\s*\{\{- \.Files\.Get "files/role-rules\.yaml" \| nindent 2 \}\}', rbac)
    values = yaml.safe_load((AGENT_CHART / "values.yaml").read_text())
    assert not re.search(r"rules", yaml.safe_dump(values), re.IGNORECASE), "values must not configure RBAC rules"


# The one exception to read-only: the agent's own incident records.
INCIDENT_RULE = {"apiGroups": ["kubelantern.io"], "resources": ["incidents"]}
INCIDENT_VERBS = {"get", "list", "watch", "create", "update", "patch", "delete"}


def _is_incident_rule(rule):
    return rule["apiGroups"] == INCIDENT_RULE["apiGroups"] and \
        rule["resources"] == INCIDENT_RULE["resources"]


def test_read_only_verbs_except_own_incident_records():
    for rule in _rules():
        if _is_incident_rule(rule):
            assert set(rule["verbs"]) <= INCIDENT_VERBS, rule
        else:
            assert set(rule["verbs"]) <= ALLOWED_VERBS, rule


def test_the_only_write_is_incidents_and_nothing_else_in_that_rule():
    writers = [r for r in _rules() if set(r["verbs"]) - ALLOWED_VERBS]
    assert len(writers) == 1 and _is_incident_rule(writers[0]), writers
    # runbooks stay read-only for the agent (teams own them)
    for r in _rules():
        if "runbooks" in r["resources"]:
            assert set(r["verbs"]) <= ALLOWED_VERBS


def test_no_wildcards():
    for rule in _rules():
        for field in ("apiGroups", "resources", "verbs"):
            assert "*" not in rule.get(field, []), rule
        assert "resourceNames" not in rule or rule["resourceNames"], rule


def test_never_list_is_not_granted():
    granted = {r for rule in _rules() for r in rule["resources"]}
    assert not (granted & NEVER_RESOURCES), granted & NEVER_RESOURCES


def test_profile_covers_what_diagnosis_reads():
    granted = {(g, r) for rule in _rules() for g in rule["apiGroups"] for r in rule["resources"]}
    for need in [("", "pods"), ("", "pods/log"), ("", "events"), ("", "services"),
                 ("apps", "deployments"), ("apps", "replicasets"), ("apps", "statefulsets"),
                 ("batch", "jobs"), ("discovery.k8s.io", "endpointslices"),
                 ("", "persistentvolumeclaims"), ("autoscaling", "horizontalpodautoscalers"),
                 ("kubelantern.io", "runbooks")]:
        assert need in granted, need


def test_team_runbook_editor_is_namespaced_and_runbooks_only():
    text = _templates()["runbooks.yaml"]
    editor = text[text.index("name: kubelantern-runbook-editor"):]
    rules = editor[editor.index("rules:"):editor.index("---")]
    assert 'apiGroups: ["kubelantern.io"]' in rules and 'resources: ["runbooks"]' in rules
    assert rules.count("apiGroups") == 1


# -- rendered chart (only where helm is installed, e.g. CI) -------------------

HELM = shutil.which("helm")


def _render(*args):
    out = subprocess.run([HELM, "template", "t", str(AGENT_CHART), "-n", "payments", *args],
                         check=True, capture_output=True, text=True).stdout
    return [d for d in yaml.safe_load_all(out) if d]


@pytest.mark.skipif(not HELM, reason="helm not installed")
@pytest.mark.parametrize("values", [None, AGENT_CHART / "ci/full-values.yaml"])
def test_rendered_agent_chart(values):
    docs = _render(*(["-f", str(values)] if values else []))
    kinds = {d["kind"] for d in docs}
    assert not kinds & {"ClusterRole", "ClusterRoleBinding"}
    for d in docs:
        assert d["metadata"]["namespace"] == "payments", d["kind"]
    [role] = [d for d in docs if d["kind"] == "Role" and d["metadata"]["name"] == "kubelantern-agent"]
    assert role["rules"] == _rules()
