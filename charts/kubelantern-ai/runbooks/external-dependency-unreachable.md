---
title: External service unreachable from the cluster (egress, firewall, private endpoint)
category: dependency
---
# Symptoms
Calls to something outside the cluster fail, while the same call works from a laptop:
- `connect ETIMEDOUT 52.x.x.x:443`
- `Connection timed out` / `i/o timeout` to a public or private IP
- `SSL handshake timed out` / `Read timed out`
- Managed databases: `could not connect to server: Connection timed out ... port 5432`

# Why it happens
Egress from the cluster is restricted: a firewall or egress gateway only allows
listed destinations, a default-deny NetworkPolicy blocks it, the database firewall
does not include the cluster's outbound IP, or a private endpoint / private DNS
zone is not linked to the cluster's network.

# What to check
1. Which host and port: from the error, or the app's configuration.
2. Test from the namespace: `kubectl -n <ns> run nettest --rm -it --image=busybox:1.36 -- nc -zv -w 5 <host> <port>`.
3. Does the host resolve to a private IP from inside the cluster (private endpoint) or a public one?
4. NetworkPolicy egress in the namespace, and the cloud firewall / NSG / egress allow-list.
5. Database firewall rules: is the cluster's outbound (NAT) IP allowed?

# Fix
Allow the destination where it is blocked: NetworkPolicy egress, the cluster's
egress firewall, or the service's own firewall (add the cluster's outbound IP).
For private endpoints, the private DNS zone must be linked to the cluster's VNet/VPC.
Requests to change shared firewalls usually go to the platform/network team.
