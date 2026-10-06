"""KubeLantern notifier — posts incident updates to Teams, Slack or a webhook.

Runs as a sidecar next to the agent, listening on 127.0.0.1 only. It is the
only container that can read the webhook URLs (a mounted Secret); the agent
container never sees them, and the ServiceAccount has no Secret access.
"""

KINDS = ("opened", "diagnosis", "diagnosis_failed", "cause_changed", "scope_changed",
         "ongoing", "resolved")
CHANNELS = ("teams", "slack", "webhook", "email")
