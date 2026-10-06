#!/usr/bin/env bash
# One-off migration for clusters set up before Stage 8 (kubectl/sed manifests).
# Hands the existing objects over to the Helm releases so `make ai-up` and
# `make deploy-agents` can upgrade them in place — no reinstall, and Ollama
# keeps its downloaded models.
#
# Usage: deployments/kind/migrate-to-helm.sh [ai-namespace] [agent namespaces...]
set -euo pipefail

AI_NS="${1:-kubelantern-ai}"; shift || true
if [ $# -gt 0 ]; then AGENT_NS=("$@"); else AGENT_NS=(demo payments orders); fi

adopt() {  # adopt <release> <release-namespace> <kubectl args identifying objects...>
  local rel="$1" rel_ns="$2"; shift 2
  kubectl annotate --overwrite "$@" \
    meta.helm.sh/release-name="$rel" meta.helm.sh/release-namespace="$rel_ns" >/dev/null
  kubectl label --overwrite "$@" app.kubernetes.io/managed-by=Helm >/dev/null
  echo "  adopted: $* -> $rel"
}

echo "Platform ($AI_NS):"
# Renamed in the chart: remove the old copies (the chart recreates them).
kubectl -n "$AI_NS" delete networkpolicy default-deny-ingress gateway-from-agents \
  ollama-from-gateway qdrant-from-gateway --ignore-not-found
kubectl delete clusterrolebinding kubelantern-gateway-auth-delegator --ignore-not-found
for obj in serviceaccount/kubelantern-gateway deployment/kubelantern-gateway service/kubelantern-gateway \
           pvc/ollama-models deployment/ollama service/ollama \
           pvc/qdrant-data deployment/qdrant service/qdrant; do
  if kubectl -n "$AI_NS" get "$obj" >/dev/null 2>&1; then
    adopt kubelantern-ai "$AI_NS" -n "$AI_NS" "$obj"
  fi
done
if kubectl get crd runbooks.kubelantern.io >/dev/null 2>&1; then
  adopt kubelantern-ai "$AI_NS" crd/runbooks.kubelantern.io
fi

for ns in "${AGENT_NS[@]}"; do
  echo "Agent ($ns):"
  # The chart's Deployment uses strategy Recreate; Helm can't patch an existing
  # RollingUpdate Deployment into that, so let the chart recreate it (the agent
  # only holds in-memory state).
  kubectl -n "$ns" delete deployment kubelantern-agent --ignore-not-found
  for obj in serviceaccount/kubelantern-agent role/kubelantern-agent \
             rolebinding/kubelantern-agent \
             role/kubelantern-runbook-editor rolebinding/kubelantern-runbook-editor; do
    if kubectl -n "$ns" get "$obj" >/dev/null 2>&1; then
      adopt kubelantern-agent "$ns" -n "$ns" "$obj"
    fi
  done
done

echo
echo "Done. Now run:  make ai-up && make deploy-agents"
