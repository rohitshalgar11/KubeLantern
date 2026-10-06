#!/usr/bin/env bash
# KubeLantern notification test (live cluster, no real Teams needed).
#
# Points the demo agent's notifier at an in-cluster webhook sink, then checks:
#   1. the agent container cannot read the webhook Secret (only the notifier can)
#   2. a crash-looping workload produces a Teams card with the diagnosis
#   3. Slack, generic webhook and email (Teams channel address) arrive too
#   4. fixing the workload produces a "resolved" card
#
# Usage: tests/notify/verify-notify.sh [namespace]   (default: demo)
set -uo pipefail
NS="${1:-demo}"
SINK=http://webhook-sink.kubelantern-test.svc:8080
PASS=0; FAIL=0
pass() { printf '  PASS  %s\n' "$1"; PASS=$((PASS+1)); }
fail() { printf '  FAIL  %s\n' "$1"; FAIL=$((FAIL+1)); }
sink_log() { kubectl -n kubelantern-test logs deploy/webhook-sink 2>/dev/null; }
wait_for() { local d=$((SECONDS + $1)); shift; until "$@" >/dev/null 2>&1; do [[ $SECONDS -ge $d ]] && return 1; sleep 5; done; }

echo "== setup: sink, Secret, notifications on for $NS =="
kubectl apply -f tests/notify/webhook-sink.yaml >/dev/null
kubectl -n kubelantern-test rollout status deploy/webhook-sink --timeout=120s >/dev/null
kubectl -n "$NS" create secret generic kubelantern-notify \
  --from-literal=teams="$SINK/teams" --from-literal=slack="$SINK/slack" \
  --from-literal=webhook="$SINK/webhook" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
helm upgrade kubelantern-agent charts/kubelantern-agent -n "$NS" --reuse-values \
  --set notifications.enabled=true --set notifications.allowInsecure=true \
  --set 'notifications.channels={teams,slack,webhook,email}' \
  --set notifications.email.smtpHost=webhook-sink.kubelantern-test.svc --set notifications.email.smtpPort=2525 \
  --set notifications.email.tls=none --set notifications.email.from=kubelantern@example.com \
  --set 'notifications.email.to={channel.test@emea.teams.ms}' --wait --timeout 120s >/dev/null
kubectl delete -f tests/crashloop/ --ignore-not-found >/dev/null
kubectl -n kubelantern-test rollout restart deploy/webhook-sink >/dev/null   # clear old output
kubectl -n kubelantern-test rollout status deploy/webhook-sink --timeout=120s >/dev/null

echo "== 1. secret isolation =="
if kubectl -n "$NS" exec deploy/kubelantern-agent -c agent -- cat /etc/kubelantern/notify/teams >/dev/null 2>&1; then
  fail "agent container can read the webhook URL"; else pass "agent container cannot read the webhook URL"; fi
if kubectl -n "$NS" exec deploy/kubelantern-agent -c notifier -- ls /var/run/secrets/kubernetes.io/serviceaccount/token >/dev/null 2>&1; then
  fail "notifier container has a Kubernetes API token"; else pass "notifier container has no Kubernetes API token"; fi

echo "== 2. diagnosis card (calls the model, ~1 min) =="
sed "s/namespace: demo/namespace: $NS/" tests/crashloop/broken-app.yaml | kubectl apply -f - >/dev/null
diag_card() { sink_log | grep -A1 "=== POST /teams" | grep -q "Incident diagnosed"; }
if wait_for 300 diag_card; then pass "Teams card: Incident diagnosed"; else fail "no Teams diagnosis card after 300s"; fi
if sink_log | grep -A1 "=== POST /teams" | grep -q "AdaptiveCard"; then pass "Teams payload is an Adaptive Card"; else fail "Teams payload is not an Adaptive Card"; fi
if sink_log | grep -A1 "=== POST /teams" | grep -q "dependency"; then pass "card carries the category"; else fail "category missing from the card"; fi

echo "== 3. other channels =="
if sink_log | grep -q "=== POST /slack"; then pass "Slack payload delivered"; else fail "no Slack payload"; fi
if sink_log | grep -A1 "=== POST /webhook" | grep -q '"source": "kubelantern"'; then pass "generic webhook JSON delivered"; else fail "no generic webhook JSON"; fi
# (long Subject headers are folded onto the next line, so match the text alone)
if sink_log | grep -A12 "=== EMAIL" | grep -q "\[KubeLantern\] Incident diagnosed"; then pass "email delivered (subject: [KubeLantern] Incident diagnosed…)"; else fail "no diagnosis email"; fi
if sink_log | grep -A12 "=== EMAIL" | grep -q "To: channel.test@emea.teams.ms"; then pass "email addressed to the channel address"; else fail "email not addressed to the channel"; fi

echo "== 4. resolved card =="
kubectl -n "$NS" patch deploy/broken-app --type=json \
  -p='[{"op":"replace","path":"/spec/template/spec/containers/0/command","value":["sh","-c","echo healthy; sleep 3600"]}]' >/dev/null
resolved_card() { sink_log | grep -A1 "=== POST /teams" | grep -q "Incident resolved"; }
if wait_for 420 resolved_card; then pass "Teams card: Incident resolved"; else fail "no resolved card after 420s"; fi

echo
echo "== Teams card (as Teams receives it) =="
sink_log | grep -A1 "=== POST /teams" | grep -m1 "Incident diagnosed" | python3 -m json.tool 2>/dev/null | head -60
echo
echo "Result: ${PASS} passed, ${FAIL} failed"
kubectl delete -f tests/crashloop/ --ignore-not-found >/dev/null
[[ $FAIL -eq 0 ]]
