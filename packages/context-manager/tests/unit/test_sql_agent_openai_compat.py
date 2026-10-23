# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""SqlAgent under ``LLM_PROVIDER=openai_compatible``: Strands OpenAIModel, not Bedrock.

The first test checks the provider wiring against the fake ``strands``. The second
runs the REAL Strands agent loop and OpenAIModel against a mocked
``/chat/completions`` stream, so it proves the tool-calling round trip (tool specs
sent, ``tool_calls`` executed, tool result fed back) works over this provider.
"""

from __future__ import annotations

import json
import sys

import httpx
import pytest
from coa_common import openai_compat

from . import strands_fake
from .strands_fake import install_fake_strands
from .test_sql_agent import GEN_SQL, _build, _generate_and_execute, _run

pytestmark = pytest.mark.unit

BASE_URL = "https://llm.example.test/v1"


@pytest.fixture
def openai_env(monkeypatch):
    """Select the OpenAI-compatible provider with a test endpoint, key and model map."""
    monkeypatch.setenv("LLM_PROVIDER", "openai_compatible")
    monkeypatch.setenv("OPENAI_COMPAT_BASE_URL", BASE_URL)
    monkeypatch.setenv("OPENAI_COMPAT_MODEL_MAP", json.dumps({"model-x": "deepseek-chat"}))
    monkeypatch.setenv("OPENAI_COMPAT_API_KEY_SECRET_ARN", "arn:aws-cn:secretsmanager:cn-north-1:1:secret:k")
    monkeypatch.setattr(openai_compat, "_fetch_api_key", lambda arn: "sk-test")
    monkeypatch.delenv("OPENAI_COMPAT_CHAT_MODEL", raising=False)
    openai_compat.load_settings.cache_clear()
    yield
    openai_compat.load_settings.cache_clear()


@pytest.mark.asyncio
async def test_openai_model_replaces_the_bedrock_provider(openai_env):
    agent, _ = _build(model_id="model-x", region="cn-north-1")

    install_fake_strands(lambda tools: _generate_and_execute(tools))
    outcome = await _run(agent)

    model = strands_fake.last_agent.model
    assert isinstance(model, strands_fake.FakeOpenAIModel)
    assert model.kwargs == {
        "client_args": {"base_url": BASE_URL, "api_key": "sk-test"},
        "model_id": "deepseek-chat",
        "params": {"max_tokens": 4096},
    }
    # Same tool set as on Bedrock, and the loop still verifies its SQL.
    assert set(strands_fake.last_agent.tools) == {"search_tables", "get_table_schema", "generate_sql", "execute_sql"}
    assert outcome.succeeded and outcome.executed_sql == GEN_SQL


def _sse(*chunks: dict) -> httpx.Response:
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode())


def _chunk(delta: dict, finish_reason: str | None = None) -> dict:
    return {
        "id": "c1",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "deepseek-chat",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


def _tool_call_turn(call_id: str, name: str, arguments: dict) -> httpx.Response:
    call = {
        "index": 0,
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }
    return _sse(_chunk({"role": "assistant", "tool_calls": [call]}), _chunk({}, "tool_calls"))


@pytest.mark.asyncio
async def test_real_strands_tool_calling_round_trip(openai_env, monkeypatch):
    # Drop the fake strands modules (other tests install them) so the real
    # package is imported; monkeypatch restores whatever was there afterwards.
    for name in [m for m in sys.modules if m == "strands" or m.startswith("strands.")]:
        monkeypatch.delitem(sys.modules, name)
    import openai

    turns = [
        _tool_call_turn("call_1", "search_tables", {"search": "orders"}),
        _tool_call_turn("call_2", "generate_sql", {"table_names": ["orders"]}),
        None,  # execute_sql — built from the handle generate_sql returned
        _sse(_chunk({"role": "assistant", "content": "There are 42 orders."}), _chunk({}, "stop")),
    ]
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        turn = turns.pop(0)
        if turn is None:
            # The tool result of generate_sql is the last message; echo its handle back.
            content = json.loads(request.content)["messages"][-1]["content"]
            generated = json.loads(content if isinstance(content, str) else content[0]["text"])
            turn = _tool_call_turn("call_3", "execute_sql", {"handle": generated["handle"]})
        return turn

    real_async_openai = openai.AsyncOpenAI
    client_kwargs: list[dict] = []

    def async_openai(**kwargs):
        client_kwargs.append(kwargs)
        return real_async_openai(**kwargs, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    monkeypatch.setattr(openai, "AsyncOpenAI", async_openai)

    agent, mocks = _build(model_id="model-x")
    outcome = await _run(agent)

    # All four turns were served: three tool calls executed, then the final answer.
    assert not turns and len(requests) == 4
    assert outcome.succeeded and outcome.executed_sql == GEN_SQL and outcome.row_count == 1
    mocks["vector"].search.assert_awaited()
    mocks["executor"].execute.assert_awaited_once()

    assert client_kwargs[0] == {"base_url": BASE_URL, "api_key": "sk-test"}
    assert str(requests[0].url) == f"{BASE_URL}/chat/completions"
    assert requests[0].headers["authorization"] == "Bearer sk-test"
    first = json.loads(requests[0].content)
    assert first["model"] == "deepseek-chat" and first["max_tokens"] == 4096 and first["stream"] is True
    assert {t["function"]["name"] for t in first["tools"]} == {
        "search_tables",
        "get_table_schema",
        "generate_sql",
        "execute_sql",
    }
    # The tool result travels back as an OpenAI ``tool`` message tied to the call id.
    second = json.loads(requests[1].content)["messages"]
    assert second[-1]["role"] == "tool" and second[-1]["tool_call_id"] == "call_1"
