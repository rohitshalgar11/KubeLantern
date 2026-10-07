---
title: Traffic blocked by a NetworkPolicy (timeouts, not refusals)
category: dependency
---
# Symptoms
Connections hang and then fail with a timeout, while the target Service exists
and has ready endpoints:
- `dial tcp 10.0.12.7:5432: i/o timeout`
- `connect ETIMEDOUT 10.0.12.7:6379`
- `Connection timed out` / `context deadline exceeded`
A blocked connection usually times out. "Connection refused" means the packet
arrived and nothing listened — that is a different problem.

# Why it happens
A NetworkPolicy in the client's namespace (egress) or the server's namespace
(ingress) does not allow this traffic. Shared clusters often have default-deny
policies, so every new dependency needs an explicit allow rule.

# What to check
1. `kubectl -n <ns> get networkpolicy` in both the client and the server namespace.
2. Egress: does a policy selecting the client pods allow the target pods/namespace and port?
3. Ingress: does a policy selecting the server pods allow the client's namespace and pod labels?
4. Namespace selectors match namespace LABELS (`kubernetes.io/metadata.name` is set automatically).

# Fix
Add an egress rule on the client side and an ingress rule on the server side for
exactly this port and these labels. For destinations outside the cluster, allow
the IP range (and DNS). Ask the platform team if the policies are managed centrally.
