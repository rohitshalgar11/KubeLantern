# Security policy

KubeLantern's purpose is isolation between teams on a shared cluster, so security
reports are taken seriously.

## Reporting a vulnerability

Please **do not open a public issue** for a vulnerability. Use GitHub's
**private vulnerability reporting** on this repository (Security → Report a
vulnerability). Include steps to reproduce and the impact.

You should get a response within a week.

## In scope

- One namespace reading another namespace's objects, logs, incidents,
  diagnoses or runbooks.
- Calling the gateway without a valid agent identity, or as another namespace.
- Reaching Ollama or Qdrant without going through the gateway.
- Secrets reaching the model, the audit log or the agent output unredacted.
- Prompt injection that changes a diagnosis' category against the rules, or
  causes data from another namespace to be returned.

## Supported versions

KubeLantern is alpha; only the `main` branch is supported.

The design is described in [docs/security.md](docs/security.md).
