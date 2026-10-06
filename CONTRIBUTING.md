# Contributing to KubeLantern

Thanks for your interest! Bug reports, docs fixes, new shared runbooks, new
evaluation scenarios and code are all welcome.

## Development setup

```bash
make venv          # .venv with dev + gateway extras
make test          # unit tests, no cluster needed
make lint          # ruff
make chart-lint    # helm lint + template (needs Helm)
```

For changes that touch the cluster, run the live checks on kind (see
[getting started](docs/getting-started.md)): `make test-rbac`,
`make test-gateway`, `make test-rag`, `make eval`.

## Ground rules

These keep KubeLantern's isolation promise intact. A pull request that breaks one
will not be merged, however useful the feature:

1. **Agents never get a ClusterRole**, and never read Secrets, ConfigMaps,
   `pods/exec`, or anything outside their namespace. The agent's Role lives in
   `charts/kubelantern-agent/files/role-rules.yaml` and must stay out of reach
   of Helm values. Its only write is its own `Incident` records. `tests/unit/test_rbac_policy.py` enforces this — don't
   weaken it.
2. **The gateway never reads cluster objects.** Evidence is collected by the
   agent and pushed.
3. **Namespace comes from the verified identity**, never from a request body.
4. **Audit logs contain metadata only** — no logs, evidence, diagnosis text or
   runbook content.
5. **KubeLantern stays advisory.** It never changes workloads; the agent's only
   write is its own incident records.

## Pull requests

- One topic per PR; include tests for behaviour changes.
- For diagnosis changes, run `make eval` and paste the result.
- Keep output formats stable, or explain the change.
- Update docs when behaviour or configuration changes.

## Good first contributions

- A shared runbook in `runbooks/shared/` for a failure class not yet covered.
- An evaluation scenario in `tests/eval/run_eval.py`.
- A new detector (failed Jobs, Pending pods) with unit tests.
