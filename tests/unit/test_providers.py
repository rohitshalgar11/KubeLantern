"""Hosted AI providers: request shapes, JSON enforcement, keys, retries, fallbacks.
No network: every provider gets a fake `post`."""

import io
import json
import urllib.error

import pytest

from gateway.core import DIAGNOSIS_SCHEMA
from gateway.providers import (
    AnthropicLLM,
    AzureOpenAIEmbedder,
    AzureOpenAILLM,
    OpenAICompatibleEmbedder,
    OpenAICompatibleLLM,
    ProviderError,
    build_embedder,
    build_llm,
    collection_name,
)

ANSWER = {"summary": "db missing", "category": "dependency", "probable_cause": "no Service db",
          "confidence": "high", "evidence": [], "next_steps": ["create it"], "suggested_fix": "x"}


class FakePost:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, payload, headers, timeout):
        self.calls.append({"url": url, "payload": payload, "headers": headers, "timeout": timeout})
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def http_error(code, body=b"", retry_after=None):
    headers = {"Retry-After": retry_after} if retry_after else {}
    return urllib.error.HTTPError("https://x", code, "err", headers, io.BytesIO(body))


@pytest.fixture
def key(tmp_path):
    f = tmp_path / "api-key"
    f.write_text("sk-test-123\n")
    return str(f)


def openai_reply(content):
    return {"choices": [{"message": {"content": content}}]}


# -- OpenAI and compatible -------------------------------------------------------------

def test_openai_request_uses_structured_outputs_and_bearer_key(key):
    post = FakePost(openai_reply(json.dumps(ANSWER)))
    llm = OpenAICompatibleLLM("gpt-4o-mini", key, post=post)
    assert json.loads(llm.chat("sys", "user", DIAGNOSIS_SCHEMA)) == ANSWER
    c = post.calls[0]
    assert c["url"] == "https://api.openai.com/v1/chat/completions"
    assert c["headers"] == {"Authorization": "Bearer sk-test-123"}
    p = c["payload"]
    assert p["model"] == "gpt-4o-mini" and p["messages"][0] == {"role": "system", "content": "sys"}
    assert p["response_format"]["type"] == "json_schema"
    assert p["response_format"]["json_schema"]["schema"] == DIAGNOSIS_SCHEMA


def test_any_openai_compatible_server_by_base_url(key):
    post = FakePost(openai_reply("{}"))
    OpenAICompatibleLLM("llama3", key, base_url="http://vllm.ai.svc:8000/v1/", post=post).chat("s", "u", {})
    assert post.calls[0]["url"] == "http://vllm.ai.svc:8000/v1/chat/completions"


def test_falls_back_when_the_server_rejects_parameters(key):
    post = FakePost(
        ProviderError("openai: HTTP 400 Unsupported parameter: 'max_tokens'"),
        ProviderError("openai: HTTP 400 Unsupported value: 'temperature'"),
        ProviderError("openai: HTTP 400 response_format json_schema is not supported"),
        openai_reply("{}"),
    )
    llm = OpenAICompatibleLLM("o-model", key, post=post)
    llm._call = lambda url, payload, headers: post(url, payload, headers, 1)   # surface errors as-is
    llm.chat("s", "u", DIAGNOSIS_SCHEMA)
    last = post.calls[-1]["payload"]
    assert "max_completion_tokens" in last and "max_tokens" not in last
    assert "temperature" not in last
    assert last["response_format"] == {"type": "json_object"}
    # the adapted settings stay for the next calls
    assert llm._max_param == "max_completion_tokens" and not llm._schema_ok


def test_other_errors_are_not_retried_as_fallbacks(key):
    post = FakePost(http_error(401, b'{"error":"invalid api key"}'))
    llm = OpenAICompatibleLLM("m", key, post=post, sleep=lambda s: None)
    with pytest.raises(ProviderError, match="HTTP 401"):
        llm.chat("s", "u", {})
    assert len(post.calls) == 1


def test_rate_limits_and_outages_are_retried_with_retry_after(key):
    waits = []
    post = FakePost(http_error(429, retry_after="7"), http_error(503), openai_reply("{}"))
    OpenAICompatibleLLM("m", key, post=post, sleep=waits.append).chat("s", "u", {})
    assert waits == [7.0, 4.0] and len(post.calls) == 3


def test_missing_key_makes_the_gateway_not_ready_and_never_calls_out(tmp_path):
    post = FakePost()
    llm = OpenAICompatibleLLM("m", str(tmp_path / "nope"), post=post)
    ok, detail = llm.ready()
    assert not ok and "API key missing" in detail
    with pytest.raises(ProviderError):
        llm.chat("s", "u", {})
    assert post.calls == []


def test_key_is_read_on_every_call_so_rotation_needs_no_restart(key, tmp_path):
    post = FakePost(openai_reply("{}"), openai_reply("{}"))
    llm = OpenAICompatibleLLM("m", key, post=post)
    llm.chat("s", "u", {})
    (tmp_path / "api-key").write_text("sk-rotated")
    llm.chat("s", "u", {})
    assert post.calls[1]["headers"]["Authorization"] == "Bearer sk-rotated"


def test_key_never_appears_in_errors(key):
    post = FakePost(http_error(400, b"bad request"))
    llm = OpenAICompatibleLLM("m", key, post=post)
    with pytest.raises(ProviderError) as e:
        llm.chat("s", "u", {})
    assert "sk-test-123" not in str(e.value)


# -- Azure OpenAI ------------------------------------------------------------------------

def test_azure_uses_the_deployment_url_api_key_header_and_no_model_field(key):
    post = FakePost(openai_reply("{}"))
    llm = AzureOpenAILLM("gpt-4o-mini", key, base_url="https://contoso.openai.azure.com/", post=post)
    llm.chat("s", "u", DIAGNOSIS_SCHEMA)
    c = post.calls[0]
    assert c["url"] == ("https://contoso.openai.azure.com/openai/deployments/gpt-4o-mini/"
                        "chat/completions?api-version=2024-10-21")
    assert c["headers"] == {"api-key": "sk-test-123"} and "model" not in c["payload"]


def test_azure_needs_an_endpoint(key):
    with pytest.raises(ProviderError, match="endpoint"):
        AzureOpenAILLM("dep", key, base_url="")


# -- Anthropic ----------------------------------------------------------------------------

def test_anthropic_forces_a_tool_so_the_answer_is_json(key):
    post = FakePost({"content": [{"type": "text", "text": "Here it is"},
                                 {"type": "tool_use", "name": "report_diagnosis", "input": ANSWER}]})
    llm = AnthropicLLM("claude-haiku-4-5-20251001", key, post=post)
    assert json.loads(llm.chat("sys", "user", DIAGNOSIS_SCHEMA)) == ANSWER
    c = post.calls[0]
    assert c["url"] == "https://api.anthropic.com/v1/messages"
    assert c["headers"] == {"x-api-key": "sk-test-123", "anthropic-version": "2023-06-01"}
    p = c["payload"]
    assert p["system"] == "sys" and p["messages"] == [{"role": "user", "content": "user"}]
    assert p["tools"][0]["input_schema"] == DIAGNOSIS_SCHEMA
    assert p["tool_choice"] == {"type": "tool", "name": "report_diagnosis"}


def test_anthropic_overloaded_is_retried(key):
    post = FakePost(http_error(529), {"content": [{"type": "tool_use", "input": {}}]})
    AnthropicLLM("m", key, post=post, sleep=lambda s: None).chat("s", "u", {})
    assert len(post.calls) == 2


# -- embeddings ------------------------------------------------------------------------------

def test_openai_embeddings_batch_and_keep_order(key):
    def reply(n, offset):
        return {"data": [{"index": i, "embedding": [float(offset + i)]} for i in reversed(range(n))]}

    post = FakePost(reply(2, 0), reply(1, 2))
    emb = OpenAICompatibleEmbedder("text-embedding-3-small", key, batch=2, post=post)
    assert emb.embed(["a", "b", "c"], "document") == [[0.0], [1.0], [2.0]]
    assert post.calls[0]["payload"] == {"model": "text-embedding-3-small", "input": ["a", "b"]}
    assert post.calls[0]["url"] == "https://api.openai.com/v1/embeddings"


def test_azure_embeddings_url(key):
    post = FakePost({"data": [{"index": 0, "embedding": [1.0]}]})
    AzureOpenAIEmbedder("emb-dep", key, base_url="https://c.openai.azure.com", post=post).embed(["a"], "query")
    assert post.calls[0]["url"] == ("https://c.openai.azure.com/openai/deployments/emb-dep/"
                                    "embeddings?api-version=2024-10-21")
    assert post.calls[0]["payload"] == {"input": ["a"]}


# -- configuration ----------------------------------------------------------------------------

def env_of(d):
    return lambda name, default="": d.get(name, default)


def test_factory_defaults_to_the_local_ollama():
    from gateway.adapters import OllamaLLM
    from gateway.knowledge import OllamaEmbedder

    assert isinstance(build_llm("qwen2.5:1.5b", "http://ollama:11434", env=env_of({})), OllamaLLM)
    assert isinstance(build_embedder("nomic-embed-text", "http://ollama:11434", env=env_of({})),
                      OllamaEmbedder)


def test_factory_builds_each_hosted_provider(key):
    e = {"KUBELANTERN_LLM_KEY_FILE": key, "KUBELANTERN_LLM_TIMEOUT_SECONDS": "60"}
    llm = build_llm("gpt-4o-mini", "", env=env_of({**e, "KUBELANTERN_LLM_PROVIDER": "openai"}))
    assert isinstance(llm, OpenAICompatibleLLM) and llm.timeout == 60 and llm.ready() == (True, "ok")
    az = build_llm("dep", "", env=env_of({**e, "KUBELANTERN_LLM_PROVIDER": "azure-openai",
                                          "KUBELANTERN_LLM_BASE_URL": "https://c.openai.azure.com",
                                          "KUBELANTERN_LLM_API_VERSION": "2025-01-01-preview"}))
    assert isinstance(az, AzureOpenAILLM) and az.api_version == "2025-01-01-preview"
    assert isinstance(build_llm("claude", "", env=env_of({**e, "KUBELANTERN_LLM_PROVIDER": "anthropic"})),
                      AnthropicLLM)
    emb = build_embedder("text-embedding-3-small", "",
                         env=env_of({**e, "KUBELANTERN_EMBED_PROVIDER": "openai"}))
    assert isinstance(emb, OpenAICompatibleEmbedder) and emb.key_file == key
    with pytest.raises(ProviderError, match="unknown LLM provider"):
        build_llm("m", "", env=env_of({"KUBELANTERN_LLM_PROVIDER": "skynet"}))
    with pytest.raises(ProviderError, match="unknown embeddings provider"):
        build_embedder("m", "", env=env_of({"KUBELANTERN_EMBED_PROVIDER": "anthropic"}))


def test_one_vector_collection_per_embedding_model():
    assert collection_name("ollama", "nomic-embed-text") == "kubelantern-runbooks"   # unchanged
    a = collection_name("openai", "text-embedding-3-small")
    b = collection_name("azure-openai", "text-embedding-3-small")
    assert a != b and a.startswith("kubelantern-runbooks-") and " " not in a


def test_a_hosted_model_gets_the_same_redacted_request_through_the_gateway(key):
    """Through the real gateway path: authentication, redaction, graph, validation."""
    from tests.unit.test_gateway import call, evidence, incident, make_gateway

    reply = openai_reply(json.dumps(ANSWER))
    post = FakePost(reply, reply, reply)        # validation may ask the model again
    gw, _ = make_gateway(llm=OpenAICompatibleLLM("gpt-4o-mini", key, post=post))
    status, body = call(gw, "demo-agent", {"incident": incident(), "evidence": evidence()})
    assert status == 200 and body["diagnosis"]["category"] == "dependency"
    sent = json.dumps(post.calls[0]["payload"])
    assert "hunter2" not in sent and "password=[REDACTED]" in sent     # redacted before it leaves
    assert body["model"] == "gpt-4o-mini"


# -- errors retrying won't fix ------------------------------------------------------------------

NO_CREDIT = (b'{"error": {"message": "You have no credits remaining. Add credits to continue.", '
             b'"type": "insufficient_quota", "code": "credit_balance_exhausted"}}')


def test_no_credit_fails_at_once_with_a_clear_message(key):
    waits = []
    post = FakePost(http_error(429, NO_CREDIT))
    llm = OpenAICompatibleLLM("gpt-4o-mini", key, post=post, sleep=waits.append)
    with pytest.raises(ProviderError) as e:
        llm.chat("s", "u", {})
    assert e.value.permanent and "no credit" in str(e.value) and "Add credits" in str(e.value)
    assert len(post.calls) == 1 and waits == []          # not retried


def test_rejected_key_is_permanent_and_named(key):
    post = FakePost(http_error(401, b'{"error": {"message": "Incorrect API key provided"}}'))
    with pytest.raises(ProviderError) as e:
        OpenAICompatibleLLM("m", key, post=post).chat("s", "u", {})
    assert e.value.permanent and "API key rejected" in str(e.value)
    assert "Incorrect API key provided" in str(e.value)


def test_a_plain_rate_limit_is_still_retried(key):
    waits = []
    post = FakePost(http_error(429, b'{"error": {"message": "Rate limit reached", "type": "requests"}}'),
                    openai_reply("{}"))
    OpenAICompatibleLLM("m", key, post=post, sleep=waits.append).chat("s", "u", {})
    assert len(post.calls) == 2 and waits


def test_gateway_reports_a_permanent_provider_error_as_424_not_retryable(key):
    from tests.unit.test_gateway import call, evidence, incident, make_gateway

    post = FakePost(http_error(429, NO_CREDIT))
    gw, _ = make_gateway(llm=OpenAICompatibleLLM("gpt-4o-mini", key, post=post))
    status, body = call(gw, "demo-agent", {"incident": incident(), "evidence": evidence()})
    assert status == 424 and "no credit" in body["error"]
    assert "retry_after_seconds" not in body


def test_error_message_from_a_list_body_like_googles(key):
    body = b'[{"error": {"code": 404, "message": "This model models/x is no longer available."}}]'
    post = FakePost(http_error(404, body))
    with pytest.raises(ProviderError) as e:
        OpenAICompatibleLLM("x", key, post=post).chat("s", "u", {})
    assert str(e.value).endswith("This model models/x is no longer available.")
