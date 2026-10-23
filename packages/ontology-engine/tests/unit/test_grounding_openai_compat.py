# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Grounding rerank honours LLM_PROVIDER (the former direct bedrock-runtime client).

Under ``openai_compatible`` the rerank client is the OpenAI-compatible stand-in;
its responses and errors flow through the existing rerank contract unchanged
(parsed JSON on success, GroundingRerankError on an infrastructure failure).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import httpx
import pytest
from coa_common import openai_compat
from coa_common.openai_compat import OpenAICompatRuntimeClient
from coa_ontology.inducer.services import grounding
from coa_ontology.inducer.services.grounding import GroundingCandidate, GroundingRerankError, GroundingService

pytestmark = pytest.mark.unit


@pytest.fixture
def openai_env(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai_compatible")
    monkeypatch.setenv("OPENAI_COMPAT_BASE_URL", "https://llm.example.test/v1")
    monkeypatch.setenv("OPENAI_COMPAT_CHAT_MODEL", "deepseek-chat")
    monkeypatch.delenv("OPENAI_COMPAT_MODEL_MAP", raising=False)
    monkeypatch.delenv("OPENAI_COMPAT_API_KEY_SECRET_ARN", raising=False)
    monkeypatch.setattr(openai_compat, "retry_delay", lambda attempt: 0.0)
    openai_compat.load_settings.cache_clear()
    yield
    openai_compat.load_settings.cache_clear()


def _service_with(monkeypatch, handler) -> GroundingService:
    original = OpenAICompatRuntimeClient.from_boto_config.__func__
    monkeypatch.setattr(
        OpenAICompatRuntimeClient,
        "from_boto_config",
        classmethod(lambda cls, config, **kw: original(cls, config, transport=httpx.MockTransport(handler), **kw)),
    )
    return GroundingService(ontology_catalog=MagicMock(), embedding_generator=MagicMock(), llm_region="cn-north-1")


def _rerank(service: GroundingService) -> dict:
    candidate = GroundingCandidate(
        entity_uri="https://example.test/Customer", ontology_id=None, label="Customer", definition="", lexical_sim=0.9
    )
    return service._llm_rerank("customers", "customer master", [], [candidate])


def test_rerank_goes_through_openai_compatible_endpoint(openai_env, monkeypatch):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        answer = {"choice": "Customer", "confidence": 0.93, "relationship": "exact", "reason": "same concept"}
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": f"```json\n{json.dumps(answer)}\n```"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 40, "completion_tokens": 12},
            },
        )

    with patch.object(grounding.boto3, "client") as boto_client:
        service = _service_with(monkeypatch, handler)
        result = _rerank(service)
    boto_client.assert_not_called()
    assert isinstance(service.bedrock, OpenAICompatRuntimeClient)
    assert result["choice"] == "Customer"
    sent = json.loads(requests[0].content)
    assert sent["model"] == "deepseek-chat"
    assert sent["messages"][0]["role"] == "system"


def test_rerank_truncation_still_raises(openai_env, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"cho'}, "finish_reason": "length"}]})

    with pytest.raises(GroundingRerankError, match="truncated"):
        _rerank(_service_with(monkeypatch, handler))


def test_rerank_throttle_after_retries_is_infrastructure_failure(openai_env, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"message": "rate limited"}})

    with pytest.raises(GroundingRerankError, match="ThrottlingException"):
        _rerank(_service_with(monkeypatch, handler))
