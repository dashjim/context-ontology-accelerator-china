# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the OpenAI-compatible backend (LLM_PROVIDER=openai_compatible).

Covers provider selection (default Bedrock), settings/secret loading, the
bedrock-runtime stand-in's Converse and embedding translation, error mapping onto
the botocore types callers already handle, and the guardrail-skip behaviour of
the shared BedrockClient / BedrockEmbedder / GuardrailScreener under this provider.
HTTP is mocked with ``httpx.MockTransport``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from unittest.mock import MagicMock, patch

import httpx
import pytest
from botocore.config import Config
from botocore.exceptions import ClientError, EndpointConnectionError, ReadTimeoutError
from coa_common import llm_provider, openai_compat
from coa_common.openai_compat import OpenAICompatRuntimeClient, OpenAICompatSettings
from structlog.testing import capture_logs

pytestmark = pytest.mark.unit

BASE_URL = "https://llm.example.test/v1"


class _Server:
    """MockTransport handler replaying queued responses and recording requests."""

    def __init__(self, *responses: httpx.Response | Callable[[httpx.Request], httpx.Response]) -> None:
        self._responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = self._responses.pop(0)
        return response(request) if callable(response) else response

    def body(self, index: int = -1) -> dict:
        return json.loads(self.requests[index].content)


def _chat(text: str = "hello", finish_reason: str = "stop", usage: dict | None = None) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": finish_reason}
            ],
            "usage": usage or {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14},
        },
    )


def _embeddings(*vectors: list[float]) -> httpx.Response:
    # Returned out of order on purpose: the client must re-sort by ``index``.
    data = [{"index": i, "embedding": v} for i, v in enumerate(vectors)]
    return httpx.Response(200, json={"data": list(reversed(data)), "usage": {"prompt_tokens": 7}})


def _raise(exc_type: type[httpx.TransportError]) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc_type("boom", request=request)

    return handler


@pytest.fixture
def openai_env(monkeypatch):
    """Select the OpenAI-compatible provider with a test endpoint and model routing."""
    monkeypatch.setenv("LLM_PROVIDER", "openai_compatible")
    monkeypatch.setenv("OPENAI_COMPAT_BASE_URL", BASE_URL + "/")
    monkeypatch.setenv("OPENAI_COMPAT_CHAT_MODEL", "deepseek-chat")
    monkeypatch.setenv("OPENAI_COMPAT_EMBED_MODEL", "text-embedding-v4")
    monkeypatch.setenv("OPENAI_COMPAT_MODEL_MAP", json.dumps({"us.anthropic.claude-sonnet-5": "qwen-max"}))
    monkeypatch.delenv("OPENAI_COMPAT_API_KEY_SECRET_ARN", raising=False)
    monkeypatch.setattr(openai_compat, "retry_delay", lambda attempt: 0.0)
    monkeypatch.setattr(llm_provider, "_WARNED_GUARDRAILS", set())
    openai_compat.load_settings.cache_clear()
    yield
    openai_compat.load_settings.cache_clear()


@pytest.fixture
def inject_transport(monkeypatch):
    """Route every stand-in built via ``from_boto_config`` through a MockTransport."""

    def install(server: _Server) -> None:
        original = OpenAICompatRuntimeClient.from_boto_config.__func__

        def from_boto_config(cls, config, **kwargs):
            return original(cls, config, transport=httpx.MockTransport(server), **kwargs)

        monkeypatch.setattr(OpenAICompatRuntimeClient, "from_boto_config", classmethod(from_boto_config))

    return install


def _runtime(server: _Server, *, max_attempts: int = 3) -> OpenAICompatRuntimeClient:
    return OpenAICompatRuntimeClient(
        openai_compat.load_settings(), max_attempts=max_attempts, transport=httpx.MockTransport(server)
    )


# ── provider selection ───────────────────────────────────────────────────


class TestProviderSelection:
    def test_defaults_to_bedrock_when_unset(self, monkeypatch):
        monkeypatch.delenv("LLM_PROVIDER", raising=False)
        assert llm_provider.llm_provider() == "bedrock"
        assert not llm_provider.is_openai_compatible()

    def test_empty_value_is_bedrock(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "")
        assert llm_provider.llm_provider() == "bedrock"

    def test_openai_compatible_is_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", " OpenAI_Compatible ")
        assert llm_provider.is_openai_compatible()

    def test_unknown_value_fails_loudly(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "openai")
        with pytest.raises(ValueError, match="LLM_PROVIDER"):
            llm_provider.llm_provider()

    def test_bedrock_default_keeps_boto3_embedder_client(self, monkeypatch):
        from coa_common.embeddings import BedrockEmbedder

        monkeypatch.delenv("LLM_PROVIDER", raising=False)
        with patch("coa_common.embeddings.boto3") as boto3:
            client = BedrockEmbedder(region="us-east-1").client
        boto3.client.assert_called_once()
        assert boto3.client.call_args.args == ("bedrock-runtime",)
        assert client is boto3.client.return_value


# ── settings ─────────────────────────────────────────────────────────────


class TestSettings:
    def test_base_url_required(self, openai_env, monkeypatch):
        monkeypatch.delenv("OPENAI_COMPAT_BASE_URL")
        with pytest.raises(ValueError, match="OPENAI_COMPAT_BASE_URL"):
            openai_compat.load_settings()

    def test_model_map_must_be_object_of_strings(self, openai_env, monkeypatch):
        monkeypatch.setenv("OPENAI_COMPAT_MODEL_MAP", '["deepseek-chat"]')
        with pytest.raises(ValueError, match="OPENAI_COMPAT_MODEL_MAP"):
            openai_compat.load_settings()

    def test_model_resolution_map_then_default_then_passthrough(self):
        settings = OpenAICompatSettings(
            base_url=BASE_URL, chat_model="deepseek-chat", model_map={"bedrock-id": "mapped"}
        )
        assert settings.resolve_chat_model("bedrock-id") == "mapped"
        assert settings.resolve_chat_model("other") == "deepseek-chat"
        assert OpenAICompatSettings(base_url=BASE_URL).resolve_chat_model("qwen-plus") == "qwen-plus"
        assert OpenAICompatSettings(base_url=BASE_URL).resolve_embed_model("bge-m3") == "bge-m3"

    @pytest.mark.parametrize("secret_string", ['{"api_key": "sk-test"}', "sk-test"])
    def test_api_key_from_secrets_manager(self, openai_env, monkeypatch, secret_string):
        arn = "arn:aws-cn:secretsmanager:cn-northwest-1:123456789012:secret:llm-key-AbCdEf"
        monkeypatch.setenv("OPENAI_COMPAT_API_KEY_SECRET_ARN", arn)
        sm = MagicMock()
        sm.get_secret_value.return_value = {"SecretString": secret_string}
        with patch("coa_common.openai_compat.boto3.client", return_value=sm) as factory:
            settings = openai_compat.load_settings()
            openai_compat.load_settings()  # cached: fetched once per process
        factory.assert_called_once()
        assert factory.call_args.args == ("secretsmanager",)
        assert factory.call_args.kwargs["region_name"] == "cn-northwest-1"
        sm.get_secret_value.assert_called_once_with(SecretId=arn)
        assert settings.headers() == {"Authorization": "Bearer sk-test"}

    def test_no_secret_means_no_auth_header(self, openai_env):
        settings = openai_compat.load_settings()
        assert settings.api_key is None
        assert settings.headers() == {}
        assert settings.base_url == BASE_URL  # trailing slash stripped


# ── Converse stand-in ────────────────────────────────────────────────────


class TestConverse:
    def test_request_and_response_mapping(self, openai_env):
        server = _Server(_chat("the answer"))
        resp = _runtime(server).converse(
            modelId="us.anthropic.claude-sonnet-5",
            messages=[{"role": "user", "content": [{"text": "context"}, {"text": "question"}]}],
            system=[{"text": "be terse"}],
            inferenceConfig={"maxTokens": 256, "temperature": 0, "topP": 0.1},
        )
        request = server.requests[0]
        assert str(request.url) == f"{BASE_URL}/chat/completions"
        assert server.body() == {
            "model": "qwen-max",
            "messages": [
                {"role": "system", "content": "be terse"},
                {"role": "user", "content": "context\n\nquestion"},
            ],
            "max_tokens": 256,
            "temperature": 0,
            "top_p": 0.1,
        }
        assert resp["output"]["message"]["content"] == [{"text": "the answer"}]
        assert resp["stopReason"] == "end_turn"
        assert resp["usage"] == {"inputTokens": 11, "outputTokens": 3, "totalTokens": 14}

    def test_empty_system_is_omitted_and_unmapped_model_uses_default(self, openai_env):
        server = _Server(_chat())
        _runtime(server).converse(
            modelId="us.anthropic.claude-haiku-4-5-20251001-v1:0",
            messages=[{"role": "user", "content": [{"text": "hi"}]}],
            system=[],
            inferenceConfig={"maxTokens": 10},
        )
        body = server.body()
        assert body["model"] == "deepseek-chat"
        assert body["messages"] == [{"role": "user", "content": "hi"}]
        assert "temperature" not in body and "top_p" not in body

    def test_length_finish_maps_to_max_tokens(self, openai_env):
        resp = _runtime(_Server(_chat("partial", finish_reason="length"))).converse(
            modelId="m", messages=[{"role": "user", "content": [{"text": "hi"}]}]
        )
        assert resp["stopReason"] == "max_tokens"

    def test_empty_content_yields_no_text_block(self, openai_env):
        resp = _runtime(_Server(_chat("", finish_reason="content_filter"))).converse(
            modelId="m", messages=[{"role": "user", "content": [{"text": "hi"}]}]
        )
        assert resp["output"]["message"]["content"] == []
        assert resp["stopReason"] == "content_filtered"

    def test_guardrail_config_is_skipped_with_one_warning(self, openai_env):
        server = _Server(_chat(), _chat())
        client = _runtime(server)
        kwargs = {
            "modelId": "m",
            "messages": [{"role": "user", "content": [{"text": "ctx"}, {"guardContent": {"text": {"text": "q"}}}]}],
            "guardrailConfig": {"guardrailIdentifier": "gr-1", "guardrailVersion": "1", "trace": "enabled"},
        }
        with capture_logs() as logs:
            client.converse(**kwargs)
            client.converse(**kwargs)
        skipped = [e for e in logs if e["event"] == "guardrail_unsupported_skipped"]
        assert len(skipped) == 1
        assert skipped[0]["guardrail_id"] == "gr-1"
        assert skipped[0]["log_level"] == "warning"
        # The guarded segment still reaches the model, as on an unguarded Bedrock call.
        assert server.body()["messages"][-1]["content"] == "ctx\n\nq"
        assert "guardrailConfig" not in server.body()


# ── embeddings stand-in ──────────────────────────────────────────────────


class TestInvokeModelEmbeddings:
    def test_cohere_shaped_batch_forwards_dimensions(self, openai_env):
        server = _Server(_embeddings([0.1, 0.2], [0.3, 0.4]))
        body = json.dumps(
            {"texts": ["a", "b"], "input_type": "search_document", "output_dimension": 2, "embedding_types": ["float"]}
        )
        resp = _runtime(server).invoke_model(modelId="us.cohere.embed-v4:0", body=body)
        assert str(server.requests[0].url) == f"{BASE_URL}/embeddings"
        assert server.body() == {
            "model": "text-embedding-v4",
            "input": ["a", "b"],
            "encoding_format": "float",
            "dimensions": 2,
        }
        assert json.loads(resp["body"].read()) == {"embeddings": {"float": [[0.1, 0.2], [0.3, 0.4]]}}

    def test_titan_shaped_single(self, openai_env):
        server = _Server(_embeddings([0.5, 0.6, 0.7]))
        body = json.dumps({"inputText": "a", "dimensions": 3, "normalize": True})
        resp = _runtime(server).invoke_model(modelId="bge-m3", body=body)
        assert server.body()["input"] == ["a"]
        assert server.body()["dimensions"] == 3
        assert json.loads(resp["body"].read()) == {"embedding": [0.5, 0.6, 0.7], "inputTextTokenCount": 7}

    def test_dimension_mismatch_is_rejected(self, openai_env):
        server = _Server(_embeddings([0.1, 0.2, 0.3]))
        body = json.dumps({"texts": ["a"], "input_type": "search_query", "output_dimension": 1024})
        with pytest.raises(ValueError, match="3-dim vectors, 1024 requested"):
            _runtime(server).invoke_model(modelId="us.cohere.embed-v4:0", body=body)


# ── error mapping ────────────────────────────────────────────────────────


class TestErrorMapping:
    @pytest.mark.parametrize(
        ("status", "payload", "code"),
        [
            (429, {"error": {"message": "Rate limit reached"}}, "ThrottlingException"),
            (400, {"error": {"message": "bad request"}}, "ValidationException"),
            (401, {"error": {"message": "invalid api key"}}, "AccessDeniedException"),
            (404, {"error": {"message": "model not found"}}, "ResourceNotFoundException"),
            (504, {"error": {"message": "upstream timeout"}}, "ModelTimeoutException"),
            (503, {"error": {"message": "overloaded"}}, "ServiceUnavailableException"),
            (500, {"error": {"message": "oops"}}, "InternalServerException"),
        ],
    )
    def test_http_status_maps_to_bedrock_error_code(self, openai_env, status, payload, code):
        server = _Server(httpx.Response(status, json=payload))
        with pytest.raises(ClientError) as exc_info:
            _runtime(server, max_attempts=1).converse(
                modelId="m", messages=[{"role": "user", "content": [{"text": "x"}]}]
            )
        err = exc_info.value.response["Error"]
        assert err["Code"] == code
        assert payload["error"]["message"] in err["Message"]
        assert exc_info.value.response["ResponseMetadata"]["HTTPStatusCode"] == status

    def test_context_length_is_validation_exception_with_bedrock_wording(self, openai_env):
        msg = "This model's maximum context length is 65536 tokens. However, you requested 70000 tokens."
        server = _Server(httpx.Response(400, json={"error": {"message": msg, "code": "context_length_exceeded"}}))
        with pytest.raises(ClientError) as exc_info:
            _runtime(server).converse(modelId="m", messages=[{"role": "user", "content": [{"text": "x"}]}])
        err = exc_info.value.response["Error"]
        assert err["Code"] == "ValidationException"
        assert err["Message"].startswith("Input is too long for requested model.")
        assert len(server.requests) == 1  # a 400 is never retried

    def test_throttle_is_retried_then_succeeds(self, openai_env):
        server = _Server(httpx.Response(429, json={"error": {"message": "slow down"}}), _chat("ok"))
        resp = _runtime(server).converse(modelId="m", messages=[{"role": "user", "content": [{"text": "x"}]}])
        assert resp["output"]["message"]["content"] == [{"text": "ok"}]
        assert len(server.requests) == 2

    def test_throttle_exhausting_retries_raises_throttling(self, openai_env):
        server = _Server(*[httpx.Response(429, json={"error": {"message": "slow down"}})] * 3)
        with pytest.raises(ClientError) as exc_info:
            _runtime(server, max_attempts=3).converse(
                modelId="m", messages=[{"role": "user", "content": [{"text": "x"}]}]
            )
        assert exc_info.value.response["Error"]["Code"] == "ThrottlingException"
        assert len(server.requests) == 3

    def test_read_timeout_maps_to_botocore_and_is_not_resent(self, openai_env):
        server = _Server(_raise(httpx.ReadTimeout), _chat())
        with pytest.raises(ReadTimeoutError):
            _runtime(server).converse(modelId="m", messages=[{"role": "user", "content": [{"text": "x"}]}])
        assert len(server.requests) == 1

    def test_connect_error_retried_then_maps_to_endpoint_connection_error(self, openai_env):
        server = _Server(*[_raise(httpx.ConnectError)] * 2)
        with pytest.raises(EndpointConnectionError):
            _runtime(server, max_attempts=2).converse(
                modelId="m", messages=[{"role": "user", "content": [{"text": "x"}]}]
            )
        assert len(server.requests) == 2

    def test_retry_budget_and_timeouts_come_from_boto_config(self, openai_env):
        config = Config(read_timeout=42, connect_timeout=5, max_pool_connections=7, retries={"max_attempts": 4})
        client = OpenAICompatRuntimeClient.from_boto_config(config)
        assert client._max_attempts == 5  # botocore max_attempts counts retries
        assert client._http.timeout.read == 42
        assert client._http.timeout.connect == 5


# ── shared clients under the openai_compatible provider ──────────────────


class TestSharedClientsRouteThroughProvider:
    def test_bedrock_client_invoke_skips_guardrail_without_metrics(self, openai_env, inject_transport):
        from coa_common.bedrock import BedrockClient

        server = _Server(_chat('```json\n{"ok": true}\n```'))
        inject_transport(server)
        with (
            capture_logs() as logs,
            patch("coa_common.bedrock.emit_guardrail_decision") as emit,
            patch("coa_common.bedrock.boto3") as boto3,
        ):
            client = BedrockClient(region="cn-north-1", guardrail_id="gr-enrich")
            result = client.invoke("system prompt", "user prompt", max_tokens=100)
        boto3.client.assert_not_called()
        assert result.result == {"ok": True}
        assert (result.input_tokens, result.output_tokens) == (11, 3)
        assert server.body()["model"] == "deepseek-chat"
        emit.assert_not_called()  # an unscreened call must not be counted as ALLOW
        skipped = [e for e in logs if e["event"] == "guardrail_unsupported_skipped"]
        assert [(e["guardrail_id"], e["component"]) for e in skipped] == [("gr-enrich", "enrichment")]

    def test_bedrock_client_maps_throttle_to_client_error(self, openai_env, inject_transport):
        from coa_common.bedrock import BedrockClient

        inject_transport(_Server(*[httpx.Response(429, json={"error": {"message": "slow"}})] * 4))
        client = BedrockClient(region="cn-north-1")
        with pytest.raises(ClientError) as exc_info:
            client.invoke("s", "u")
        assert exc_info.value.response["Error"]["Code"] == "ThrottlingException"

    def test_embedder_batches_with_configured_dimensions(self, openai_env, inject_transport):
        from coa_common.embeddings import BedrockEmbedder

        server = _Server(_embeddings([1.0] * 4, [2.0] * 4), _embeddings([3.0] * 4))
        inject_transport(server)
        embedder = BedrockEmbedder(model_id="us.cohere.embed-v4:0", dimensions=4, region="cn-north-1")
        assert embedder.embed_documents(["a", "b"]) == [[1.0] * 4, [2.0] * 4]
        assert embedder.embed_query("q") == [3.0] * 4
        assert server.body(0) == {
            "model": "text-embedding-v4",
            "input": ["a", "b"],
            "encoding_format": "float",
            "dimensions": 4,
        }
        assert server.body(1)["input"] == ["q"]

    def test_guardrail_screener_passes_through_with_warning(self, openai_env):
        from coa_common.guardrail_screener import GuardrailScreener

        screener = GuardrailScreener(guardrail_id="gr-retrieval", region="cn-north-1", component="serve-retrieval")
        with (
            capture_logs() as logs,
            patch("coa_common.guardrail_screener.boto3") as boto3,
            patch("coa_common.guardrail_screener.emit_guardrail_decision") as emit,
        ):
            results = screener.screen_texts(["chunk one", "chunk two"])
        boto3.client.assert_not_called()
        emit.assert_not_called()
        assert [(r.text_index, r.passed, r.action) for r in results] == [(0, True, "NONE"), (1, True, "NONE")]
        assert any(e["event"] == "guardrail_unsupported_skipped" and e["guardrail_id"] == "gr-retrieval" for e in logs)
