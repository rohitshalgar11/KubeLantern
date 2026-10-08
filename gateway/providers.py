"""AI providers for the gateway: the local Ollama (default) or a hosted model.

Chat (diagnosis):
  ollama        local, in the cluster (default; nothing leaves the cluster)
  openai        OpenAI, or ANY OpenAI-compatible API: vLLM, LiteLLM, LM Studio,
                Groq, Mistral, Together, OpenRouter, Google Gemini (its
                OpenAI-compatible endpoint), ... — set the base URL
  azure-openai  Azure OpenAI (model = your deployment name)
  anthropic     Anthropic Claude (Messages API)

Embeddings (runbook search): ollama (default), openai, azure-openai.

API keys are read from a file (a mounted Secret) on every call, so a rotated
key is picked up without a restart. Keys are never logged. Only the gateway
talks to the provider; agents never do.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger("kubelantern.gateway")

CHAT_PROVIDERS = ("ollama", "openai", "azure-openai", "anthropic")
EMBED_PROVIDERS = ("ollama", "openai", "azure-openai")
OPENAI_URL = "https://api.openai.com/v1"
ANTHROPIC_URL = "https://api.anthropic.com"
AZURE_API_VERSION = "2024-10-21"
RETRYABLE = {408, 409, 425, 429, 500, 502, 503, 504, 529}


class ProviderError(Exception):
    """`permanent`: retrying won't help (bad key, no credit, unknown model) —
    the gateway reports it at once instead of retrying."""

    def __init__(self, message: str, permanent: bool = False) -> None:
        super().__init__(message)
        self.permanent = permanent


_NO_CREDIT = ("insufficient_quota", "credit_balance", "billing", "credit balance", "quota exceeded")


def _error_message(raw: str) -> str:
    """The provider's own error message from its JSON body, if there is one."""
    try:
        body = json.loads(raw)
        if isinstance(body, list) and body:          # Google returns [{"error": {...}}]
            body = body[0]
        err = body.get("error") if isinstance(body, dict) else None
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])
        if isinstance(err, str):
            return err
        if isinstance(body, dict) and body.get("message"):
            return str(body["message"])
    except (ValueError, AttributeError):
        pass
    return raw.strip()


def _http_json(url: str, payload: dict, headers: dict, timeout: float) -> dict:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read() or b"{}")


class _Remote:
    """Shared plumbing: API key from a file, retries with Retry-After, safe errors."""

    name = "remote"

    def __init__(self, model: str, key_file: str | None, timeout: float = 120, retries: int = 3,
                 post: Callable[[str, dict, dict, float], dict] = _http_json,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.model = model
        self.key_file = key_file
        self.timeout = timeout
        self.retries = retries
        self._post, self._sleep = post, sleep

    def _key(self) -> str:
        try:
            key = Path(self.key_file).read_text().strip() if self.key_file else ""
        except OSError:
            key = ""
        if not key:
            raise ProviderError(f"{self.name}: API key missing ({self.key_file or 'no key file set'})")
        return key

    def ready(self) -> tuple[bool, str]:
        if not self.model:
            return False, f"{self.name}: no model set"
        try:
            self._key()
        except ProviderError as e:
            return False, str(e)
        return True, "ok"

    def _call(self, url: str, payload: dict, headers: dict) -> dict:
        delay = 2.0
        for attempt in range(1, self.retries + 1):
            try:
                return self._post(url, payload, headers, self.timeout)
            except urllib.error.HTTPError as e:
                try:
                    raw = (e.read() or b"")[:2000].decode(errors="replace")
                except (OSError, ValueError):
                    raw = ""
                detail = _error_message(raw)[:300]
                if any(m in raw.lower() for m in _NO_CREDIT):
                    raise ProviderError(f"{self.name}: no credit / quota left on the provider "
                                        f"account — {detail}", permanent=True) from None
                if e.code in (401, 403):
                    raise ProviderError(f"{self.name}: API key rejected (HTTP {e.code}) — {detail}",
                                        permanent=True) from None
                if e.code == 404:
                    raise ProviderError(f"{self.name}: not found (HTTP 404) — check the model "
                                        f"(Azure: deployment) name and base URL — {detail}",
                                        permanent=True) from None
                if e.code in RETRYABLE and attempt < self.retries:
                    ra = e.headers.get("Retry-After") if e.headers else None
                    try:
                        wait = float(ra) if ra else delay
                    except ValueError:
                        wait = delay
                    log.info("%s: HTTP %s, retrying in %.0fs", self.name, e.code, min(wait, 30))
                    self._sleep(min(wait, 30))
                    delay *= 2
                    continue
                raise ProviderError(f"{self.name}: HTTP {e.code} {detail}".strip(),
                                    permanent=e.code == 400) from None
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                if attempt < self.retries:
                    self._sleep(delay)
                    delay *= 2
                    continue
                reason = getattr(e, "reason", None) or type(e).__name__
                raise ProviderError(f"{self.name}: unreachable ({reason})") from None
        raise ProviderError(f"{self.name}: gave up")

    def pull(self) -> None:  # only Ollama pulls models
        pass


# -- chat ------------------------------------------------------------------------------

class OpenAICompatibleLLM(_Remote):
    """OpenAI Chat Completions, and every API that copies it."""

    name = "openai"

    def __init__(self, model: str, key_file: str | None, base_url: str = OPENAI_URL,
                 max_tokens: int = 1000, temperature: float = 0.1, **kw) -> None:
        super().__init__(model, key_file, **kw)
        self.base_url = (base_url or OPENAI_URL).rstrip("/")
        self.max_tokens, self.temperature = max_tokens, temperature
        # Compatibility switches, adjusted once if the server rejects a parameter:
        # some servers have no structured outputs (json_schema), newer OpenAI models
        # want max_completion_tokens and no temperature.
        self._schema_ok = True
        self._max_param = "max_tokens"
        self._send_temperature = True

    def _url(self) -> str:
        return self.base_url + "/chat/completions"

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._key()}"}

    def _body(self, system: str, user: str, schema: dict) -> dict:
        body = {
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            self._max_param: self.max_tokens,
            "response_format": (
                {"type": "json_schema",
                 "json_schema": {"name": "diagnosis", "schema": schema, "strict": False}}
                if self._schema_ok else {"type": "json_object"}),
        }
        if self._send_temperature:
            body["temperature"] = self.temperature
        if self.model:
            body["model"] = self.model
        return body

    def _adapt(self, error: str) -> bool:
        """Adjust to a 400 'unsupported parameter' answer. True if worth retrying."""
        e = error.lower()
        if "max_tokens" in e and self._max_param == "max_tokens":
            self._max_param = "max_completion_tokens"
        elif "temperature" in e and self._send_temperature:
            self._send_temperature = False
        elif self._schema_ok and ("response_format" in e or "json_schema" in e or "schema" in e):
            self._schema_ok = False
        else:
            return False
        log.info("%s: adapting request to the server (%s)", self.name, error[:120])
        return True

    def chat(self, system: str, user: str, schema: dict) -> str:
        for _ in range(4):
            try:
                out = self._call(self._url(), self._body(system, user, schema), self._headers())
                break
            except ProviderError as e:
                if "HTTP 400" not in str(e) or not self._adapt(str(e)):
                    raise
        else:
            raise ProviderError(f"{self.name}: request rejected")
        choices = out.get("choices") or []
        if not choices:
            raise ProviderError(f"{self.name}: empty response")
        return (choices[0].get("message") or {}).get("content") or ""


class AzureOpenAILLM(OpenAICompatibleLLM):
    """Azure OpenAI: the model is the DEPLOYMENT name."""

    name = "azure-openai"

    def __init__(self, model: str, key_file: str | None, base_url: str,
                 api_version: str = AZURE_API_VERSION, **kw) -> None:
        if not base_url:
            raise ProviderError("azure-openai: set the endpoint (https://<resource>.openai.azure.com)")
        super().__init__(model, key_file, base_url=base_url, **kw)
        self.api_version = api_version or AZURE_API_VERSION

    def _url(self) -> str:
        dep = urllib.parse.quote(self.model, safe="")
        return (f"{self.base_url}/openai/deployments/{dep}/chat/completions"
                f"?api-version={self.api_version}")

    def _headers(self) -> dict:
        return {"api-key": self._key()}

    def _body(self, system, user, schema) -> dict:
        body = super()._body(system, user, schema)
        body.pop("model", None)          # the deployment is in the URL
        return body


class AnthropicLLM(_Remote):
    """Anthropic Messages API. JSON is enforced by forcing a tool whose input
    schema is the diagnosis schema."""

    name = "anthropic"

    def __init__(self, model: str, key_file: str | None, base_url: str = ANTHROPIC_URL,
                 max_tokens: int = 1000, temperature: float = 0.1, **kw) -> None:
        super().__init__(model, key_file, **kw)
        self.base_url = (base_url or ANTHROPIC_URL).rstrip("/")
        self.max_tokens, self.temperature = max_tokens, temperature

    def chat(self, system: str, user: str, schema: dict) -> str:
        out = self._call(self.base_url + "/v1/messages", {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "tools": [{"name": "report_diagnosis",
                       "description": "Report the diagnosis of the incident.",
                       "input_schema": schema}],
            "tool_choice": {"type": "tool", "name": "report_diagnosis"},
        }, {"x-api-key": self._key(), "anthropic-version": "2023-06-01"})
        for block in out.get("content") or []:
            if block.get("type") == "tool_use":
                return json.dumps(block.get("input") or {})
        text = "".join(b.get("text", "") for b in out.get("content") or [] if b.get("type") == "text")
        if text:
            return text
        raise ProviderError("anthropic: no diagnosis in the response")


# -- embeddings ------------------------------------------------------------------------------

class OpenAICompatibleEmbedder(_Remote):
    name = "openai-embeddings"

    def __init__(self, model: str, key_file: str | None, base_url: str = OPENAI_URL,
                 batch: int = 64, **kw) -> None:
        super().__init__(model, key_file, **kw)
        self.base_url = (base_url or OPENAI_URL).rstrip("/")
        self.batch = batch

    def _url(self) -> str:
        return self.base_url + "/embeddings"

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._key()}"}

    def _body(self, texts: list[str]) -> dict:
        return {"model": self.model, "input": texts}

    def embed(self, texts: list[str], kind: str) -> list[list[float]]:
        vectors: list[list[float]] = []
        for i in range(0, len(texts), self.batch):
            part = texts[i:i + self.batch]
            out = self._call(self._url(), self._body(part), self._headers())
            data = sorted(out.get("data") or [], key=lambda d: d.get("index", 0))
            if len(data) != len(part):
                raise ProviderError(f"{self.name}: expected {len(part)} vectors, got {len(data)}")
            vectors += [d["embedding"] for d in data]
        return vectors


class AzureOpenAIEmbedder(OpenAICompatibleEmbedder):
    name = "azure-openai-embeddings"

    def __init__(self, model: str, key_file: str | None, base_url: str,
                 api_version: str = AZURE_API_VERSION, **kw) -> None:
        if not base_url:
            raise ProviderError("azure-openai: set the endpoint (https://<resource>.openai.azure.com)")
        super().__init__(model, key_file, base_url=base_url, **kw)
        self.api_version = api_version or AZURE_API_VERSION

    def _url(self) -> str:
        dep = urllib.parse.quote(self.model, safe="")
        return f"{self.base_url}/openai/deployments/{dep}/embeddings?api-version={self.api_version}"

    def _headers(self) -> dict:
        return {"api-key": self._key()}

    def _body(self, texts: list[str]) -> dict:
        return {"input": texts}


# -- factories (configuration from the environment the chart sets) -----------------------------

def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def build_llm(model: str, ollama_url: str, env=_env):
    provider = env("KUBELANTERN_LLM_PROVIDER", "ollama").lower()
    timeout = float(env("KUBELANTERN_LLM_TIMEOUT_SECONDS", "180"))
    if provider == "ollama":
        from gateway.adapters import OllamaLLM

        return OllamaLLM(ollama_url, model, timeout=timeout)
    common = {"key_file": env("KUBELANTERN_LLM_KEY_FILE", "/etc/kubelantern/llm/api-key"),
              "timeout": timeout, "max_tokens": int(env("KUBELANTERN_LLM_MAX_TOKENS", "1000"))}
    base = env("KUBELANTERN_LLM_BASE_URL")
    if provider == "openai":
        return OpenAICompatibleLLM(model, base_url=base or OPENAI_URL, **common)
    if provider == "azure-openai":
        return AzureOpenAILLM(model, base_url=base,
                              api_version=env("KUBELANTERN_LLM_API_VERSION", AZURE_API_VERSION), **common)
    if provider == "anthropic":
        return AnthropicLLM(model, base_url=base or ANTHROPIC_URL, **common)
    raise ProviderError(f"unknown LLM provider '{provider}' (use one of: {', '.join(CHAT_PROVIDERS)})")


def build_embedder(model: str, ollama_url: str, env=_env):
    provider = env("KUBELANTERN_EMBED_PROVIDER", "ollama").lower()
    if provider == "ollama":
        from gateway.knowledge import OllamaEmbedder

        return OllamaEmbedder(ollama_url, model)
    key_file = env("KUBELANTERN_EMBED_KEY_FILE") or env("KUBELANTERN_LLM_KEY_FILE",
                                                        "/etc/kubelantern/llm/api-key")
    base = env("KUBELANTERN_EMBED_BASE_URL")
    if provider == "openai":
        return OpenAICompatibleEmbedder(model, key_file, base_url=base or OPENAI_URL)
    if provider == "azure-openai":
        return AzureOpenAIEmbedder(model, key_file, base_url=base,
                                   api_version=env("KUBELANTERN_EMBED_API_VERSION", AZURE_API_VERSION))
    raise ProviderError(f"unknown embeddings provider '{provider}' "
                        f"(use one of: {', '.join(EMBED_PROVIDERS)})")


def collection_name(embed_provider: str, embed_model: str) -> str:
    """One Qdrant collection per embedding model: vectors of different models
    (and dimensions) never mix, and switching back and forth needs no cleanup."""
    if embed_provider == "ollama" and embed_model == "nomic-embed-text":
        return "kubelantern-runbooks"          # the name used before providers existed
    slug = "".join(c if c.isalnum() else "-" for c in f"{embed_provider}-{embed_model}".lower())
    return f"kubelantern-runbooks-{slug.strip('-')}"[:120]
