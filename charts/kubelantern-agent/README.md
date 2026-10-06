# kubelantern-agent

The KubeLantern namespace agent. Install **one release per namespace** you want
covered. It watches pods in that namespace, turns failures into incidents with
evidence, and asks the KubeLantern gateway (from the
[`kubelantern-ai`](../kubelantern-ai) chart) for a diagnosis.

**Security:** the chart creates a **namespaced Role** with a fixed, read-mostly
profile. It never creates a ClusterRole, and never grants access to Secrets,
ConfigMaps or `pods/exec`. Its only write is its own `Incident` records in this
namespace. The rules live in
[`files/role-rules.yaml`](files/role-rules.yaml) and deliberately can't be
changed through values.

## Install

```bash
helm install kubelantern-agent oci://ghcr.io/rohitshalgar11/charts/kubelantern-agent \
  --namespace payments
kubectl label namespace payments kubelantern.io/enabled=true --overwrite
```

The namespace label is required: the gateway's NetworkPolicy only admits agents
from labelled namespaces. This chart doesn't own your Namespace object, so set
the label wherever you create namespaces.

### As a dependency of another chart

```yaml
# Chart.yaml of your chart
dependencies:
  - name: kubelantern-agent
    alias: kubelantern
    version: 0.2.x
    repository: oci://ghcr.io/rohitshalgar11/charts
    condition: kubelantern.enabled
```

```yaml
# values for one namespace
kubelantern:
  enabled: true
  networkPolicy:
    egress:
      enabled: true        # if your namespaces are default-deny egress
```

See [docs/helm-argocd.md](../../docs/helm-argocd.md) for the full ArgoCD setup.

## Values

| Key | Default | Description |
|---|---|---|
| `image.repository` | `ghcr.io/rohitshalgar11/kubelantern-agent` | agent image |
| `image.tag` | chart `appVersion` | |
| `image.pullPolicy` | `IfNotPresent` | |
| `imagePullSecrets` | `[]` | for private mirrors (e.g. ACR) |
| `gateway.url` | `http://kubelantern-gateway.kubelantern-ai.svc:8080` | empty = no AI diagnosis |
| `gateway.audience` | `kubelantern-gateway` | token audience; must match the `kubelantern-ai` chart |
| `gateway.namespace` | `kubelantern-ai` | used by the egress NetworkPolicy |
| `gateway.port` | `8080` | |
| `gateway.tokenExpirationSeconds` | `3600` | projected token lifetime |
| `incidents.resolveAfterSeconds` | `300` | healthy window before RESOLVED |
| `incidents.reminderMinutes` | `30` | ONGOING reminder interval |
| `incidents.persist` | `true` | store incidents as `Incident` objects in this namespace (survive restarts, `kubectl get incidents`); `false` keeps them in memory |
| `incidents.history` | `50` | resolved incidents kept |
| `incidents.retentionDays` | `30` | maximum age of a resolved incident |
| `incidents.viewers.groups` / `users` | `[]` | may read Incidents in this namespace |
| `runbooks.syncSeconds` | `30` | Runbook push interval; `0` = off |
| `runbooks.items` | `[]` | team runbooks to create (name, title, category, workloads, content) |
| `runbooks.editors.groups` / `users` | `[]` | may edit Runbooks in this namespace |
| `notifications.enabled` | `false` | post incidents to Teams / Slack / a webhook (see [docs/notifications.md](../../docs/notifications.md)) |
| `notifications.existingSecret` | `kubelantern-notify` | Secret in this namespace with keys `teams` / `slack` / `webhook` (URLs) and `smtp-username` / `smtp-password` |
| `notifications.channels` | `["teams"]` | `teams`, `slack`, `webhook`, `email` |
| `notifications.email.to` | `[]` | email recipients, e.g. a Teams channel's email address |
| `notifications.email.from` | `""` | sender address |
| `notifications.email.smtpHost` / `smtpPort` | `""` / `587` | SMTP server |
| `notifications.email.tls` | `starttls` | `starttls`, `ssl` or `none` (testing only) |
| `notifications.events` | `["diagnosis", "resolved"]` | also: `opened`, `cause_changed`, `scope_changed`, `ongoing` |
| `notifications.detail` | `summary` | `full` adds evidence and log lines (redacted) |
| `notifications.maxPerMinute` | `20` | messages per minute from this namespace |
| `notifications.allowInsecure` | `false` | allow `http://` webhooks and SMTP without TLS (testing only) |
| `notifications.egressCidrs` | `[]` | with egress lockdown: where HTTPS may go (empty = anywhere) |
| `networkPolicy.egress.enabled` | `false` | allow DNS, API server and gateway under default-deny egress |
| `networkPolicy.egress.dns.namespace` | `kube-system` | |
| `networkPolicy.egress.dns.podLabels` | `{k8s-app: kube-dns}` | |
| `networkPolicy.egress.apiServer.cidrs` | `[]` | restrict API server egress to these CIDRs |
| `networkPolicy.egress.apiServer.ports` | `[443, 6443]` | |
| `logLevel` | `INFO` | |
| `output` | `text` | `json` prints the full evidence bundle |
| `logLines` | `5` | log lines shown in text output |
| `resources` | 20m/64Mi → 200m/128Mi | |
| `podAnnotations`, `podLabels`, `nodeSelector`, `tolerations`, `affinity`, `priorityClassName` | empty | scheduling and metadata |

Values are validated by [`values.schema.json`](values.schema.json); typos fail
the install instead of being ignored.
