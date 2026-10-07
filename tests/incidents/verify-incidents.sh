#!/usr/bin/env bash
# KubeLantern incident persistence test (live cluster).
#
#   1. a crash-looping workload opens an incident -> an Incident object appears
#   2. the diagnosis is stored on it
#   3. the agent is restarted -> SAME incident continues: no new OPENED, no new
#      object, no second diagnosis
#   4. the workload is fixed -> the object becomes Resolved
#
# Usage: tests/incidents/verify-incidents.sh [namespace]   (default: demo)
set -uo pipefail

NS="${1:-demo}"
PASS=0
FAIL=0
pass() { printf '  PASS  %s\n' "$1"; PASS=$((PASS+1)); }
fail() { printf '  FAIL  %s\n' "$1"; FAIL=$((FAIL+1)); }

wait_for() {  # wait_for <seconds> <command...>  — true when the command succeeds
  local deadline=$((SECONDS + $1)); shift
  until "$@" >/dev/null 2>&1; do
    [[ $SECONDS -ge $deadline ]] && return 1
    sleep 5
  done
}

incidents() { kubectl -n "$NS" get incidents.kubelantern.io -l kubelantern.io/workload=broken-app \
                -o jsonpath='{range .items[*]}{.metadata.name} {.status.state} {.status.diagnosis.category}{"\n"}{end}'; }
open_incident() { incidents | grep -q " Open"; }
diagnosed() { incidents | grep " Open" | grep -qv " Open $"; }
resolved() { incidents | grep -q " Resolved"; }

echo "== setup =="
kubectl get crd incidents.kubelantern.io >/dev/null || { echo "Incident CRD missing: make ai-up"; exit 1; }
kubectl delete -f tests/crashloop/ --ignore-not-found >/dev/null
kubectl -n "$NS" delete incidents.kubelantern.io -l kubelantern.io/workload=broken-app --ignore-not-found >/dev/null
sed "s/namespace: demo/namespace: $NS/" tests/crashloop/broken-app.yaml | kubectl apply -f - >/dev/null

echo "== 1. incident is persisted =="
if wait_for 120 open_incident; then pass "Incident object created (Open)"; else fail "no Open Incident after 120s"; fi
NAME=$(incidents | awk '/ Open/{print $1; exit}')
echo "    $NAME"

echo "== 2. diagnosis stored on the incident (calls the model) =="
if wait_for 240 diagnosed; then pass "diagnosis stored: $(incidents | awk '/ Open/{print $3; exit}')"; else fail "no diagnosis on the Incident after 240s"; fi

echo "== 3. agent restart continues the same incident =="
kubectl -n "$NS" rollout restart deploy/kubelantern-agent >/dev/null
kubectl -n "$NS" rollout status deploy/kubelantern-agent --timeout=120s >/dev/null
sleep 45   # let the new agent watch the still-failing pod for a while
LOG=$(kubectl -n "$NS" logs deploy/kubelantern-agent)
if echo "$LOG" | grep -qi "Restored open incident(s):.*${NAME}"; then   # object name = lowercased ID
  pass "new agent restored the incident"; else fail "new agent did not report restoring $NAME"; fi
if echo "$LOG" | grep -q "\[INCIDENT OPENED\] .*broken-app"; then
  fail "new agent opened a duplicate incident"; else pass "no duplicate OPENED after restart"; fi
if echo "$LOG" | grep -q "\[INCIDENT DIAGNOSIS\]"; then
  fail "new agent re-diagnosed an already diagnosed incident"; else pass "no second diagnosis"; fi
COUNT=$(incidents | grep -c .)
if [[ "$COUNT" == "1" ]]; then pass "still exactly one Incident object"; else fail "expected 1 Incident object, found $COUNT"; fi

echo "== 4. fix the workload -> Resolved (healthy window + tick) =="
kubectl -n "$NS" patch deploy/broken-app --type=json \
  -p='[{"op":"replace","path":"/spec/template/spec/containers/0/command","value":["sh","-c","echo healthy; sleep 3600"]}]' >/dev/null
if wait_for 420 resolved; then pass "Incident marked Resolved"; else fail "Incident not Resolved after 420s"; fi

echo
kubectl -n "$NS" get incidents.kubelantern.io
echo
echo "Result: ${PASS} passed, ${FAIL} failed"
kubectl delete -f tests/crashloop/ --ignore-not-found >/dev/null
[[ $FAIL -eq 0 ]]
