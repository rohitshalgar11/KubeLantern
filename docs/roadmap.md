# Roadmap

| Stage | Status |
|---|---|
| 1–7 · agent, collector, incidents, gateway, graph, RAG | ✅ done |
| 8 · Helm charts + GitOps, incident persistence | ✅ |
| 9 · Per-namespace notifications | built, live test pending |
| 10 · Production hardening | planned |

## Stage 8 — Helm charts, GitOps and incident persistence

**Why not an operator?** Shared clusters are commonly run with GitOps (ArgoCD or
Flux) from Helm charts. There, ArgoCD already does an operator's job: it creates
the objects, corrects drift and removes them. A KubeLantern operator would
compete with it, and would need broad RBAC to create Roles in every namespace.
Plain charts keep the agent's **namespaced Role** exactly as it is, applied by
the GitOps tool that already manages the namespace.

Done:
- [`kubelantern-ai`](../charts/kubelantern-ai) chart: gateway,
  Ollama, Qdrant, `Runbook` CRD (ArgoCD sync wave -1, kept on uninstall),
  ingress and optional egress NetworkPolicies, external Ollama option.
- [`kubelantern-agent`](../charts/kubelantern-agent) chart: fixed namespaced
  Role (not configurable through values), optional egress policy for
  default-deny-egress namespaces, team runbooks and runbook editors from values,
  usable standalone or as a dependency of another chart.
- Values schemas, CI (`helm lint`, `helm template`, kubeconform, an e2e test under
  default-deny egress), release workflow publishing multi-arch images and OCI
  charts to GHCR.
- [Helm & ArgoCD guide](helm-argocd.md).

- `Incident` CRD (`incidents.kubelantern.io`): incidents persisted as objects in
  each team namespace. They survive agent restarts (same ID, no duplicate
  OPENED, no second diagnosis), and teams can run `kubectl get incidents` with
  namespaced RBAC (`incidents.viewers`). The agent's only write is its own
  incident records, never workloads. Resolved incidents are pruned
  automatically (50 / 30 days).

## Stage 9 — Notifications

Built ([docs](notifications.md)):
- Microsoft Teams (Workflows webhook, Adaptive Card), email (e.g. a Teams
  channel's email address, any SMTP service), Slack (incoming webhook) and a
  generic JSON webhook, configured per namespace.
- Default: one message when the diagnosis is ready, one when resolved; other
  events optional. Restored incidents don't re-notify.
- Webhook URLs in a Secret the team creates with its usual tool; mounted only
  into a notifier sidecar listening on 127.0.0.1. The agent container never
  sees them, and the notifier has no Kubernetes API token.
- Off by default; `summary` detail (no logs) by default; redacted twice;
  https only; rate-limited; retries with `Retry-After`.

Not done: threading updates of one incident into a single Teams/Slack thread
(webhooks can't reply to a message; it needs a bot/app integration).

## Stage 10 — Production hardening

- Gateway: HA replicas, TLS, persistent audit sink, metrics (Prometheus).
- GPU support and model selection guidance; OpenAI-compatible model servers
  such as vLLM alongside Ollama (today the gateway speaks the Ollama API only).
- Better retrieval ranking (known nit: the wrong-port scenario ranks the
  missing-Service runbook first).
- Validate that the model's summary only claims evidence that exists (seen
  live: "the logs show a stack trace" when they didn't).
- More detectors: failing Jobs, stuck rollouts, Pending pods, node pressure.
- Supply chain: signed images, SBOM, pinned dependencies.
- Load and fairness testing with many namespaces.

Ideas and pull requests are welcome — see [CONTRIBUTING.md](../CONTRIBUTING.md).
