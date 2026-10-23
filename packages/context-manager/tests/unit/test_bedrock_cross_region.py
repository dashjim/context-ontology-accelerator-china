# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Serve on native Bedrock in another region (AWS China → commercial us-east-1).

Covers BEDROCK_REGION, the Bedrock API key (bearer auth) and the model map on
every serve Bedrock path — the Converse/ConverseStream client, the Strands SQL
agent and graphrag's response LLM — plus reasoning-model (gpt-oss) responses,
whose ``reasoningContent`` must never surface as answer text.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest
from coa_common.bedrock_access import BEARER_TOKEN_ENV

from . import strands_fake
from .strands_fake import install_fake_strands
from .test_sql_agent import _build, _generate_and_execute, _run

pytestmark = pytest.mark.unit

GPT_OSS = "openai.gpt-oss-120b-1:0"
SONNET = "us.anthropic.claude-sonnet-5"


@pytest.fixture
def cross_region(monkeypatch):
    """AWS China serve runtime pointed at us-east-1 Bedrock with Anthropic ids remapped."""
    for name in ("BEDROCK_API_KEY_SECRET_ARN", BEARER_TOKEN_ENV, "BEDROCK_MODEL_MAP", "LLM_PROVIDER"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_REGION", "cn-northwest-1")
    monkeypatch.setenv("BEDROCK_REGION", "us-east-1")
    monkeypatch.setenv("BEDROCK_MODEL_DEFAULT", GPT_OSS)


def _llm_client(**kwargs):
    from coa_serve.clients.bedrock import BedrockLLMClient

    return BedrockLLMClient(model_id=SONNET, region="cn-northwest-1", **kwargs)


class TestLLMClient:
    def test_client_is_built_in_bedrock_region(self, cross_region):
        with patch("coa_serve.clients.bedrock.boto3.client") as factory:
            _llm_client()._get_client()
        assert factory.call_args.args == ("bedrock-runtime",)
        assert factory.call_args.kwargs["region_name"] == "us-east-1"

    async def test_default_and_override_model_ids_are_mapped(self, cross_region):
        client = _llm_client()
        client._client = MagicMock()
        client._client.converse.return_value = {"output": {"message": {"content": [{"text": "ok"}]}}}

        await client.converse("q")
        await client.converse("q", model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0")

        sent = [c.kwargs["modelId"] for c in client._client.converse.call_args_list]
        assert sent == [GPT_OSS, GPT_OSS]

    async def test_unmapped_without_env(self, monkeypatch):
        monkeypatch.delenv("BEDROCK_MODEL_MAP", raising=False)
        monkeypatch.delenv("BEDROCK_MODEL_DEFAULT", raising=False)
        client = _llm_client()
        client._client = MagicMock()
        client._client.converse.return_value = {"output": {"message": {"content": [{"text": "ok"}]}}}

        await client.converse("q")

        assert client._client.converse.call_args.kwargs["modelId"] == SONNET

    async def test_converse_returns_only_the_text_after_reasoning(self, cross_region):
        client = _llm_client()
        client._client = MagicMock()
        client._client.converse.return_value = {
            "output": {
                "message": {
                    "content": [
                        {"reasoningContent": {"reasoningText": {"text": "The user wants SQL. Let me think."}}},
                        {"text": "SELECT 1"},
                    ]
                }
            },
            "stopReason": "end_turn",
        }

        result = await client.converse("q")

        assert result.text == "SELECT 1"

    async def test_stream_yields_text_deltas_only(self, cross_region):
        events = [
            {"messageStart": {"role": "assistant"}},
            {"contentBlockDelta": {"delta": {"reasoningContent": {"text": "thinking..."}}, "contentBlockIndex": 0}},
            {"contentBlockStop": {"contentBlockIndex": 0}},
            {"contentBlockDelta": {"delta": {"text": "Forty"}, "contentBlockIndex": 1}},
            {"contentBlockDelta": {"delta": {"text": "-two"}, "contentBlockIndex": 1}},
            {"messageStop": {"stopReason": "end_turn"}},
            {"metadata": {"usage": {"inputTokens": 5, "outputTokens": 9}}},
        ]
        client = _llm_client()
        client._client = MagicMock()
        client._client.converse_stream.return_value = {"stream": iter(events)}

        tokens = [t async for t in client.converse_stream("q")]

        assert tokens == ["Forty", "-two"]
        assert client._client.converse_stream.call_args.kwargs["modelId"] == GPT_OSS


class TestSqlAgent:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(("model_id", "expected"), [(SONNET, GPT_OSS), (None, GPT_OSS)])
    async def test_strands_model_uses_bedrock_region_and_mapped_id(self, cross_region, model_id, expected):
        # None: Strands picks its own (Anthropic) default, which must be mapped too.
        agent, _ = _build(model_id=model_id, region="cn-northwest-1")

        install_fake_strands(lambda tools: _generate_and_execute(tools))
        outcome = await _run(agent)

        model = strands_fake.last_agent.model
        assert model.kwargs["region_name"] == "us-east-1"
        assert model.config["model_id"] == expected
        assert outcome.succeeded

    def test_real_strands_client_sends_the_api_key_as_bearer(self, cross_region, monkeypatch):
        """Strands builds its own boto3 session; the API key must still beat role credentials."""
        for name in [m for m in sys.modules if m == "strands" or m.startswith("strands.")]:
            monkeypatch.delitem(sys.modules, name)
        from coa_common.bedrock_access import map_bedrock_model_id, prepare_bedrock_client
        from strands.models import BedrockModel

        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAFAKEFAKEFAKEFAKE")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "fake-secret")
        monkeypatch.setenv(BEARER_TOKEN_ENV, "test-bedrock-api-key")
        model = BedrockModel(
            region_name=prepare_bedrock_client("cn-northwest-1"), model_id=map_bedrock_model_id(SONNET)
        )
        sent = {}

        def _before_send(request, **_kwargs):
            sent["auth"] = request.headers["Authorization"]
            sent["url"] = request.url
            raise RuntimeError("stop")

        model.client.meta.events.register("before-send.bedrock-runtime", _before_send)
        with pytest.raises(RuntimeError, match="stop"):
            model.client.converse(
                modelId=model.config["model_id"], messages=[{"role": "user", "content": [{"text": "x"}]}]
            )

        assert sent["auth"] in ("Bearer test-bedrock-api-key", b"Bearer test-bedrock-api-key")
        assert sent["url"].startswith("https://bedrock-runtime.us-east-1.amazonaws.com/model/openai.gpt-oss-120b-1")


class TestLexicalResponseLLM:
    @pytest.fixture
    def graphrag_config(self, monkeypatch):
        from graphrag_toolkit.lexical_graph import GraphRAGConfig

        monkeypatch.delenv("RESPONSE_MODEL", raising=False)
        monkeypatch.setattr(GraphRAGConfig, "_response_llm", None)
        return GraphRAGConfig

    def _retriever(self):
        from coa_serve.lexical.baseline_retriever import BaselineLexicalRetriever

        return BaselineLexicalRetriever(
            graph_store_uri="neptune-graph://g-abc", vector_store_uri="aoss://https://xyz.aoss.amazonaws.com"
        )

    def test_response_llm_is_a_cross_region_instance(self, cross_region, graphrag_config):
        from llama_index.llms.bedrock_converse import BedrockConverse

        self._retriever()

        llm = graphrag_config._response_llm
        assert isinstance(llm, BedrockConverse)
        assert llm.model == GPT_OSS and llm.region_name == "us-east-1"
        assert llm.temperature == 0.0 and llm.max_tokens == 4096
        # The toolkit's own region still signs OpenSearch/Neptune in the deployment region.
        assert graphrag_config.aws_region == "cn-northwest-1"

    def test_default_leaves_the_toolkit_string_path(self, monkeypatch, graphrag_config):
        for name in ("BEDROCK_REGION", "BEDROCK_MODEL_MAP", "BEDROCK_MODEL_DEFAULT", "LLM_PROVIDER"):
            monkeypatch.delenv(name, raising=False)

        self._retriever()

        assert graphrag_config._response_llm is None
