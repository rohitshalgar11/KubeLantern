#!/usr/bin/env bash
# KubeLantern RBAC isolation test.
#
# Proves each namespace agent can read ONLY its own namespace, and can
# never write anything or read secrets. Uses impersonation, so it tests
# the real Role/RoleBinding objects in the cluster.
#
# Usage: tests/rbac/verify-isolation.sh [ns-a] [ns-b]   (default: payments orders)
set -uo pipefail

A="${1:-payments}"
B="${2:-orders}"
PASS=0
FAIL=0

check() {
  # check <expect yes|no> <namespace|--all-namespaces> <verb> <resource> <as-namespace>
  local expect="$1" scope="$2" verb="$3" res="$4" as_ns="$5"
  local sa="system:serviceaccount:${as_ns}:kubelantern-agent"
  local scope_flag
  if [[ "$scope" == "--all-namespaces" ]]; then scope_flag="--all-namespaces"; else scope_flag="-n $scope"; fi

  local got
  got=$(kubectl auth can-i "$verb" "$res" $scope_flag --as="$sa" 2>/dev/null || true)
  got="${got%%$'\n'*}"

  local label="${as_ns}-agent  ${verb} ${res}  in ${scope}"
  if [[ "$got" == "$expect" ]]; then
    printf '  PASS  %-55s -> %s\n' "$label" "$got"; PASS=$((PASS+1))
  else
    printf '  FAIL  %-55s -> %s (expected %s)\n' "$label" "$got" "$expect"; FAIL=$((FAIL+1))
  fi
}

for pair in "$A $B" "$B $A"; do
  set -- $pair
  own="$1"; other="$2"
  echo "== ${own}-agent =="
  echo " own namespace (allowed):"
  check yes "$own" watch pods        "$own"
  check yes "$own" list  pods        "$own"
  check yes "$own" get   pods/log    "$own"
  check yes "$own" list  events      "$own"
  check yes "$own" get   replicasets.apps "$own"
  check yes "$own" get   deployments.apps "$own"
  check yes "$own" list  services    "$own"
  check yes "$own" list  endpointslices.discovery.k8s.io "$own"
  check yes "$own" get   statefulsets.apps "$own"
  check yes "$own" get   jobs.batch "$own"
  check yes "$own" get   persistentvolumeclaims "$own"
  check yes "$own" get   horizontalpodautoscalers.autoscaling "$own"
  check yes "$own" list  runbooks.kubelantern.io "$own"
  echo " own incident records (the agent's only write):"
  check yes "$own" create incidents.kubelantern.io "$own"
  check yes "$own" patch  incidents.kubelantern.io "$own"
  check yes "$own" list   incidents.kubelantern.io "$own"
  echo " other namespace (forbidden):"
  check no  "$other" list  pods      "$own"
  check no  "$other" watch pods      "$own"
  check no  "$other" get   pods/log  "$own"
  check no  "$other" list  events    "$own"
  check no  "$other" get   deployments.apps "$own"
  check no  "$other" list  services  "$own"
  check no  "$other" list  endpointslices.discovery.k8s.io "$own"
  check no  "$other" get   statefulsets.apps "$own"
  check no  "$other" list  runbooks.kubelantern.io "$own"
  check no  "$other" list  incidents.kubelantern.io "$own"
  check no  "$other" create incidents.kubelantern.io "$own"
  echo " cluster-wide (forbidden):"
  check no  --all-namespaces list pods "$own"
  check no  --all-namespaces list namespaces "$own"
  check no  --all-namespaces list incidents.kubelantern.io "$own"
  echo " writes & secrets (forbidden, even in own namespace):"
  check no  "$own" delete pods       "$own"
  check no  "$own" create pods       "$own"
  check no  "$own" patch  deployments.apps "$own"
  check no  "$own" get    secrets    "$own"
  check no  "$own" get    configmaps "$own"
  check no  "$own" create pods/exec  "$own"
  check no  "$own" create pods/attach "$own"
  check no  "$own" create pods/portforward "$own"
  check no  "$own" create serviceaccounts/token "$own"
  check no  "$own" update deployments.apps "$own"
  check no  "$own" create runbooks.kubelantern.io "$own"
  echo
done

# Live check: from inside the payments agent pod, try to read orders.
if kubectl -n "$A" get deploy kubelantern-agent >/dev/null 2>&1; then
  echo "== live API call from inside ${A} agent pod =="
  out=$(kubectl -n "$A" exec deploy/kubelantern-agent -- python -c "
from kubernetes import client, config
config.load_incluster_config()
try:
    client.CoreV1Api().list_namespaced_pod('${B}')
    print('ALLOWED')
except client.exceptions.ApiException as e:
    print('HTTP', e.status)
" 2>&1 | tail -1)
  if [[ "$out" == "HTTP 403" ]]; then
    echo "  PASS  ${A}-agent list pods in ${B}  -> 403 Forbidden"; PASS=$((PASS+1))
  else
    echo "  FAIL  ${A}-agent list pods in ${B}  -> ${out}"; FAIL=$((FAIL+1))
  fi
  echo
fi

echo "Result: ${PASS} passed, ${FAIL} failed"
[[ $FAIL -eq 0 ]]
