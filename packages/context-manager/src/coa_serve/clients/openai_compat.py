# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""OpenAI-compatible client — LLMClient implementation for regions without Bedrock.

Selected by ``LLM_PROVIDER=openai_compatible`` (see ``factory.build_llm_client``).
Chat calls go to ``{OPENAI_COMPAT_BASE_URL}/chat/completions`` over a native async
httpx client; ``converse_stream`` consumes the SSE stream directly, so closing the
generator closes the HTTP response and stops generation. Endpoint, API key and
model mapping are described in :mod:`coa_common.openai_compat`.

Embeddings go through the shared ``BedrockEmbedder`` exactly as on Bedrock. Under
this provider its transport is the OpenAI-compatible stand-in, so query vectors
come from the same model, at the same dimensions, as the ingested documents.

Bedrock Guardrails have no equivalent here. A requested ``guardrail_id`` is
skipped with a ``guardrail_unsupported_skipped`` warning, ``guard_content`` is
sent to the model as ordinary text (as on an unguarded Bedrock call), results
report ``GuardrailOutcome.NONE``, and no guardrail decision metric is emitted.

Errors surface as the botocore types the Bedrock client raises (``ClientError``
with a Bedrock error code, ``ReadTimeoutError``, ...). Only health_check() is
exception-safe and returns a status dict.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
import structlog
from coa_common import BedrockEmbedder, resolve_region
from coa_common.constants import DEFAULT_EMBED_MODEL_ID
from coa_common.guardrail_metrics import COMPONENT_NL_TO_SPARQL
from coa_common.llm_provider import skip_unsupported_guardrail
from coa_common.openai_compat import (
    RETRYABLE_STATUS,
    chat_body,
    chat_messages,
    http_error,
    load_settings,
    retry_delay,
    stop_reason_for,
    transport_error,
)

from .base import (
    STOP_REASON_MAX_TOKENS,
    ContentFilteredError,
    ConverseResult,
    GuardrailOutcome,
    instrumented,
)

logger = structlog.get_logger(__name__)

_EXECUTOR = ThreadPoolExecutor(max_workers=10, thread_name_prefix="openai-compat")

# Total attempts per chat request — parity with the Bedrock client's botocore
# retries={"max_attempts": 2}. A stream is retried only before its first byte.
_MAX_ATTEMPTS = 3

_CHAT_PATH = "/chat/completions"


def _stream_error(payload: dict[str, Any]) -> Exception:
    """Map an in-band SSE ``{"error": ...}`` event (sent after HTTP 200) to a ClientError."""
    err = payload.get("error")
    code = err.get("code") if isinstance(err, dict) else None
    status = code if isinstance(code, int) and 400 <= code < 600 else 500
    return http_error(status, json.dumps(payload), operation="ConverseStream")


class OpenAICompatLLMClient:
    """OpenAI-compatible chat completions + shared-embedder client."""

    def __init__(
        self,
        model_id: str | None = None,
        embed_model_id: str | None = None,
        region: str | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        """Configure the default model, embedder and HTTP settings.

        Args:
            model_id: Default chat model id; a Bedrock id is mapped via
                ``OPENAI_COMPAT_MODEL_MAP`` / ``OPENAI_COMPAT_CHAT_MODEL``. Defaults
                to ``BEDROCK_MODEL_ID``, as on the Bedrock client.
            embed_model_id: Embedding model id for the shared embedder. Defaults to
                ``BEDROCK_EMBED_MODEL_ID`` then ``DEFAULT_EMBED_MODEL_ID``.
            region: AWS region for the embedder. Defaults to ``BEDROCK_REGION`` then
                the resolved region.
            transport: Optional httpx transport (test seam).
        """
        self._settings = load_settings()
        self._model_id = model_id or os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-5")
        self._embed_model_id = embed_model_id or os.environ.get("BEDROCK_EMBED_MODEL_ID", DEFAULT_EMBED_MODEL_ID)
        self._region = region or os.environ.get("BEDROCK_REGION", resolve_region())
        self._embedder = BedrockEmbedder(model_id=self._embed_model_id, region=self._region)
        # Same knobs as the Bedrock client. The read timeout bounds every socket
        # read, so it is also the idle limit between two streamed chunks.
        self._timeout = httpx.Timeout(
            float(os.environ.get("BEDROCK_READ_TIMEOUT", "120")),
            connect=float(os.environ.get("BEDROCK_CONNECT_TIMEOUT", "10")),
        )
        self._transport = transport
        self._http: httpx.AsyncClient | None = None
        logger.info(
            "openai_compat_client_configured",
            model_id=self._model_id,
            provider_model=self._settings.resolve_chat_model(self._model_id),
            embed_model_id=self._embed_model_id,
            base_url=self._settings.base_url,
        )

    @property
    def model_id(self) -> str:
        """Return the default chat model id for this client."""
        return self._model_id

    def _get_http(self) -> httpx.AsyncClient:
        # Created on first use, inside the serving event loop, rather than at
        # startup (build_clients runs outside the loop).
        if self._http is None:
            self._http = httpx.AsyncClient(
                base_url=self._settings.base_url,
                headers=self._settings.headers(),
                timeout=self._timeout,
                limits=httpx.Limits(max_connections=10),
                transport=self._transport,
            )
        return self._http

    async def close(self) -> None:
        """Close the HTTP client and release its connection pool."""
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    def _build_body(
        self,
        prompt: str,
        *,
        system: str | None,
        guardrail_id: str | None,
        max_tokens: int,
        temperature: float | None = None,
        top_p: float | None = None,
        guard_content: str | None,
        model_id: str | None,
        stream: bool = False,
    ) -> dict[str, Any]:
        """Build the chat-completions body shared by converse() and converse_stream()."""
        if guardrail_id:
            # The single guardrail decision point for this backend: today it skips
            # (and warns once per guardrail id); a domestic content-safety check
            # would replace this call.
            skip_unsupported_guardrail(guardrail_id, component=COMPONENT_NL_TO_SPARQL)
        effective_model_id = model_id or self._model_id
        if model_id and model_id != self._model_id:
            logger.info("using_model_override", override=model_id, default=self._model_id)
        # guard_content is the SOLE channel for the untrusted user text on several
        # callers, so it must reach the model; with no guardrail to tag it for it
        # is ordinary text, as on an unguarded Bedrock call.
        return chat_body(
            model=self._settings.resolve_chat_model(effective_model_id),
            messages=chat_messages(system, [prompt, guard_content or ""]),
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            stream=stream,
        )

    async def _send(self, body: dict[str, Any], *, stream: bool, operation: str) -> httpx.Response:
        """POST with bounded retries on throttling / 5xx / connect failures.

        Returns a 2xx response (unread when ``stream``); the caller closes it.
        """
        http = self._get_http()
        url = f"{self._settings.base_url}{_CHAT_PATH}"
        for attempt in range(_MAX_ATTEMPTS):
            last = attempt == _MAX_ATTEMPTS - 1
            try:
                resp = await http.send(http.build_request("POST", _CHAT_PATH, json=body), stream=stream)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                if last:
                    raise transport_error(exc, url=url) from exc
            except httpx.TransportError as exc:
                # A read timeout may already be billed server-side; do not resend.
                raise transport_error(exc, url=url) from exc
            else:
                if resp.status_code < 400:
                    return resp
                content = await resp.aread()
                await resp.aclose()
                if last or resp.status_code not in RETRYABLE_STATUS:
                    raise http_error(resp.status_code, content, operation=operation)
            logger.warning("openai_compat_retry", operation=operation, attempt=attempt + 1)
            await asyncio.sleep(retry_delay(attempt))
        raise RuntimeError("openai_compat retry loop exhausted without returning or raising")  # pragma: no cover

    @instrumented("openai_compat")
    async def converse(
        self,
        prompt: str,
        *,
        system: str | None = None,
        guardrail_id: str | None = None,
        max_tokens: int = 4096,
        temperature: float | None = None,
        top_p: float | None = None,
        guard_content: str | None = None,
        model_id: str | None = None,
    ) -> ConverseResult:
        """Send a single-turn prompt as one chat completion.

        Args mirror ``BedrockLLMClient.converse``. ``guardrail_id`` is skipped with
        a warning (see module docstring).

        Returns:
            A ConverseResult with ``outcome=NONE`` and the Converse-equivalent
            ``stop_reason`` (``finish_reason=length`` → ``max_tokens``).

        Raises:
            ContentFilteredError: If the provider filtered the output and no text remains.
            ValueError: If the response contains no text content.
        """
        body = self._build_body(
            prompt,
            system=system,
            guardrail_id=guardrail_id,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            guard_content=guard_content,
            model_id=model_id,
        )
        resp = await self._send(body, stream=False, operation="Converse")
        data = resp.json()
        choice = (data.get("choices") or [{}])[0]
        stop_reason = stop_reason_for(choice.get("finish_reason"))
        text = (choice.get("message") or {}).get("content") or ""
        if not text:
            if stop_reason == "content_filtered":
                raise ContentFilteredError(
                    f"Model filtered its own output: stopReason={stop_reason}",
                    stop_reason=stop_reason,
                )
            raise ValueError(f"No text content in OpenAI-compatible response: stopReason={stop_reason}")
        if stop_reason == STOP_REASON_MAX_TOKENS:
            # Same event as the Bedrock client so existing log queries still match.
            logger.warning(
                "bedrock_output_truncated",
                model=body["model"],
                max_tokens=max_tokens,
                text_chars=len(text),
                provider="openai_compatible",
            )
        return ConverseResult(text=text, outcome=GuardrailOutcome.NONE, stop_reason=stop_reason)

    async def converse_stream(
        self,
        prompt: str,
        *,
        system: str | None = None,
        guardrail_id: str | None = None,
        max_tokens: int = 4096,
        guard_content: str | None = None,
        model_id: str | None = None,
    ) -> AsyncIterator[str]:
        """Stream text fragments from a ``stream=true`` chat completion (SSE).

        Never raises GuardrailBlockedError: no guardrail is applied on this backend.
        Usage arrives in the final chunk (``stream_options.include_usage``) and is
        logged with the stop reason once the stream ends.
        """
        body = self._build_body(
            prompt,
            system=system,
            guardrail_id=guardrail_id,
            max_tokens=max_tokens,
            guard_content=guard_content,
            model_id=model_id,
            stream=True,
        )
        resp = await self._send(body, stream=True, operation="ConverseStream")
        finish_reason: str | None = None
        usage: dict[str, Any] = {}
        try:
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue  # blank separators, SSE comments / keep-alives
                data = line[len("data:") :].strip()
                if data == "[DONE]":
                    break
                chunk = json.loads(data)
                if "error" in chunk:
                    raise _stream_error(chunk)
                for choice in chunk.get("choices") or []:
                    text = (choice.get("delta") or {}).get("content")
                    if text:
                        yield text
                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]
                if chunk.get("usage"):
                    usage = chunk["usage"]
        except httpx.TransportError as exc:
            raise transport_error(exc, url=f"{self._settings.base_url}{_CHAT_PATH}") from exc
        finally:
            # Closing the response is what stops generation when the consumer
            # disconnects mid-stream (the generator is closed at the yield).
            await resp.aclose()

        stop_reason = stop_reason_for(finish_reason)
        logger.info(
            "openai_compat_stream_finished",
            model=body["model"],
            stop_reason=stop_reason,
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
        )
        if stop_reason == STOP_REASON_MAX_TOKENS:
            logger.warning(
                "bedrock_output_truncated",
                model=body["model"],
                max_tokens=max_tokens,
                streaming=True,
                provider="openai_compatible",
            )

    @instrumented("openai_compat")
    async def embed(self, text: str) -> list[float]:
        """Embed query text via the shared search-query embedder.

        Args:
            text: The query text to embed.

        Returns:
            The embedding vector, at the embedder's configured dimensions.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_EXECUTOR, self._embedder.embed_query, text)

    async def health_check(self) -> dict[str, Any]:
        """Probe endpoint reachability via a test embedding call.

        Returns:
            A status dict: ``{"status": "ok", ...}`` on success, otherwise
            ``{"status": "error", ...}``. This method never raises.
        """
        try:
            await self.embed("health")
            return {"status": "ok", "model": self._model_id}
        except Exception:
            return {"status": "error", "detail": "Health check failed"}
