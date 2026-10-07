#!/usr/bin/env bash
# KubeLantern shared knowledge base test (live cluster).
#
#   1. the gateway loaded the built-in library from its ConfigMap (no image copy)
#   2. the real embeddings find the right runbook for known failures (tests/kb/cases.json)
#   3. a platform runbook added as a ConfigMap is picked up WITHOUT a restart
#   4. a runbook with a built-in name overrides the built-in one
#   5. deleting the ConfigMap removes its runbooks again
#
# Needs the kind values (existingConfigMaps: [kubelantern-runbooks-platform],
# reloadSeconds: 10): make ai-up
# Usage: tests/kb/verify-kb.sh
set -uo pipefail

AI_NS="${AI_NS:-kubelantern-ai}"
CM=kubelantern-runbooks-platform
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

kbcheck() { kubectl -n "$AI_NS" exec -i deploy/kubelantern-gateway -c gateway -- python -m gateway.kbcheck; }
gw_log() { kubectl -n "$AI_NS" logs deploy/kubelantern-gateway -c gateway --since-time="$START"; }
PLATFORM_CASE='[{"name":"platform-db","expect":"platform-postgres","log":"psycopg2.OperationalError: connection to orders.postgres.database.azure.com timed out; private endpoint not reachable\n"}]'
platform_found() { echo "$PLATFORM_CASE" | kbcheck | grep -q "^Found: 1/1"; }
platform_gone() { ! platform_found; }

cleanup() { kubectl -n "$AI_NS" delete configmap "$CM" --ignore-not-found >/dev/null 2>&1; }
trap cleanup EXIT

echo "== setup =="
kubectl -n "$AI_NS" get deploy/kubelantern-gateway >/dev/null || { echo "no gateway: make ai-up"; exit 1; }
if ! kubectl -n "$AI_NS" get deploy/kubelantern-gateway -o yaml | grep -q "$CM"; then
  echo "the gateway doesn't mount $CM: run 'make ai-up' (kind values) first"; exit 1
fi
cleanup
START=$(date -u +%Y-%m-%dT%H:%M:%SZ)
POD=$(kubectl -n "$AI_NS" get pod -l app.kubernetes.io/name=kubelantern-gateway -o jsonpath='{.items[0].metadata.name}')

echo "== 1. built-in library loaded from the ConfigMap =="
N=$(kubectl -n "$AI_NS" get configmap kubelantern-runbooks-builtin -o go-template='{{len .data}}' 2>/dev/null || echo 0)
LOADED=$(kubectl -n "$AI_NS" logs "$POD" -c gateway | grep -Eo 'loaded [0-9]+ shared runbooks' | tail -1 | grep -Eo '[0-9]+')
if [[ "${N:-0}" -ge 40 && "${LOADED:-0}" -ge "$N" ]]; then pass "ConfigMap has $N runbooks; gateway loaded ${LOADED}"
else fail "ConfigMap has ${N:-0} runbooks, gateway log says loaded ${LOADED:-none}"; fi
if kubectl -n "$AI_NS" exec "$POD" -c gateway -- test -e /app/runbooks/shared 2>/dev/null; then
  fail "runbooks are still baked into the image (/app/runbooks/shared)"
else pass "no runbooks in the image: they come from the ConfigMap"; fi

echo "== 2. real embeddings find the right runbook =="
OUT=$(kbcheck < tests/kb/cases.json)
echo "$OUT" | sed 's/^/    /'
FOUND=$(echo "$OUT" | sed -n 's/^Found: \([0-9]*\)\/\([0-9]*\).*/\1 \2/p')
read -r F T <<<"${FOUND:-0 1}"
if (( F * 100 >= T * 90 )); then pass "expected runbook found for $F/$T cases (need 90%)"
else fail "expected runbook found for only $F/$T cases (need 90%)"; fi

echo "== 3. platform runbook added as a ConfigMap, no restart =="
sed "s/namespace: kubelantern-ai/namespace: $AI_NS/" examples/shared-runbooks/configmap.yaml | kubectl apply -f - >/dev/null
if wait_for 180 platform_found; then pass "platform-postgres found by the search"
else fail "platform-postgres not found within 180s"; fi
NOW=$(kubectl -n "$AI_NS" get pod -l app.kubernetes.io/name=kubelantern-gateway -o jsonpath='{.items[0].metadata.name}')
if [[ "$NOW" == "$POD" ]]; then pass "gateway was not restarted ($POD)"; else fail "gateway pod changed: $POD -> $NOW"; fi

echo "== 4. same name as a built-in runbook overrides it =="
kubectl -n "$AI_NS" patch configmap "$CM" --type merge -p "$(cat <<'JSON'
{"data":{"redis-errors.md":"---\ntitle: Our Redis (platform version)\n---\n# Symptoms\nREADONLY You can't write against a read only replica.\n# Fix\nUse the platform Redis primary endpoint.\n"}}
JSON
)" >/dev/null
if wait_for 180 bash -c "kubectl -n $AI_NS logs deploy/kubelantern-gateway -c gateway --since-time=$START | grep -q 'overridden by a later folder: redis-errors'"; then
  pass "gateway reports redis-errors overridden by the platform ConfigMap"
else fail "no override reported within 180s"; fi

echo "== 5. deleting the ConfigMap removes its runbooks =="
cleanup
if wait_for 180 platform_gone; then pass "platform-postgres no longer found"
else fail "platform-postgres still found 180s after deleting the ConfigMap"; fi

echo
gw_log | grep -E "shared runbook" | sed 's/^/    /'
echo
echo "Result: ${PASS} passed, ${FAIL} failed"
[[ $FAIL -eq 0 ]]
