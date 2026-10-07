#!/usr/bin/env bash
# KubeLantern maintenance mode test (live cluster, cluster-wide switch).
#
#   1. maintenance on (kubelantern-ai chart values) -> the agent logs the pause
#   2. while paused, a crash loop (broken-app) and a short blip (flappy-app)
#      open NO incident, no diagnosis, no notification
#   3. maintenance off -> the agent waits settleSeconds, re-checks every pod
#      and opens an incident ONLY for what is still broken (broken-app)
#
# Needs the kind values (settleSeconds 60, pollSeconds 10): make deploy-agents
# Usage: tests/maintenance/verify-maintenance.sh [namespace]   (default: demo)
# No pipefail: checks use `... | grep -q`, and grep -q exits at the first match,
# so the writer (kubectl logs) gets SIGPIPE and pipefail would turn a match into a failure.
set -u

NS="${1:-demo}"
AI_NS="${AI_NS:-kubelantern-ai}"
HELM="${HELM:-helm}"
PASS=0
FAIL=0
pass() { printf '  PASS  %s\n' "$1"; PASS=$((PASS+1)); }
fail() { printf '  FAIL  %s\n' "$1"; FAIL=$((FAIL+1)); }

wait_for() {  # wait_for <seconds> <command...>
  local deadline=$((SECONDS + $1)); shift
  until "$@" >/dev/null 2>&1; do
    [[ $SECONDS -ge $deadline ]] && return 1
    sleep 5
  done
}

agent_log() { kubectl -n "$NS" logs deploy/kubelantern-agent -c agent --since-time="$START"; }
logged() { agent_log | grep -q "$1"; }
incident_for() { kubectl -n "$NS" get incidents.kubelantern.io -l "kubelantern.io/workload=$1" \
                   -o jsonpath='{range .items[*]}{.metadata.name} {.status.state}{"\n"}{end}'; }
open_for() { incident_for "$1" | grep -q " Open"; }

maintenance() {  # maintenance on|off
  if [[ "$1" == on ]]; then
    "$HELM" upgrade kubelantern-ai charts/kubelantern-ai -n "$AI_NS" --reuse-values \
      --set maintenance.paused=true \
      --set-string maintenance.until="$(date -u -d '+30 minutes' +%Y-%m-%dT%H:%M:%SZ)" \
      --set-string maintenance.reason="verify-maintenance" >/dev/null
  else
    "$HELM" upgrade kubelantern-ai charts/kubelantern-ai -n "$AI_NS" --reuse-values \
      --set maintenance.paused=false --set-string maintenance.until= \
      --set-string maintenance.reason= >/dev/null
  fi
}

cleanup() {
  kubectl -n "$NS" delete deploy/broken-app deploy/flappy-app svc/broken-app --ignore-not-found >/dev/null 2>&1
  maintenance off >/dev/null 2>&1
}
trap cleanup EXIT

echo "== setup =="
kubectl -n "$NS" get deploy/kubelantern-agent >/dev/null || { echo "no agent in $NS: make deploy-agents"; exit 1; }
cleanup
kubectl -n "$NS" delete incidents.kubelantern.io -l 'kubelantern.io/workload in (broken-app,flappy-app)' \
  --ignore-not-found >/dev/null
START=$(date -u +%Y-%m-%dT%H:%M:%SZ)

echo "== 1. maintenance on (cluster-wide) =="
maintenance on
# The kubelet refreshes the mounted ConfigMap (~1 min), then the agent polls.
if wait_for 180 logged "\[MAINTENANCE\] paused"; then pass "agent paused: $(agent_log | grep '\[MAINTENANCE\] paused' | tail -1 | cut -c1-90)…"
else fail "agent did not log a pause within 180s"; fi

echo "== 2. failures during the pause stay silent =="
sed "s/namespace: demo/namespace: $NS/" tests/crashloop/broken-app.yaml | kubectl apply -f - >/dev/null
sed "s/namespace: demo/namespace: $NS/" tests/maintenance/flappy-app.yaml | kubectl apply -f - >/dev/null
sleep 75   # both pods fail at least once; flappy-app recovers
if [[ -z "$(incident_for broken-app)$(incident_for flappy-app)" ]]; then pass "no Incident objects created while paused"
else fail "Incident created during maintenance: $(incident_for broken-app) $(incident_for flappy-app)"; fi
if logged "\[INCIDENT OPENED\]"; then fail "agent opened an incident while paused"; else pass "no INCIDENT OPENED in the agent log"; fi
if logged "\[INCIDENT DIAGNOSIS\]"; then fail "agent diagnosed while paused"; else pass "no diagnosis while paused"; fi
FLAPPY=$(kubectl -n "$NS" get pods -l app=flappy-app -o jsonpath='{.items[0].status.containerStatuses[0].ready}')
if [[ "$FLAPPY" == "true" ]]; then pass "flappy-app failed once and recovered by itself"
else fail "flappy-app is not Ready ($FLAPPY)"; fi

echo "== 3. maintenance off -> settle, re-check, only real failures =="
maintenance off
if wait_for 180 logged "\[MAINTENANCE\] over"; then pass "agent noticed the end of maintenance"
else fail "agent did not log the end of maintenance within 180s"; fi
if wait_for 120 logged "\[MAINTENANCE\] re-check done"; then pass "$(agent_log | grep 're-check done' | tail -1)"
else fail "no re-check after the settle window"; fi
if wait_for 330 open_for broken-app; then pass "incident opened for broken-app (still failing)"
else fail "no incident for broken-app after maintenance"; fi
if [[ -z "$(incident_for flappy-app)" ]]; then pass "no incident for flappy-app (recovered during maintenance)"
else fail "flappy-app got an incident: $(incident_for flappy-app)"; fi

echo
agent_log | grep "\[MAINTENANCE\]"
echo
echo "Result: ${PASS} passed, ${FAIL} failed"
[[ $FAIL -eq 0 ]]
