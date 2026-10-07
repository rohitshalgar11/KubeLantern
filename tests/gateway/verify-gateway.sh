#!/usr/bin/env bash
# KubeLantern Gateway security test.
#
# Runs requests from INSIDE the demo agent pod (the only kind of pod allowed to
# reach the gateway) and checks that the gateway enforces identity, audience and
# namespace isolation. Then checks NetworkPolicy blocks everything else.
#
# Usage: tests/gateway/verify-gateway.sh [--skip-llm]
# No pipefail: checks use `... | grep -q`, and grep -q exits at the first match,
# so the writer (kubectl logs) gets SIGPIPE and pipefail would turn a match into a failure.
set -u

NS=demo
OTHER=payments
GW=http://kubelantern-gateway.kubelantern-ai.svc:8080
OLLAMA=http://ollama.kubelantern-ai.svc:11434
SKIP_LLM=0; [[ "${1:-}" == "--skip-llm" ]] && SKIP_LLM=1
PASS=0; FAIL=0; WARN=0

# POST from inside the agent pod; prints the HTTP status (or ERR <type>)
PY='
import sys, json, urllib.request, urllib.error
url, tok, body = sys.argv[1], sys.argv[2], sys.argv[3]
h = {"Content-Type": "application/json"}
if tok == "@self":
    tok = open("/var/run/secrets/kubelantern/token").read().strip()
if tok:
    h["Authorization"] = "Bearer " + tok
try:
    r = urllib.request.urlopen(urllib.request.Request(url, data=body.encode() if body else None,
                               headers=h, method="POST" if body else "GET"), timeout=float(sys.argv[4]))
    print(r.status)
except urllib.error.HTTPError as e:
    print(e.code)
except Exception as e:
    print("ERR", type(e).__name__)
'
req() { kubectl -n "$NS" exec deploy/kubelantern-agent -- python -c "$PY" "$1" "$2" "$3" "${4:-15}" 2>/dev/null | tail -1; }

body() {  # body <namespace-in-request>
  printf '{"incident":{"id":"%s-verify-INC000000","namespace":"%s","workload":"Deployment/verify","container":"verify","cause":"crash (exit 1)"},"evidence":{"failure":{"namespace":"%s","reason":"Error","exit_code":1},"logs":{"current":"FATAL: cannot connect to database at db:5432"}}}' "$1" "$1" "$1"
}

check() {  # check <label> <got> <expected...>
  local label="$1" got="$2"; shift 2
  for exp in "$@"; do
    if [[ "$got" == "$exp" ]]; then printf '  PASS  %-62s -> %s\n' "$label" "$got"; PASS=$((PASS+1)); return; fi
  done
  printf '  FAIL  %-62s -> %s (expected %s)\n' "$label" "$got" "$*"; FAIL=$((FAIL+1))
}

echo "== identity & audience =="
check "no token"                                         "$(req $GW/v1/diagnose "" "$(body $NS)")" 401
WRONG_AUD=$(kubectl -n $NS create token kubelantern-agent --duration=10m)
check "agent token for the Kubernetes API (wrong audience)" "$(req $GW/v1/diagnose "$WRONG_AUD" "$(body $NS)")" 401
OTHER_SA=$(kubectl -n $NS create token default --audience kubelantern-gateway --duration=10m)
check "'default' SA token (not a KubeLantern agent)"         "$(req $GW/v1/diagnose "$OTHER_SA" "$(body $NS)")" 403

echo "== namespace isolation =="
check "demo agent asks about $OTHER incident"            "$(req $GW/v1/diagnose @self "$(body $OTHER)")" 403
OTHER_TOKEN=$(kubectl -n $OTHER create token kubelantern-agent --audience kubelantern-gateway --duration=10m)
check "$OTHER token used to ask about $NS"                "$(req $GW/v1/diagnose "$OTHER_TOKEN" "$(body $NS)")" 403

if [[ $SKIP_LLM -eq 0 ]]; then
  echo "== happy path (calls the model; can take ~1 min on CPU) =="
  check "demo agent, own namespace"                       "$(req $GW/v1/diagnose @self "$(body $NS)" 300)" 200 429
fi

echo "== network isolation (NetworkPolicies from the kubelantern-ai chart) =="
check "agent pod -> ollama directly (must be blocked)"   "$(req $OLLAMA/api/tags "" "" 5)" "ERR URLError" "ERR TimeoutError" "ERR timeout" "ERR socket.timeout"
# part-of=kubelantern: the agent ignores KubeLantern's own pods, so this
# intentionally failing probe doesn't open an incident.
out=$(kubectl -n $NS run gw-probe-$RANDOM --rm -i --restart=Never --image=busybox:1.36 --quiet \
      --labels=app.kubernetes.io/part-of=kubelantern -- \
      wget -T 5 -qO- $GW/healthz 2>&1 || true)
if echo "$out" | grep -q '"status"'; then
  printf '  FAIL  %-62s -> reachable\n' "non-agent pod in $NS -> gateway (must be blocked)"; FAIL=$((FAIL+1))
else
  printf '  PASS  %-62s -> blocked\n' "non-agent pod in $NS -> gateway (must be blocked)"; PASS=$((PASS+1))
fi

echo
echo "Result: ${PASS} passed, ${FAIL} failed"
if [[ $FAIL -gt 0 ]]; then
  echo "Hint: network checks fail if networkPolicy.enabled=false in the kubelantern-ai chart, or if the CNI does not enforce NetworkPolicy."
fi
[[ $FAIL -eq 0 ]]
