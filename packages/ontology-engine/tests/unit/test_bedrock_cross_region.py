# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Induction LLM paths on native Bedrock in another region (AWS China → us-east-1).

BEDROCK_REGION and the model map reach the generation, rerank and RIGOR clients,
and a reasoning model's (gpt-oss) ``reasoningContent`` block is skipped.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from coa_ontology.inducer.services.grounding import GroundingService
from coa_ontology.inducer.services.llm import LLMClient
from coa_ontology.inducer.strategies.rigor_ontology import RigorOntologyStrategy

pytestmark = pytest.mark.unit

GPT_OSS = "openai.gpt-oss-120b-1:0"
SONNET = "us.anthropic.claude-sonnet-4-6"

REASONING_RESPONSE = {
    "output": {
        "message": {
            "content": [
                {"reasoningContent": {"reasoningText": {"text": "Let me think about the schema."}}},
                {"text": "@prefix ex: <http://example.org/> ."},
            ]
        }
    },
    "stopReason": "end_turn",
    "usage": {"inputTokens": 10, "outputTokens": 20},
}


@pytest.fixture
def cross_region(monkeypatch):
    for name in ("BEDROCK_API_KEY_SECRET_ARN", "AWS_BEARER_TOKEN_BEDROCK", "BEDROCK_MODEL_MAP", "LLM_PROVIDER"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BEDROCK_REGION", "us-east-1")
    monkeypatch.setenv("BEDROCK_MODEL_DEFAULT", GPT_OSS)


def test_generation_client(cross_region):
    llm = LLMClient(model_id=SONNET, region="cn-northwest-1")
    with patch("coa_ontology.inducer.services.llm.boto3.client") as factory:
        factory.return_value.converse.return_value = REASONING_RESPONSE
        text = llm.generate("p")

    assert factory.call_args.kwargs["region_name"] == "us-east-1"
    assert factory.return_value.converse.call_args.kwargs["modelId"] == GPT_OSS
    assert text == "@prefix ex: <http://example.org/> ."


def test_rerank_client(cross_region):
    service = GroundingService(MagicMock(), MagicMock(), llm_region="cn-northwest-1", llm_model_id=SONNET)
    with patch("coa_ontology.inducer.services.grounding.boto3.client") as factory:
        service.bedrock  # noqa: B018 - builds the lazy client

    assert factory.call_args.kwargs["region_name"] == "us-east-1"
    assert service._llm_model_id == GPT_OSS


def test_rigor_client_and_reasoning_response(cross_region):
    strategy = RigorOntologyStrategy()
    with patch("coa_ontology.inducer.strategies.rigor_ontology.boto3.client") as factory:
        client = strategy._get_llm_client({"llm_region": "cn-northwest-1"})
    assert factory.call_args.kwargs["region_name"] == "us-east-1"

    client.converse.return_value = REASONING_RESPONSE
    result = strategy._call_llm(client, SONNET, "prompt", "system")

    # Previously content[0]["text"] raised KeyError on the reasoning block and the
    # generation was silently dropped (None).
    assert result == "@prefix ex: <http://example.org/> ."
    assert client.converse.call_args.kwargs["modelId"] == GPT_OSS


def test_defaults_unchanged(monkeypatch):
    for name in ("BEDROCK_REGION", "BEDROCK_MODEL_MAP", "BEDROCK_MODEL_DEFAULT", "LLM_PROVIDER"):
        monkeypatch.delenv(name, raising=False)
    llm = LLMClient(model_id=SONNET, region="eu-west-1")
    with patch("coa_ontology.inducer.services.llm.boto3.client") as factory:
        llm.client  # noqa: B018 - builds the lazy client

    assert factory.call_args.kwargs["region_name"] == "eu-west-1"
    assert llm.model_id == SONNET
