---
title: Kafka client cannot connect or find topics
---
# Symptoms
- `Connection to node -1 (localhost/127.0.0.1:9092) could not be established. Broker may not be available.`
- `Bootstrap broker kafka:9092 (id: -1 rack: null) disconnected`
- `org.apache.kafka.common.errors.TimeoutException: Topic orders not present in metadata after 60000 ms.`
- `LEADER_NOT_AVAILABLE` / `UNKNOWN_TOPIC_OR_PARTITION`
- `SaslAuthenticationException: Authentication failed`

# Why it happens
The client reaches the bootstrap server but the broker advertises addresses the pod
can't reach (`advertised.listeners` set to localhost or an external name), the topic
doesn't exist and auto-creation is off, the client uses the wrong security protocol
(PLAINTEXT vs SASL_SSL), or credentials are wrong.

# What to check
1. Bootstrap servers and security settings in the app's configuration.
2. If the error mentions localhost or an unexpected host: the broker's advertised listeners.
3. Does the topic exist (and does the user have ACLs for it)?

# Fix
Use the bootstrap address and listener meant for in-cluster clients, create the
topic (topics should be created by your platform tooling, not by apps), set the
right `security.protocol` / SASL mechanism, and fix credentials or ACLs.
