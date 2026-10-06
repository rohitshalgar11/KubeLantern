# Getting started

This guide brings up the full stack on a laptop with [kind](https://kind.sigs.k8s.io/):
three tenant namespaces (`demo`, `payments`, `orders`), an agent in each, and
the AI services in `kubelantern-ai`, all installed from the same Helm charts you'd
use on a real cluster.

Installing on an existing cluster with Helm or ArgoCD? See
[helm-argocd.md](helm-argocd.md).

## Prerequisites

| Tool | Version | Notes |
|---|---|---|
| Docker | 24+ | Docker Engine or Docker Desktop |
| kind | 0.23+ | local Kubernetes in Docker |
| kubectl | 1.29+ | |
| Helm | 3.14+ | installs the charts (`curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 \| bash`) |
| Python | 3.10+ | only for unit tests / running the agent locally |
| make, git | any | |

**Resources:** ~8 GB RAM free and ~15 GB disk. The first AI start downloads the
Ollama image (~4 GB) and two models (~1.3 GB). A CPU is enough; a diagnosis
takes 30–50 s with `qwen2.5:1.5b`.

### Windows

Use **WSL 2** (not WSL 1 — kind needs a real Linux kernel):

```powershell
wsl -l -v                              # VERSION column must be 2
wsl --set-version Ubuntu-22.04 2       # convert if it says 1
```

`uname -r` inside Ubuntu should end in `microsoft-standard-WSL2`. Install
Docker Engine inside WSL (with systemd enabled in `/etc/wsl.conf`) or use
Docker Desktop with WSL integration.

## Install, step by step

```bash
git clone https://github.com/<you>/kubelantern.git && cd kubelantern

make up              # 1. kind cluster, images, namespaces, agents (agent chart)
make ai-up           # 2. kubelantern-ai chart: gateway, Ollama, Qdrant, CRD, NetworkPolicies
make runbooks-demo   # 3. example team runbook for the demo namespace
```

Check it:

```bash
make ai-status       # pods, NetworkPolicies, pulled models
kubectl -n demo get runbooks
```

`make ai-up` can take 10–30 minutes on the first run while images and models
download; later runs are fast (`imagePullPolicy: IfNotPresent`, models on a PVC).

Agents that start before the platform simply retry until the gateway is up.

**Coming from a checkout before Stage 8** (kubectl/sed manifests)? Run
`make migrate-to-helm` once, then `make ai-up && make deploy-agents`. Existing
objects are handed over to Helm, and Ollama keeps its models.

## See it work

Terminal 1:

```bash
make logs NS=demo
```

Terminal 2:

```bash
make test-crashloop
```

Within a minute terminal 1 shows `[INCIDENT OPENED]` with evidence, and
30–50 s later `[INCIDENT DIAGNOSIS]` with category, cause, next steps from
the team runbook and the escalation contact.

Then walk the incident lifecycle:

```bash
make test-scale          # 3 replicas  → SCOPE CHANGED (still one incident)
make test-cause-change   # exit 1 → OOM → CAUSE CHANGED (+ new diagnosis)
make test-recover        # fix it      → RESOLVED after ~2 min healthy
```

Other scenarios: `make test-oom`, `make test-imagepull`, `make test-failures`.
Clean up with `make clean-failures`.

## Verify the security model

```bash
make test-rbac       # 83 checks: agent permissions, cross-namespace 403s, incident writes
make test-incidents  # incident survives an agent restart, then resolves (~8 min)
make test-gateway    # 8 checks: token audience, identity, namespace, NetworkPolicy
make test-rag        # 8 checks: a private payments runbook never reaches demo
make eval            # 9 diagnosis scenarios through the real gateway
```

Unit tests (no cluster):

```bash
make venv && make test && make lint
```

## Incident history

Every incident is stored as an `Incident` object in its namespace, so it
survives agent restarts (same ID, no second diagnosis) and teams can look back:

```bash
make incidents NS=demo                                   # or: kubectl -n demo get incidents
kubectl -n demo get incident <name> -o yaml              # cause history, pods, diagnosis
```

Resolved incidents are kept up to `incidents.history` (50) and
`incidents.retentionDays` (30). Set `incidents.persist=false` to keep them in
memory only; the agent then writes nothing at all.

## Notifications (Teams, Slack, webhook)

To see the messages without a real Teams channel:

```bash
make test-notify NS=demo        # test receiver + checks; prints the Teams card JSON
make notify-off NS=demo
```

Real Teams setup: [notifications.md](notifications.md).

## Onboarding a new team namespace

```bash
kubectl create namespace checkout
kubectl label namespace checkout kubelantern.io/enabled=true      # allowed to reach the gateway
make deploy-agent NS=checkout                                   # agent chart: agent + namespaced Role
make onboard-runbooks NS=checkout GROUP=checkout-developers     # team may manage its Runbooks
```

The label is what the gateway NetworkPolicy matches; without it the agent
cannot reach the AI. On a real cluster you do the same with the
[agent chart](../charts/kubelantern-agent) from your GitOps repo.

## Default-deny egress

If your namespaces deny egress by default, the agent needs DNS, the API server
and the gateway. The chart adds exactly that policy with
`networkPolicy.egress.enabled=true`. Try it locally:

```bash
make egress-lockdown NS=demo   # default-deny egress + the agent's egress policy
make test-crashloop            # incident and diagnosis still arrive
make egress-unlock NS=demo
```

Details, API server addresses and CNI notes: [helm-argocd.md](helm-argocd.md#3-namespaces-with-default-deny-egress).

## Command reference

`make help` prints this list from the Makefile. Every target is a shortcut for
plain `helm` / `kubectl` / `docker` commands; open the `Makefile` to see exactly
what each one runs. On a real cluster you don't use `make` at all (see
[helm-argocd.md](helm-argocd.md)).

**Setup and deploy (kind)**

| Target | What it does |
|---|---|
| `make up` | kind cluster + images + namespaces + agents (`cluster` + `load` + `deploy-agents`) |
| `make cluster` / `cluster-down` | create / delete the kind cluster |
| `make load` | build both images and load them into kind (`image` + `gateway-image`) |
| `make namespaces` | create the demo namespaces `demo`, `payments`, `orders` |
| `make deploy-agent NS=x [EGRESS=true]` | `helm upgrade --install` the agent chart in one namespace |
| `make deploy-agents` | the same for every namespace in `AGENT_NS` |
| `make ai-up [MODEL=…]` | `helm upgrade --install` the `kubelantern-ai` chart (gateway, Ollama, Qdrant, CRDs) |
| `make ai-down` | uninstall the AI part (agents and CRDs stay) |
| `make ai-model MODEL=qwen2.5:3b` | switch the diagnosis model |
| `make ai-pull MODEL=…` | pull a model into Ollama by hand |
| `make runbooks-demo` | apply the example team runbook in `demo` |
| `make onboard-runbooks NS=x GROUP=g` | let a team's group edit Runbooks in its namespace |
| `make migrate-to-helm` | one-off: hand pre-Stage-8 objects over to Helm |

**Watch**

| Target | What it does |
|---|---|
| `make logs NS=x` | follow an agent |
| `make incidents NS=x` | list stored incidents |
| `make ai-status` / `ai-logs` | AI pods and models / gateway audit log |
| `make notify-sink` / `notify-sink-logs` | test webhook receiver / what it received |
| `make notify-off NS=x` | turn notifications off for a namespace |

**Failure scenarios**

| Target | What it does |
|---|---|
| `make test-crashloop` / `test-oom` / `test-imagepull` | deploy one failing workload in `demo` |
| `make test-failures` | all three at once |
| `make test-scale` | scale broken-app to 3 → SCOPE CHANGED |
| `make test-cause-change` | make broken-app OOM → CAUSE CHANGED |
| `make test-recover` | fix broken-app → RESOLVED |
| `make clean-failures` | remove the failing workloads |
| `make egress-lockdown NS=x` / `egress-unlock NS=x` | simulate default-deny egress |

**Tests**

| Target | What it does |
|---|---|
| `make venv` | create `.venv` with test tools |
| `make test` / `lint` | unit tests / ruff |
| `make chart-lint` | `helm lint` + `helm template` both charts |
| `make test-rbac` | live RBAC isolation (83 checks) |
| `make test-gateway` | gateway identity, namespace and network checks (8) |
| `make test-rag` | runbook isolation between namespaces (8) |
| `make test-incidents` | incidents survive an agent restart, then resolve (7) |
| `make test-notify` | notifications end to end against a test receiver |
| `make eval` | diagnosis quality on 9 scenarios |
| `make run-local NS=x` | run the agent on your machine against the current kubeconfig |

After code changes: `make load`, then
`kubectl -n <ns> rollout restart deploy/kubelantern-agent` (the `:dev` tag doesn't change).

## Configuration

On a cluster, configure KubeLantern through the chart values: see the
[agent chart](../charts/kubelantern-agent/README.md) and
[kubelantern-ai chart](../charts/kubelantern-ai/README.md) READMEs. The charts
set these environment variables:

Agent (env on `deploy/kubelantern-agent`):

| Variable | Default | Meaning |
|---|---|---|
| `KUBELANTERN_GATEWAY_URL` | *(empty = no AI)* | gateway URL |
| `KUBELANTERN_TOKEN_PATH` | `/var/run/secrets/kubelantern/token` | projected token |
| `KUBELANTERN_RESOLVE_AFTER_SECONDS` | 300 (kind values: 120) | healthy window before RESOLVED |
| `KUBELANTERN_REMINDER_MINUTES` | 30 | ONGOING reminder interval |
| `KUBELANTERN_INCIDENT_STORE` | `true` | persist incidents as `Incident` objects |
| `KUBELANTERN_INCIDENT_HISTORY` / `_RETENTION_DAYS` | 50 / 30 | resolved incidents kept |
| `KUBELANTERN_NOTIFY_URL` | *(empty = off)* | notifier sidecar (`http://127.0.0.1:8081/v1/notify`) |
| `KUBELANTERN_NOTIFY_EVENTS` / `_DETAIL` | `diagnosis,resolved` / `summary` | what to post, and how much |
| `KUBELANTERN_RUNBOOK_SYNC_SECONDS` | 0 = off (chart: 30) | Runbook poll interval |
| `KUBELANTERN_LOG_LINES` | 5 | log lines shown in output |
| `KUBELANTERN_OUTPUT` | `text` | `json` prints the full evidence bundle |
| `KUBELANTERN_LOG_LEVEL` | `INFO` | |

Gateway (env on `deploy/kubelantern-gateway`):

| Variable | Default | Meaning |
|---|---|---|
| `KUBELANTERN_MODEL` | `qwen2.5:1.5b` | Ollama chat model |
| `OLLAMA_URL` | `http://ollama:11434` | |
| `KUBELANTERN_PULL_MODEL` | `true` | pull missing models at start |
| `KUBELANTERN_AUDIENCE` | `kubelantern-gateway` | required token audience |
| `KUBELANTERN_RATE_PER_MINUTE` | 6 | diagnoses per namespace per minute |
| `QDRANT_URL` | *(empty = in-memory store)* | vector store |
| `KUBELANTERN_EMBED_MODEL` | `nomic-embed-text` | embedding model |
| `KUBELANTERN_RUNBOOK_MIN_SCORE` | 0.35 | minimum similarity for a runbook |

## Troubleshooting

| Symptom | Fix |
|---|---|
| `make: kind: No such file or directory` | install kind |
| kind fails on Windows | you are on WSL 1 — convert to WSL 2 |
| `ai-up` rollout timeout | images still downloading; rerun `make ai-up` or watch `kubectl -n kubelantern-ai get pods -w` |
| `helm: command not found` | install Helm 3 (see prerequisites) |
| `rendered manifests contain a resource that already exists` | objects from a pre-Stage-8 install — run `make migrate-to-helm` |
| Diagnosis says "runbooks unavailable" | embedding model still pulling, or Qdrant blocked — check `make ai-logs` and `kubectl -n kubelantern-ai get networkpolicy` |
| No diagnosis at all | namespace not labelled `kubelantern.io/enabled=true`, or gateway not Ready yet (`make ai-status`) |
| Agent logs API or DNS timeouts | the namespace denies egress — deploy with `EGRESS=true` / `networkPolicy.egress.enabled` |
| 429 in agent log | rate limit; the agent retries with backoff automatically |
