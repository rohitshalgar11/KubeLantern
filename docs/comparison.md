# How KubeLantern compares

There are excellent open-source AI tools for Kubernetes. KubeLantern is not a
replacement for them; it is built for one specific situation they are not
primarily designed around: **a shared cluster where each namespace belongs to
a different team, and isolation between teams is the first requirement.**

> This comparison reflects public documentation as of October 2026. These
> projects move fast — check their docs for current capabilities, and please
> open an issue if anything here is out of date or unfair.

## The tools

| | What it is |
|---|---|
| **[K8sGPT](https://k8sgpt.ai/)** (CNCF sandbox) | Scans a cluster with built-in analyzers, explains problems in plain language with an LLM backend (OpenAI, Azure, Bedrock, Gemini, LocalAI and others), can anonymize data, runs as a CLI or an operator. |
| **[HolmesGPT](https://github.com/robusta-dev/holmesgpt)** (CNCF sandbox) | Agentic troubleshooting: the model plans and calls tools (kubectl, Prometheus, logs, many integrations) to investigate alerts and questions; supports runbooks and any OpenAI-compatible LLM. |
| **[kagent](https://kagent.dev/)** (CNCF sandbox) | A framework for building and running AI agents inside Kubernetes as CRDs, with MCP tool servers; agents can analyze *and act* on the cluster. |
| **Kubernetes MCP servers** | Expose Kubernetes operations as tools to an AI assistant (an IDE, a chat client). |
| **KubeLantern** | Per-namespace agents that detect failures, build evidence and incidents, and get a grounded diagnosis from a shared model — local by default, or a hosted one — through an isolating gateway. Advisory only. |

## Design differences

| Question | Typical approach in general-purpose tools | KubeLantern |
|---|---|---|
| **Who is the user?** | cluster operators / SREs with broad access | app teams that each own a namespace, on a platform team's cluster |
| **Scope of access** | usually cluster-wide read (configurable) | one agent per namespace with a **namespaced Role**; no ClusterRole anywhere for agents or teams |
| **Secrets & ConfigMaps** | often readable, depending on RBAC | **never** readable by design; enforced by a CI policy test |
| **Who decides what to read?** | the model chooses tools at runtime (agentic) | a **fixed collector** gathers evidence before the model is involved; the gateway has no cluster read access at all |
| **Identity at the AI boundary** | the tool's own credentials | every request authenticated with an audience-bound ServiceAccount token; namespace taken from identity, mismatches rejected |
| **Knowledge / runbooks** | global, configured by the operator | shared runbooks + **team runbooks isolated per namespace**, stored as namespaced CRs, filtered at retrieval and re-checked |
| **Trigger** | on demand, or on an alert | continuous watch; **incident lifecycle** (open, cause change, scope change, resolve) with deduplication — one model call per real problem |
| **Model** | mostly hosted LLM APIs, local optional | **local by default** (Ollama in-cluster, no data leaves the cluster); optionally Azure OpenAI, OpenAI, Anthropic, Gemini or any OpenAI-compatible API |
| **Reliability of answers** | depends on the model | rules + verified facts + validation around a small model; the model cannot override a rule-confident category |
| **Actions** | some tools can change the cluster | **advisory**: never changes workloads; writes only its own incident records |

## When to use what

- **Want broad cluster analysis, many analyzers, quick start with a hosted
  model?** K8sGPT.
- **Investigating alerts across Prometheus, logs, cloud services and more, with
  an agent that decides which tools to call?** HolmesGPT.
- **Building your own AI agents that operate on Kubernetes, including actions?**
  kagent, or an MCP server with your assistant.
- **Running a shared cluster where each team must only see its own namespace —
  including what the AI sees and what knowledge it uses — and data must stay
  in-cluster?** That is the gap KubeLantern aims at.

They can coexist: a platform team may run HolmesGPT for its own investigations
while app teams get KubeLantern in their namespaces.

## What KubeLantern does *not* do (yet)

Honesty matters more than a feature table:

- **Pod-level failures only.** No metrics, traces, node problems, failed Jobs
  or stuck rollouts yet.
- **Incident history is per namespace** (`Incident` objects, last 50 / 30 days by default); there is no cross-namespace dashboard by design.
- **No notifications** until Stage 9 — output is the agent log.
- **Single cluster**, tested on kind. Not production-hardened (Stage 10).
- **CPU inference is slow** (30–50 s per diagnosis with a 1.5B model). Fine for
  incidents, not for chat.
- **Small model limits.** The deterministic layers catch category and
  fact errors, not every weak sentence.
- **Alpha.** APIs (`kubelantern.io/v1alpha1`) may change.

## Sources

- [HolmesGPT: agentic troubleshooting built for the cloud-native era — CNCF](https://www.cncf.io/blog/2026/01/07/holmesgpt-agentic-troubleshooting-built-for-the-cloud-native-era/)
- [Auto-diagnosing Kubernetes alerts with HolmesGPT and CNCF tools — CNCF](https://www.cncf.io/blog/2026/04/21/auto-diagnosing-kubernetes-alerts-with-holmesgpt-and-cncf-tools/)
- [What is kagent — kagent docs](https://www.kagent.dev/docs/kagent/introduction/what-is-kagent)
- [Meet kagent — The New Stack](https://thenewstack.io/meet-kagent-open-source-framework-for-ai-agents-in-kubernetes/)
- [K8sGPT features and capabilities — KodeKloud notes](https://notes.kodekloud.com/docs/Introduction-to-K8sGPT-and-AI-Driven-Kubernetes-Engineering/Introducing-K8sGPT-and-AI-Agents/K8sGPT-Features-and-Capabilities/page.md)
