"""Concrete adapters: Ollama over HTTP and Kubernetes TokenReview."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

log = logging.getLogger("kubelantern.gateway")


class OllamaLLM:
    def __init__(self, base_url: str, model: str, timeout: float = 180,
                 num_ctx: int = 4096, num_predict: int = 600, temperature: float = 0.1) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.options = {"num_ctx": num_ctx, "num_predict": num_predict, "temperature": temperature}

    def _post(self, path: str, payload: dict, timeout: float) -> dict:
        req = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read() or b"{}")

    def chat(self, system: str, user: str, schema: dict) -> str:
        out = self._post("/api/chat", {
            "model": self.model,
            "stream": False,
            "format": schema,  # Ollama structured outputs (JSON schema)
            "options": self.options,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }, self.timeout)
        return (out.get("message") or {}).get("content", "")

    def models(self) -> list[str]:
        with urllib.request.urlopen(self.base_url + "/api/tags", timeout=5) as resp:
            data = json.loads(resp.read() or b"{}")
        return [m.get("name", "") for m in data.get("models", [])]

    def ready(self) -> tuple[bool, str]:
        try:
            names = self.models()
        except (urllib.error.URLError, OSError, ValueError) as e:
            return False, f"ollama unreachable: {e}"
        wanted = self.model if ":" in self.model else self.model + ":latest"
        if wanted not in names and self.model not in names:
            return False, f"model {self.model} not pulled (have: {', '.join(names) or 'none'})"
        return True, "ok"

    def pull(self) -> None:
        log.info("pulling model %s (this can take a few minutes)", self.model)
        self._post("/api/pull", {"model": self.model, "name": self.model, "stream": False}, 1800)
        log.info("model %s ready", self.model)


class KubeTokenReviewer:
    def __init__(self) -> None:
        from kubernetes import client, config

        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()
        self._api = client.AuthenticationV1Api()
        self._client = client

    def review(self, token: str, audience: str) -> tuple[bool, str | None, str | None]:
        c = self._client
        body = c.V1TokenReview(spec=c.V1TokenReviewSpec(token=token, audiences=[audience]))
        try:
            st = self._api.create_token_review(body).status
        except Exception as e:  # noqa: BLE001
            log.warning("TokenReview failed: %s", e)
            return False, None, "token review unavailable"
        if not st or not st.authenticated:
            return False, None, (st.error if st else None)
        if audience not in (st.audiences or []):
            return False, None, "audience mismatch"
        return True, st.user.username if st.user else None, None
