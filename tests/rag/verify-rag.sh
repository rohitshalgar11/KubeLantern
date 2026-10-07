#!/usr/bin/env bash
# Live check of runbook isolation (Stage 7).
#
# 1. A PRIVATE runbook is created in payments (with a unique marker string).
# 2. A team runbook is created in demo.
# 3. Both agents sync them to the gateway.
# 4. A demo incident must cite demo + shared runbooks and NEVER the payments one.
# 5. The same incident from payments must cite the payments runbook.
# No pipefail: checks use `... | grep -q`, and grep -q exits at the first match,
# so the writer (kubectl logs) gets SIGPIPE and pipefail would turn a match into a failure.
set -u
PASS=0; FAIL=0
GW=http://kubelantern-gateway.kubelantern-ai.svc:8080/v1/diagnose
MARKER=PAYMENTS-PRIVATE-MARKER

ok()   { printf '  PASS  %s\n' "$1"; PASS=$((PASS+1)); }
bad()  { printf '  FAIL  %s\n' "$1"; FAIL=$((FAIL+1)); }

echo "== apply runbooks =="
kubectl apply -f examples/runbooks/payments-private.yaml -f examples/runbooks/demo-broken-app.yaml

echo "== wait for both agents to sync (up to 3 min) =="
synced() { kubectl -n kubelantern-ai logs deploy/kubelantern-gateway 2>/dev/null \
           | grep '"event": "runbooks_sync"' | grep "\"namespace\": \"$1\"" | grep -q '"status": 200'; }
for i in $(seq 1 36); do
  if synced demo && synced payments; then break; fi
  sleep 5
done
synced demo && ok "demo runbooks synced" || bad "demo runbooks not synced (is the agent running Stage 7?)"
synced payments && ok "payments runbooks synced" || bad "payments runbooks not synced"

# Diagnose a dependency incident from INSIDE an agent pod, with that agent's token.
PY='
import json, sys, urllib.request
ns = sys.argv[1]
body = {"incident": {"id": ns + "-broken-app-INCrag0001", "namespace": ns,
                     "workload": "Deployment/broken-app", "container": "broken-app",
                     "cause": "crash (exit 1)", "cause_family": "crash", "exit_code": 1},
        "evidence": {"failure": {"namespace": ns, "reason": "CrashLoopBackOff", "exit_code": 1},
                     "logs": {"current": "FATAL: cannot connect to database at db:5432"},
                     "dependencies": [{"host": "db", "port": 5432, "scope": "namespace",
                                       "service": "db", "service_exists": False}]}}
tok = open("/var/run/secrets/kubelantern/token").read().strip()
req = urllib.request.Request(sys.argv[2], data=json.dumps(body).encode(), method="POST",
      headers={"Content-Type": "application/json", "Authorization": "Bearer " + tok})
raw = urllib.request.urlopen(req, timeout=400).read().decode()
out = json.loads(raw)
print("SOURCES", ",".join(r["source"] for r in out.get("references", [])))
print("MARKER_IN_RESPONSE", sys.argv[3] in raw)
'
run() { kubectl -n "$1" exec deploy/kubelantern-agent -- python -c "$PY" "$1" "$GW" "$MARKER" 2>&1; }

echo "== demo incident (calls the model, ~30-60s) =="
out=$(run demo); echo "$out" | sed 's/^/    /'
src=$(echo "$out" | awk '/^SOURCES/{print $2}')
[[ "$src" != *payments/* ]] && ok "demo never cites payments runbooks" || bad "demo cited a payments runbook!"
echo "$out" | grep -q "MARKER_IN_RESPONSE False" && ok "payments marker absent from demo diagnosis" \
  || bad "payments private content leaked into demo diagnosis!"
[[ "$src" == *demo/broken-app-database* ]] && ok "demo cites its own team runbook" || bad "demo team runbook not cited"
[[ "$src" == *shared/* ]] && ok "demo cites shared runbooks" || bad "no shared runbook cited"

echo "== payments incident =="
out=$(run payments); echo "$out" | sed 's/^/    /'
src=$(echo "$out" | awk '/^SOURCES/{print $2}')
[[ "$src" == *payments/payments-private-db* ]] && ok "payments cites its own private runbook" \
  || bad "payments did not cite its own runbook"
[[ "$src" != *demo/* ]] && ok "payments never cites demo runbooks" || bad "payments cited a demo runbook!"

echo
echo "Result: ${PASS} passed, ${FAIL} failed"
[[ $FAIL -eq 0 ]]
