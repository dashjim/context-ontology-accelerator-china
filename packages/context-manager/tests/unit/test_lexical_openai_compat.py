# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""BaselineLexicalRetriever under ``LLM_PROVIDER=openai_compatible``.

graphrag-toolkit builds ``BedrockConverse`` for its response LLM unless handed an
instance, so on this provider the retriever must install an OpenAI-compatible one.
The toolkit's own ``LLMCache`` (what every retrieval-side consumer wraps the LLM
in) is driven against a mocked ``/chat/completions`` to prove the call goes there.
"""

from __future__ import annotations

import json

import httpx
import pytest
from coa_common import openai_compat
from graphrag_toolkit.lexical_graph import GraphRAGConfig

pytestmark = pytest.mark.unit

BASE_URL = "https://llm.example.test/v1"


@pytest.fixture
def openai_env(monkeypatch):
    """Select the OpenAI-compatible provider; restore the toolkit's response LLM after."""
    monkeypatch.setenv("LLM_PROVIDER", "openai_compatible")
    monkeypatch.setenv("OPENAI_COMPAT_BASE_URL", BASE_URL)
    monkeypatch.setenv("OPENAI_COMPAT_MODEL_MAP", json.dumps({"us.anthropic.claude-sonnet-5": "deepseek-chat"}))
    monkeypatch.delenv("OPENAI_COMPAT_CHAT_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_COMPAT_API_KEY_SECRET_ARN", raising=False)
    monkeypatch.delenv("RESPONSE_MODEL", raising=False)
    monkeypatch.setattr(GraphRAGConfig, "_response_llm", None)
    openai_compat.load_settings.cache_clear()
    yield
    openai_compat.load_settings.cache_clear()


def _retriever():
    from coa_serve.lexical.baseline_retriever import BaselineLexicalRetriever

    return BaselineLexicalRetriever(
        graph_store_uri="neptune-graph://g-abc", vector_store_uri="aoss://https://xyz.aoss.amazonaws.com"
    )


def test_response_llm_is_openai_like(openai_env):
    from llama_index.llms.bedrock_converse import BedrockConverse
    from llama_index.llms.openai_like import OpenAILike

    _retriever()

    llm = GraphRAGConfig.response_llm
    assert isinstance(llm, OpenAILike) and not isinstance(llm, BedrockConverse)
    assert llm.model == "deepseek-chat"  # GRAPHRAG_LLM_MODEL default, mapped
    assert llm.api_base == BASE_URL and llm.is_chat_model
    assert llm.temperature == 0.0 and llm.max_tokens == 4096
    # No key configured: the SDK still needs one; the endpoint ignores it.
    assert llm.api_key == "unused"


def test_response_model_env_is_honoured(openai_env, monkeypatch):
    monkeypatch.setenv("RESPONSE_MODEL", "qwen-plus")

    _retriever()

    assert GraphRAGConfig.response_llm.model == "qwen-plus"  # unmapped id passes through


def test_toolkit_llm_cache_calls_the_endpoint(openai_env, monkeypatch):
    import llama_index.llms.openai.base as openai_base
    from graphrag_toolkit.lexical_graph.utils.llm_cache import LLMCache
    from llama_index.core.prompts import PromptTemplate

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": "c1",
                "object": "chat.completion",
                "created": 0,
                "model": "deepseek-chat",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": "kw1^kw2"}, "finish_reason": "stop"}
                ],
            },
        )

    real_sync_openai = openai_base.SyncOpenAI
    monkeypatch.setattr(
        openai_base,
        "SyncOpenAI",
        lambda **kw: real_sync_openai(**{**kw, "http_client": httpx.Client(transport=httpx.MockTransport(handler))}),
    )

    _retriever()
    answer = LLMCache(llm=GraphRAGConfig.response_llm).predict(PromptTemplate("keywords for {q}"), q="revenue")

    assert answer == "kw1^kw2"
    assert len(requests) == 1 and str(requests[0].url) == f"{BASE_URL}/chat/completions"
    body = json.loads(requests[0].content)
    assert body["model"] == "deepseek-chat" and body["max_tokens"] == 4096
    assert body["messages"] == [{"role": "user", "content": "keywords for revenue"}]


def test_bedrock_default_leaves_the_toolkit_llm_alone(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    sentinel = object()
    monkeypatch.setattr(GraphRAGConfig, "_response_llm", sentinel)

    _retriever()

    assert GraphRAGConfig._response_llm is sentinel
