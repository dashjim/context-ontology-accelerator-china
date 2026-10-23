# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""OpenAI-compatible chat + embeddings transport, for regions without Bedrock.

Selected by ``LLM_PROVIDER=openai_compatible`` (see :mod:`coa_common.llm_provider`).
Targets any server speaking the OpenAI ``/chat/completions`` and ``/embeddings``
wire format (DeepSeek, Qwen/DashScope compatible mode, self-hosted vLLM).

Configuration (environment):

* ``OPENAI_COMPAT_BASE_URL`` — required, e.g. ``https://api.deepseek.com/v1``.
  Requests go to ``{base}/chat/completions`` and ``{base}/embeddings``.
* ``OPENAI_COMPAT_API_KEY_SECRET_ARN`` — Secrets Manager secret holding the API
  key, either as the bare key or as JSON ``{"api_key": "..."}``. Fetched once per
  process. Unset means no ``Authorization`` header (an unauthenticated vLLM).
* ``OPENAI_COMPAT_MODEL_MAP`` — JSON object mapping the Bedrock model ids the
  code passes (``BEDROCK_MODEL_ID``, ``BEDROCK_CHAT_MODEL_ID``, per-call
  overrides, built-in defaults) to provider model names.
* ``OPENAI_COMPAT_CHAT_MODEL`` / ``OPENAI_COMPAT_EMBED_MODEL`` — the provider
  model used for any chat / embedding model id absent from the map. When unset
  the id passes through unchanged, so ``BEDROCK_MODEL_ID=deepseek-chat`` works.

Errors are raised as the botocore types callers already handle for Bedrock:
``ClientError`` carrying a Bedrock error code (``ThrottlingException``,
``ValidationException``, ``ModelTimeoutException``, ...) for HTTP errors, and
``ReadTimeoutError`` / ``ConnectTimeoutError`` / ``EndpointConnectionError`` for
transport failures.
"""

from __future__ import annotations

import functools
import io
import json
import os
import random
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import boto3
import httpx
import structlog
from botocore.config import Config
from botocore.exceptions import ClientError, ConnectTimeoutError, EndpointConnectionError, ReadTimeoutError

from coa_common.aws_config import sync_boto_config
from coa_common.llm_provider import skip_unsupported_guardrail

logger = structlog.get_logger(__name__)

# ``component`` label for a guardrailConfig reaching the bedrock-runtime stand-in.
COMPONENT_RUNTIME_SHIM = "openai-compat-runtime"

# OpenAI ``finish_reason`` → Bedrock Converse ``stopReason``, so callers that
# branch on the Converse value (truncation, model-side filtering) keep working.
_FINISH_TO_STOP_REASON = {
    "stop": "end_turn",
    "length": "max_tokens",
    "content_filter": "content_filtered",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
}

# Statuses worth retrying: throttling and transient server-side failures —
# the same classes botocore's standard retry mode retries for Bedrock.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
_BASE_BACKOFF_S = 0.5
_MAX_BACKOFF_S = 8.0

# Substrings that identify a context-window overflow in a provider's 400 body.
# There is no standard error code for it across OpenAI-compatible servers.
_CONTEXT_LENGTH_MARKERS = (
    "context_length_exceeded",
    "context length",
    "context window",
    "maximum context",
    "too many tokens",
    "input is too long",
    "prompt is too long",
)


@dataclass(frozen=True)
class OpenAICompatSettings:
    """Endpoint, credentials and model routing for the OpenAI-compatible backend."""

    base_url: str
    api_key: str | None = None
    chat_model: str | None = None
    embed_model: str | None = None
    model_map: Mapping[str, str] = field(default_factory=dict)

    def resolve_chat_model(self, model_id: str) -> str:
        """Map a Bedrock chat model id onto the provider's model name."""
        return self.model_map.get(model_id) or self.chat_model or model_id

    def resolve_embed_model(self, model_id: str) -> str:
        """Map a Bedrock embedding model id onto the provider's model name."""
        return self.model_map.get(model_id) or self.embed_model or model_id

    def headers(self) -> dict[str, str]:
        """Request headers, including bearer auth when an API key is configured."""
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}


def _fetch_api_key(secret_arn: str) -> str:
    """Read the API key from Secrets Manager (bare string or ``{"api_key": ...}``)."""
    # The secret's own region, from its ARN, so a cross-region secret resolves
    # and the aws-cn partition needs no special casing.
    region = secret_arn.split(":")[3]
    client = boto3.client("secretsmanager", region_name=region, config=sync_boto_config())
    value = client.get_secret_value(SecretId=secret_arn)["SecretString"]
    try:
        parsed = json.loads(value)
    except ValueError:
        return value.strip()
    if isinstance(parsed, dict):
        if not parsed.get("api_key"):
            raise ValueError(f"Secret {secret_arn} is a JSON object without an 'api_key' field")
        return str(parsed["api_key"])
    return value.strip()


@functools.cache
def load_settings() -> OpenAICompatSettings:
    """Load settings from the environment once per process (see module docstring).

    Raises:
        ValueError: If ``OPENAI_COMPAT_BASE_URL`` is unset or the model map is
            not a JSON object of strings.
    """
    base_url = os.environ.get("OPENAI_COMPAT_BASE_URL", "").strip().rstrip("/")
    if not base_url:
        raise ValueError("LLM_PROVIDER=openai_compatible requires OPENAI_COMPAT_BASE_URL")
    raw_map = os.environ.get("OPENAI_COMPAT_MODEL_MAP", "").strip()
    model_map = json.loads(raw_map) if raw_map else {}
    if not isinstance(model_map, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in model_map.items()
    ):
        raise ValueError("OPENAI_COMPAT_MODEL_MAP must be a JSON object of model-id strings")
    secret_arn = os.environ.get("OPENAI_COMPAT_API_KEY_SECRET_ARN", "").strip()
    settings = OpenAICompatSettings(
        base_url=base_url,
        api_key=_fetch_api_key(secret_arn) if secret_arn else None,
        chat_model=os.environ.get("OPENAI_COMPAT_CHAT_MODEL") or None,
        embed_model=os.environ.get("OPENAI_COMPAT_EMBED_MODEL") or None,
        model_map=model_map,
    )
    logger.info(
        "openai_compat_configured",
        base_url=base_url,
        chat_model=settings.chat_model,
        embed_model=settings.embed_model,
        mapped_models=sorted(model_map),
        api_key_configured=settings.api_key is not None,
    )
    return settings


def sdk_api_key(settings: OpenAICompatSettings) -> str:
    """API key for a client built on the ``openai`` SDK (Strands, LlamaIndex).

    The SDK refuses to construct a client without a key, while an endpoint that
    needs none (a vLLM started without ``--api-key``) ignores the bearer header,
    so a placeholder stands in when no key is configured.
    """
    return settings.api_key or "unused"


def make_llama_index_llm(model_id: str, **kwargs: Any) -> Any:
    """A LlamaIndex chat LLM on the OpenAI-compatible endpoint, for graphrag-toolkit.

    The toolkit builds ``BedrockConverse`` for any model given as a string, so the
    graphrag paths hand it this instance instead (``GraphRAGConfig.to_llm`` returns
    an ``LLM`` instance unchanged). ``model_id`` is mapped like every other chat
    model id; ``kwargs`` (``max_tokens``, ``temperature``, ``timeout``,
    ``max_retries``) pass through to ``OpenAILike``.

    Imported lazily: only the graphrag paths depend on ``llama_index``. Picklable
    for graphrag's ProcessPool fan-out — the SDK client is rebuilt on first use.
    """
    from llama_index.llms.openai_like import OpenAILike

    settings = load_settings()
    return OpenAILike(
        model=settings.resolve_chat_model(model_id),
        api_base=settings.base_url,
        api_key=sdk_api_key(settings),
        # Without this OpenAILike calls the legacy /completions endpoint.
        is_chat_model=True,
        **kwargs,
    )


# ── wire-format helpers (shared with the async serve client) ─────────────


def stop_reason_for(finish_reason: str | None) -> str:
    """Translate an OpenAI ``finish_reason`` into a Converse ``stopReason``."""
    return _FINISH_TO_STOP_REASON.get(finish_reason or "", finish_reason or "")


def chat_messages(system: str | None, user_parts: list[str]) -> list[dict[str, str]]:
    """Build chat messages: optional system message, then ONE user message.

    Parts are joined into a plain string rather than sent as content-part
    arrays, which several OpenAI-compatible servers (e.g. DeepSeek) reject.
    """
    messages = [{"role": "system", "content": system}] if system else []
    messages.append({"role": "user", "content": "\n\n".join(p for p in user_parts if p)})
    return messages


def chat_body(
    *,
    model: str,
    messages: list[dict[str, str]],
    max_tokens: int,
    temperature: float | None = None,
    top_p: float | None = None,
    stop: list[str] | None = None,
    stream: bool = False,
) -> dict[str, Any]:
    """Build a ``/chat/completions`` request body; unset sampling params are omitted."""
    body: dict[str, Any] = {"model": model, "messages": messages, "max_tokens": max_tokens}
    if temperature is not None:
        body["temperature"] = temperature
    if top_p is not None:
        body["top_p"] = top_p
    if stop:
        body["stop"] = stop
    if stream:
        body["stream"] = True
        # Without this, streamed responses carry no token usage at all.
        body["stream_options"] = {"include_usage": True}
    return body


def _error_message(body: bytes | str) -> str:
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
    try:
        parsed = json.loads(text)
    except ValueError:
        return text[:500]
    if isinstance(parsed, dict):
        err = parsed.get("error", parsed)
        if isinstance(err, dict):
            return str(err.get("message") or err.get("detail") or err.get("code") or err)[:500]
        return str(err)[:500]
    return text[:500]


def http_error(status: int, body: bytes | str, *, operation: str) -> ClientError:
    """Map a non-2xx response onto the ``ClientError`` Bedrock would have raised."""
    message = _error_message(body)
    if status == 429:
        code = "ThrottlingException"
    elif status in (408, 504):
        code = "ModelTimeoutException"
    elif status in (401, 403):
        code = "AccessDeniedException"
    elif status == 404:
        code = "ResourceNotFoundException"
    elif status == 503:
        code = "ServiceUnavailableException"
    elif status >= 500:
        code = "InternalServerException"
    else:
        code = "ValidationException"
        if any(marker in message.lower() for marker in _CONTEXT_LENGTH_MARKERS):
            # Bedrock's own wording for this case, so log searches and any caller
            # matching on it see the same text on both backends.
            message = f"Input is too long for requested model. {message}"
    error_response: Any = {"Error": {"Code": code, "Message": message}, "ResponseMetadata": {"HTTPStatusCode": status}}
    return ClientError(error_response, operation)


def transport_error(exc: httpx.TransportError, *, url: str) -> Exception:
    """Map an httpx transport failure onto the matching botocore exception."""
    if isinstance(exc, httpx.ConnectTimeout):
        return ConnectTimeoutError(endpoint_url=url, error=exc)
    if isinstance(exc, httpx.TimeoutException):
        return ReadTimeoutError(endpoint_url=url, error=exc)
    return EndpointConnectionError(endpoint_url=url, error=exc)


def retry_delay(attempt: int) -> float:
    """Capped exponential backoff with jitter for the given 0-based attempt."""
    return min(_MAX_BACKOFF_S, _BASE_BACKOFF_S * 2**attempt) + random.uniform(0, _BASE_BACKOFF_S)


def total_attempts(config: Config) -> int:
    """Total attempts implied by a botocore ``Config.retries`` dict.

    botocore's ``max_attempts`` counts retries (initial call excluded) while
    ``total_max_attempts`` includes it; honour whichever the site set.
    """
    retries = getattr(config, "retries", None) or {}
    if retries.get("total_max_attempts"):
        return max(1, int(retries["total_max_attempts"]))
    return max(1, int(retries.get("max_attempts", 2)) + 1)


# ── bedrock-runtime stand-in ─────────────────────────────────────────────


def _block_text(block: dict[str, Any]) -> str:
    if "text" in block:
        return block["text"]
    if "guardContent" in block:
        # No guardrail exists to evaluate the tagged segment; it still has to
        # reach the model, exactly as on an unguarded Bedrock call.
        return block["guardContent"]["text"]["text"]
    raise ValueError(f"Unsupported Converse content block for openai_compatible: {sorted(block)}")


class OpenAICompatRuntimeClient:
    """Stand-in for the bedrock-runtime client subset used in this codebase.

    Implements ``converse`` (text-only messages) and ``invoke_model`` for the
    Cohere and Titan embedding request bodies :class:`coa_common.BedrockEmbedder`
    sends, returning Bedrock-shaped responses. Thread-safe: one ``httpx.Client``
    is shared, as a boto3 client is.
    """

    # Callers written against boto3 catch ``client.exceptions.ClientError``.
    exceptions = SimpleNamespace(ClientError=ClientError)

    def __init__(
        self,
        settings: OpenAICompatSettings,
        *,
        read_timeout: float = 60,
        connect_timeout: float = 10,
        max_connections: int = 10,
        max_attempts: int = 3,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """Create the pooled HTTP client (``transport`` is a test seam)."""
        self._settings = settings
        self._max_attempts = max(1, max_attempts)
        self._http = httpx.Client(
            base_url=settings.base_url,
            headers=settings.headers(),
            timeout=httpx.Timeout(read_timeout, connect=connect_timeout),
            limits=httpx.Limits(max_connections=max_connections),
            transport=transport,
        )

    @classmethod
    def from_boto_config(cls, config: Config, **kwargs: Any) -> OpenAICompatRuntimeClient:
        """Build from the botocore ``Config`` the call site already constructs."""
        # botocore resolves unset options to these same defaults (60s / 60s / 10).
        return cls(
            load_settings(),
            read_timeout=getattr(config, "read_timeout", 60),
            connect_timeout=getattr(config, "connect_timeout", 60),
            max_connections=getattr(config, "max_pool_connections", 10),
            max_attempts=total_attempts(config),
            **kwargs,
        )

    def close(self) -> None:
        """Release the HTTP connection pool."""
        self._http.close()

    def _post(self, path: str, payload: dict[str, Any], *, operation: str) -> dict[str, Any]:
        url = f"{self._settings.base_url}{path}"
        for attempt in range(self._max_attempts):
            last = attempt == self._max_attempts - 1
            try:
                resp = self._http.post(path, json=payload)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                if last:
                    raise transport_error(exc, url=url) from exc
            except httpx.TransportError as exc:
                # A read timeout may have been billed server-side; do not resend.
                raise transport_error(exc, url=url) from exc
            else:
                if resp.status_code < 400:
                    return resp.json()
                if last or resp.status_code not in RETRYABLE_STATUS:
                    raise http_error(resp.status_code, resp.content, operation=operation)
            logger.warning("openai_compat_retry", operation=operation, attempt=attempt + 1)
            time.sleep(retry_delay(attempt))
        raise RuntimeError("openai_compat retry loop exhausted without returning or raising")  # pragma: no cover

    def converse(
        self,
        *,
        modelId: str,  # noqa: N803 - mirrors the boto3 keyword
        messages: list[dict[str, Any]],
        system: list[dict[str, Any]] | None = None,
        inferenceConfig: dict[str, Any] | None = None,  # noqa: N803 - mirrors the boto3 keyword
        guardrailConfig: dict[str, Any] | None = None,  # noqa: N803 - mirrors the boto3 keyword
    ) -> dict[str, Any]:
        """Run a Converse-shaped request as a chat completion; return a Converse response."""
        if guardrailConfig:
            skip_unsupported_guardrail(guardrailConfig.get("guardrailIdentifier"), component=COMPONENT_RUNTIME_SHIM)
        chat: list[dict[str, str]] = []
        system_text = "\n\n".join(b["text"] for b in system or [] if b.get("text"))
        if system_text:
            chat.append({"role": "system", "content": system_text})
        for message in messages:
            chat.append({"role": message["role"], "content": "\n\n".join(_block_text(b) for b in message["content"])})
        inference = inferenceConfig or {}
        body = chat_body(
            model=self._settings.resolve_chat_model(modelId),
            messages=chat,
            max_tokens=inference.get("maxTokens", 4096),
            temperature=inference.get("temperature"),
            top_p=inference.get("topP"),
            stop=inference.get("stopSequences"),
        )
        start = time.monotonic()
        data = self._post("/chat/completions", body, operation="Converse")
        choice = (data.get("choices") or [{}])[0]
        text = (choice.get("message") or {}).get("content") or ""
        usage = data.get("usage") or {}
        return {
            "output": {"message": {"role": "assistant", "content": [{"text": text}] if text else []}},
            "stopReason": stop_reason_for(choice.get("finish_reason")),
            "usage": {
                "inputTokens": usage.get("prompt_tokens", 0),
                "outputTokens": usage.get("completion_tokens", 0),
                "totalTokens": usage.get("total_tokens", 0),
            },
            "metrics": {"latencyMs": int((time.monotonic() - start) * 1000)},
        }

    def invoke_model(self, *, modelId: str, body: str | bytes) -> dict[str, Any]:  # noqa: N803 - boto3 keyword
        """Serve a Cohere- or Titan-shaped embedding request via ``/embeddings``.

        The requested output dimension is forwarded as ``dimensions`` so vectors
        match the index (e.g. 1024). A response of any other width is rejected:
        indexing or k-NN against a mismatched vector fails far from the cause.
        """
        request = json.loads(body)
        cohere_shaped = "texts" in request
        texts = request["texts"] if cohere_shaped else [request["inputText"]]
        dimensions = request.get("output_dimension") if cohere_shaped else request.get("dimensions")
        model = self._settings.resolve_embed_model(modelId)
        payload: dict[str, Any] = {"model": model, "input": texts, "encoding_format": "float"}
        if dimensions:
            payload["dimensions"] = dimensions
        data = self._post("/embeddings", payload, operation="InvokeModel")
        vectors = [d["embedding"] for d in sorted(data.get("data") or [], key=lambda d: d.get("index", 0))]
        if dimensions and any(len(v) != dimensions for v in vectors):
            raise ValueError(
                f"Embedding endpoint returned {len(vectors[0])}-dim vectors, {dimensions} requested (model={model})"
            )
        if cohere_shaped:
            out: dict[str, Any] = {"embeddings": {"float": vectors}}
        else:
            if not vectors:
                raise ValueError(f"No embedding in response (model={model})")
            out = {"embedding": vectors[0], "inputTextTokenCount": (data.get("usage") or {}).get("prompt_tokens")}
        return {"body": io.BytesIO(json.dumps(out).encode()), "contentType": "application/json"}
