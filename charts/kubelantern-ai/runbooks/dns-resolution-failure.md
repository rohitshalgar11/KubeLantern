---
title: DNS lookups fail inside the pod
category: dependency
---
# Symptoms
The application cannot resolve host names:
- `Temporary failure in name resolution`
- `getaddrinfo ENOTFOUND api.example.com` / `EAI_AGAIN`
- `dial tcp: lookup db on 10.96.0.10:53: no such host`
- `dial tcp: lookup db on 10.96.0.10:53: read udp ...: i/o timeout`
- `UnknownHostException: payments-db`

# Why it happens
- `no such host` / `ENOTFOUND`: the name does not exist (typo, a Service in another
  namespace used without `.<namespace>`, a private DNS zone the cluster can't see).
- `i/o timeout` / `EAI_AGAIN`: the DNS server could not be reached — often a
  NetworkPolicy with default-deny egress that does not allow port 53 to kube-dns,
  or cluster DNS (CoreDNS) is overloaded.

# What to check
1. The exact host name in the app's configuration.
2. Same namespace? `kubectl -n <ns> get svc <name>`. Other namespace? Use `<svc>.<namespace>.svc.cluster.local`.
3. Test from a pod in the namespace: `kubectl -n <ns> run dnstest --rm -it --image=busybox:1.36 -- nslookup <host>`.
4. Egress NetworkPolicies: `kubectl -n <ns> get networkpolicy` — is UDP and TCP 53 to kube-dns allowed?

# Fix
Correct the host name, add the namespace for cross-namespace Services, or allow
DNS egress (UDP and TCP 53 to the kube-dns pods). External names that only exist
in a private zone need the platform team (DNS forwarding). Many lookups to external
names can be sped up with a trailing dot (`api.example.com.`) to skip search domains.
