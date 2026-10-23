# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for OpenAICompatLLMClient and LLM provider selection in the factory.

HTTP is mocked with ``httpx.MockTransport``: chat goes through the client's own
async transport; embeddings go through the shared BedrockEmbedder, whose
OpenAI-compatible stand-in gets a sync MockTransport injected.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest
from botocore.exceptions import ClientError, ReadTimeoutError
from coa_common import llm_provider, openai_compat
from coa_common.openai_compat import OpenAICompatRuntimeClient
from coa_serve.clients.base import ContentFilteredError, GuardrailOutcome, LLMClient
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


def _chat(text: str, finish_reason: str = "stop") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": finish_reason}
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
        },
    )


def _sse(*events: dict | str) -> httpx.Response:
    lines = []
    for event in events:
        lines.append(event if isinstance(event, str) else f"data: {json.dumps(event)}")
        lines.append("")
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content="\n".join(lines).encode())


def _delta(text: str | None = None, finish_reason: str | None = None) -> dict:
    delta = {"content": text} if text is not None else {}
    return {"choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]}


@pytest.fixture
def openai_env(monkeypatch):
    """Select the OpenAI-compatible provider with a test endpoint and model routing."""
    monkeypatch.setenv("LLM_PROVIDER", "openai_compatible")
    monkeypatch.setenv("OPENAI_COMPAT_BASE_URL", BASE_URL)
    monkeypatch.setenv("OPENAI_COMPAT_MODEL_MAP", json.dumps({"us.anthropic.claude-sonnet-5": "deepseek-chat"}))
    monkeypatch.setenv("OPENAI_COMPAT_EMBED_MODEL", "text-embedding-v4")
    monkeypatch.delenv("OPENAI_COMPAT_CHAT_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_COMPAT_API_KEY_SECRET_ARN", raising=False)
    monkeypatch.delenv("BEDROCK_MODEL_ID", raising=False)
    monkeypatch.setattr("coa_serve.clients.openai_compat.retry_delay", lambda attempt: 0.0)
    monkeypatch.setattr(llm_provider, "_WARNED_GUARDRAILS", set())
    openai_compat.load_settings.cache_clear()
    yield
    openai_compat.load_settings.cache_clear()


def _client(server: _Server):
    from coa_serve.clients.openai_compat import OpenAICompatLLMClient

    return OpenAICompatLLMClient(transport=httpx.MockTransport(server))


async def _collect(stream) -> list[str]:
    return [token async for token in stream]


class TestConverse:
    async def test_maps_request_and_returns_no_intervention(self, openai_env):
        server = _Server(_chat("SELECT 1"))
        result = await _client(server).converse(
            "schema context", system="write SQL", max_tokens=512, temperature=0, top_p=0
        )
        assert str(server.requests[0].url) == f"{BASE_URL}/chat/completions"
        assert server.body() == {
            "model": "deepseek-chat",
            "messages": [
                {"role": "system", "content": "write SQL"},
                {"role": "user", "content": "schema context"},
            ],
            "max_tokens": 512,
            "temperature": 0,
            "top_p": 0,
        }
        assert result.text == "SELECT 1"
        assert result.outcome is GuardrailOutcome.NONE
        assert result.guardrail_blocked is False
        assert result.stop_reason == "end_turn"
        assert not result.truncated

    async def test_per_call_model_override_passes_through_unmapped(self, openai_env):
        server = _Server(_chat("ok"))
        await _client(server).converse("p", model_id="qwen-plus")
        assert server.body()["model"] == "qwen-plus"

    async def test_length_finish_is_truncation(self, openai_env):
        with capture_logs() as logs:
            result = await _client(_Server(_chat("SELECT a FR", finish_reason="length"))).converse("p", max_tokens=8)
        assert result.stop_reason == "max_tokens"
        assert result.truncated
        assert any(e["event"] == "bedrock_output_truncated" and e["max_tokens"] == 8 for e in logs)

    async def test_filtered_without_text_raises_content_filtered(self, openai_env):
        with pytest.raises(ContentFilteredError):
            await _client(_Server(_chat("", finish_reason="content_filter"))).converse("p")

    async def test_no_text_raises_value_error(self, openai_env):
        with pytest.raises(ValueError, match="No text content"):
            await _client(_Server(_chat(""))).converse("p")

    async def test_guardrail_skipped_with_single_warning_and_guard_content_sent(self, openai_env):
        server = _Server(_chat("a1"), _chat("a2"))
        client = _client(server)
        with capture_logs() as logs:
            first = await client.converse("context", guardrail_id="gr-serve", guard_content="user question")
            await client.converse("context", guardrail_id="gr-serve", guard_content="user question")
        skipped = [e for e in logs if e["event"] == "guardrail_unsupported_skipped"]
        assert len(skipped) == 1
        assert skipped[0]["guardrail_id"] == "gr-serve"
        assert skipped[0]["log_level"] == "warning"
        assert first.outcome is GuardrailOutcome.NONE and first.guardrail_blocked is False
        assert server.body()["messages"] == [{"role": "user", "content": "context\n\nuser question"}]


class TestConverseStream:
    async def test_streams_chunks_with_finish_reason_and_usage(self, openai_env):
        server = _Server(
            _sse(
                ": keep-alive",
                _delta(""),
                _delta("Hel"),
                _delta("lo"),
                _delta(" world", finish_reason="stop"),
                {"choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 3, "total_tokens": 12}},
                "data: [DONE]",
            )
        )
        with capture_logs() as logs:
            tokens = await _collect(_client(server).converse_stream("p", system="s", max_tokens=64))
        assert tokens == ["Hel", "lo", " world"]
        body = server.body()
        assert body["stream"] is True
        assert body["stream_options"] == {"include_usage": True}
        assert body["max_tokens"] == 64
        finished = next(e for e in logs if e["event"] == "openai_compat_stream_finished")
        assert finished["stop_reason"] == "end_turn"
        assert (finished["input_tokens"], finished["output_tokens"]) == (9, 3)
        assert not any(e["event"] == "bedrock_output_truncated" for e in logs)

    async def test_length_finish_logs_truncation(self, openai_env):
        server = _Server(_sse(_delta("part", finish_reason="length"), "data: [DONE]"))
        with capture_logs() as logs:
            tokens = await _collect(_client(server).converse_stream("p", max_tokens=4))
        assert tokens == ["part"]
        assert any(e["event"] == "bedrock_output_truncated" and e.get("streaming") for e in logs)

    async def test_guardrail_never_raises_blocked(self, openai_env):
        server = _Server(_sse(_delta("ok", finish_reason="stop"), "data: [DONE]"))
        with capture_logs() as logs:
            tokens = await _collect(_client(server).converse_stream("p", guardrail_id="gr-1", guard_content="q"))
        assert tokens == ["ok"]
        assert server.body()["messages"][-1]["content"] == "p\n\nq"
        assert any(e["event"] == "guardrail_unsupported_skipped" for e in logs)

    async def test_in_band_error_event_raises_client_error(self, openai_env):
        server = _Server(_sse(_delta("a"), {"error": {"message": "engine overloaded", "code": 503}}))
        stream = _client(server).converse_stream("p")
        assert await stream.__anext__() == "a"
        with pytest.raises(ClientError) as exc_info:
            await stream.__anext__()
        assert exc_info.value.response["Error"]["Code"] == "ServiceUnavailableException"

    async def test_throttled_open_is_retried_before_first_token(self, openai_env):
        server = _Server(
            httpx.Response(429, json={"error": {"message": "rate limited"}}),
            _sse(_delta("ok", finish_reason="stop"), "data: [DONE]"),
        )
        assert await _collect(_client(server).converse_stream("p")) == ["ok"]
        assert len(server.requests) == 2

    async def test_context_length_on_open_raises_validation(self, openai_env):
        server = _Server(httpx.Response(400, json={"error": {"message": "maximum context length is 32768 tokens"}}))
        with pytest.raises(ClientError) as exc_info:
            await _collect(_client(server).converse_stream("p"))
        err = exc_info.value.response["Error"]
        assert err["Code"] == "ValidationException"
        assert err["Message"].startswith("Input is too long for requested model.")


class TestErrorMapping:
    async def test_throttle_exhausting_retries_raises_throttling(self, openai_env):
        server = _Server(*[httpx.Response(429, json={"error": {"message": "rate limited"}})] * 3)
        with pytest.raises(ClientError) as exc_info:
            await _client(server).converse("p")
        assert exc_info.value.response["Error"]["Code"] == "ThrottlingException"
        assert len(server.requests) == 3

    async def test_read_timeout_maps_to_botocore(self, openai_env):
        def timeout(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow", request=request)

        server = _Server(timeout)
        with pytest.raises(ReadTimeoutError):
            await _client(server).converse("p")
        assert len(server.requests) == 1


class TestEmbed:
    async def test_embed_uses_shared_embedder_with_index_dimensions(self, openai_env, monkeypatch):
        server = _Server(httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.5] * 1024}]}))
        original = OpenAICompatRuntimeClient.from_boto_config.__func__
        monkeypatch.setattr(
            OpenAICompatRuntimeClient,
            "from_boto_config",
            classmethod(lambda cls, config, **kw: original(cls, config, transport=httpx.MockTransport(server), **kw)),
        )
        client = _client(_Server())
        assert await client.embed("revenue by region") == [0.5] * 1024
        assert server.body() == {
            "model": "text-embedding-v4",
            "input": ["revenue by region"],
            "encoding_format": "float",
            "dimensions": 1024,
        }


class TestProviderSelection:
    def test_factory_defaults_to_bedrock(self, monkeypatch):
        from coa_serve.clients.bedrock import BedrockLLMClient
        from coa_serve.clients.factory import build_llm_client

        monkeypatch.delenv("LLM_PROVIDER", raising=False)
        client = build_llm_client(model_id="us.anthropic.claude-sonnet-5", region="us-east-1")
        assert isinstance(client, BedrockLLMClient)
        assert isinstance(client, LLMClient)

    def test_factory_builds_openai_compatible_client(self, openai_env):
        from coa_serve.clients.factory import build_llm_client
        from coa_serve.clients.openai_compat import OpenAICompatLLMClient

        client = build_llm_client(model_id="us.anthropic.claude-sonnet-5", region="cn-north-1")
        assert isinstance(client, OpenAICompatLLMClient)
        assert isinstance(client, LLMClient)
        assert client.model_id == "us.anthropic.claude-sonnet-5"
