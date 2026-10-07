---
title: TLS certificate errors (unknown authority, expired, hostname mismatch)
---
# Symptoms
- `x509: certificate signed by unknown authority`
- `x509: certificate has expired or is not yet valid`
- `x509: certificate is valid for a.example.com, not b.example.com`
- `SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer certificate`
- `PKIX path building failed: unable to find valid certification path to requested target`
- `Error: self-signed certificate in certificate chain` / `UNABLE_TO_VERIFY_LEAF_SIGNATURE`

# Why it happens
- Unknown authority / PKIX / unable to get local issuer: the server uses a private
  or corporate CA (or a TLS-inspecting proxy) that the image does not trust, or the
  server sends an incomplete chain. Slim/distroless images may have no CA bundle at all.
- Expired: the server's (or a client) certificate expired, or the clock is wrong.
- Hostname mismatch: the app connects by a name the certificate doesn't cover
  (an IP, an internal alias, a Service name).

# What to check
1. Which host the app connects to, and the certificate it presents:
   `openssl s_client -connect <host>:443 -servername <host> -showcerts`.
2. Expiry and names: `openssl x509 -noout -dates -subject -ext subjectAltName`.
3. Does the image contain CA certificates (`ca-certificates` package; Java truststore)?
4. Is there a corporate proxy re-signing TLS traffic?

# Fix
Add the private/corporate CA to the image's trust store (or mount it, and point
`SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, `NODE_EXTRA_CA_CERTS` or the Java truststore
at it), renew expired certificates (check cert-manager `Certificate` objects), and
connect using a host name the certificate covers. Never disable verification in production.
