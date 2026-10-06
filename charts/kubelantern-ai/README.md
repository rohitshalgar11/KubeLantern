# kubelantern-ai

The shared, platform-owned part of KubeLantern. Install **once per cluster**,
into its own namespace (`kubelantern-ai` by default):

| Component | Purpose |
|---|---|
| **Gateway** | the only path to the AI: verifies agent tokens (TokenReview), stamps the namespace from identity, redacts, rate-limits, runs the diagnosis graph, audit log |
| **Ollama** | local LLM (`qwen2.5:1.5b`) and embedding model (`nomic-embed-text`); nothing leaves the cluster |
| **Qdrant** | vector store for shared and team runbooks, filtered by namespace |
| **CRDs** | `Runbook` (`kubelantern.io/v1alpha1`) |
| **NetworkPolicies** | agents → gateway → Ollama/Qdrant; nothing else (optional egress rules too) |

Cluster-scoped objects: the `Runbook` CRD, and one ClusterRoleBinding of the
gateway's ServiceAccount to the built-in `system:auth-delegator` (TokenReview
only — it can't read any object).

## Install

```bash
helm install kubelantern-ai oci://ghcr.io/rohitshalgar11/charts/kubelantern-ai \
  --namespace kubelantern-ai --create-namespace --wait --timeout 30m
```

The first start downloads the models (~1.3 GB) into Ollama, so `--wait` can take
a while. Then install the [`kubelantern-agent`](../kubelantern-agent) chart into
each team namespace.

With ArgoCD, see [docs/helm-argocd.md](../../docs/helm-argocd.md). The CRDs carry
`argocd.argoproj.io/sync-wave: "-1"` so they are applied first.

## Values

| Key | Default | Description |
|---|---|---|
| `gateway.image.repository` | `ghcr.io/rohitshalgar11/kubelantern-gateway` | |
| `gateway.image.tag` | chart `appVersion` | |
| `gateway.model` | `qwen2.5:1.5b` | chat model for diagnoses |
| `gateway.embedModel` | `nomic-embed-text` | embedding model for runbooks |
| `gateway.pullModels` | `true` | pull missing models at start |
| `gateway.audience` | `kubelantern-gateway` | token audience agents must present |
| `gateway.ratePerMinute` | `6` | diagnoses per namespace per minute |
| `gateway.runbookMinScore` | `0.35` | minimum runbook similarity |
| `gateway.port` | `8080` | |
| `gateway.resources` | 20m/128Mi → 500m/512Mi | |
| `ollama.enabled` | `true` | `false` = use `ollama.externalUrl` |
| `ollama.externalUrl` | `""` | e.g. Ollama on a GPU node pool |
| `ollama.image.tag` | `latest` | pin a version in production |
| `ollama.persistence.size` | `8Gi` | model storage |
| `ollama.persistence.storageClass` | `""` | cluster default |
| `ollama.resources` | 500m/2Gi → 5Gi | |
| `qdrant.enabled` | `true` | `false` = diagnoses without runbooks |
| `qdrant.image.tag` | `v1.12.4` | |
| `qdrant.persistence.size` | `2Gi` | |
| `crds.install` | `true` | `false` if you manage CRDs separately |
| `crds.keep` | `true` | keep CRDs (and teams' Runbooks) on uninstall |
| `agentNamespaceSelector` | `{kubelantern.io/enabled: "true"}` | namespaces whose agents may call the gateway |
| `networkPolicy.enabled` | `true` | ingress lockdown |
| `networkPolicy.egress.enabled` | `false` | egress rules for default-deny-egress namespaces |
| `networkPolicy.egress.apiServer.cidrs` | `[]` | restrict the gateway's API server egress |
| `networkPolicy.egress.ollamaInternet` | `true` | allow Ollama HTTPS egress to download models |

`*.nodeSelector`, `*.tolerations`, `*.affinity` are available for the gateway,
Ollama and Qdrant (e.g. to put Ollama on a GPU pool). Values are validated by
[`values.schema.json`](values.schema.json).
