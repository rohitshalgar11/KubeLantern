---
title: Application gets 403 Forbidden from the Kubernetes API
category: permissions
---
# Symptoms
An app or controller that talks to the Kubernetes API logs:
- `pods is forbidden: User "system:serviceaccount:<ns>:default" cannot list resource "pods" in API group "" in the namespace "<ns>"`
- `configmaps "leader-lock" is forbidden: ... cannot update resource "configmaps"`
- `Unauthorized` / `401` from `https://kubernetes.default.svc`

# Why it happens
The pod's ServiceAccount has no Role/RoleBinding for that action, it runs as the
`default` ServiceAccount, the request targets another namespace or a cluster-wide
resource, or the token is not mounted (`automountServiceAccountToken: false`).

# What to check
1. Which ServiceAccount the pod uses: `kubectl -n <ns> get pod <pod> -o jsonpath='{.spec.serviceAccountName}'`.
2. What it may do: `kubectl auth can-i --list --as=system:serviceaccount:<ns>:<sa> -n <ns>`.
3. The verb, resource and namespace in the error message.

# Fix
Create a dedicated ServiceAccount with a namespaced Role granting exactly the verbs
and resources in the error, and bind it. Cluster-wide permissions (ClusterRoles)
are usually not available to app teams on a shared cluster: ask the platform team,
or change the app to work within its namespace.
