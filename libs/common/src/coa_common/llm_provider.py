# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""LLM backend selection: Amazon Bedrock (default) or an OpenAI-compatible endpoint.

Bedrock is not offered in the AWS China regions, so a deployment there points
every chat and embedding call at an OpenAI-compatible endpoint instead (DeepSeek,
Qwen/DashScope, a self-hosted vLLM). ``LLM_PROVIDER`` selects the backend for the
whole process; unset means ``bedrock``, so existing deployments are unchanged.

Every non-test site that builds ``boto3.client("bedrock-runtime")`` builds
:func:`openai_compat_runtime_client` instead when this backend is selected — a
stand-in for the subset of the bedrock-runtime surface those sites use
(``converse``, ``invoke_model`` for embeddings). The stand-in raises the same
botocore exception types Bedrock does, so existing error handling keeps working
unchanged.

This module deliberately does not import ``httpx``: the OpenAI-compatible
transport is imported lazily, only when that provider is selected.
"""

from __future__ import annotations

import os
from typing import Any

import structlog
from botocore.config import Config

logger = structlog.get_logger(__name__)

PROVIDER_BEDROCK = "bedrock"
PROVIDER_OPENAI_COMPATIBLE = "openai_compatible"
_PROVIDERS = frozenset({PROVIDER_BEDROCK, PROVIDER_OPENAI_COMPATIBLE})

# (component, guardrail_id) pairs already warned about. Process-wide so a
# per-request caller logs once rather than on every call; a lost race under the
# GIL costs one duplicate warning line, nothing more.
_WARNED_GUARDRAILS: set[tuple[str, str]] = set()


def llm_provider() -> str:
    """Return the configured LLM backend (``LLM_PROVIDER``, default ``bedrock``).

    Raises:
        ValueError: On an unrecognized value. A typo must fail loudly at client
            construction rather than silently fall back to Bedrock, which does
            not exist in the regions this switch is for.
    """
    raw = (os.environ.get("LLM_PROVIDER") or PROVIDER_BEDROCK).strip().lower()
    if raw not in _PROVIDERS:
        raise ValueError(f"LLM_PROVIDER={raw!r} is not one of {sorted(_PROVIDERS)}")
    return raw


def is_openai_compatible() -> bool:
    """Whether the OpenAI-compatible backend is selected."""
    return llm_provider() == PROVIDER_OPENAI_COMPATIBLE


def openai_compat_runtime_client(config: Config) -> Any:
    """Build the OpenAI-compatible stand-in for a bedrock-runtime client.

    Takes the read/connect timeouts, connection-pool size and retry budget from
    the same botocore ``Config`` the call site builds for Bedrock, so per-site
    tuning carries over.
    """
    from coa_common.openai_compat import OpenAICompatRuntimeClient

    return OpenAICompatRuntimeClient.from_boto_config(config)


def skip_unsupported_guardrail(guardrail_id: str | None, *, component: str) -> None:
    """Record that a requested Bedrock guardrail is skipped on a non-Bedrock backend.

    Bedrock Guardrails (``guardrailConfig`` / ``guardContent`` / ApplyGuardrail)
    have no OpenAI-compatible equivalent, so the guardrail cannot be applied. It
    must not be dropped silently either: this emits a structured warning, once
    per component and guardrail id, so an operator can see that content is
    flowing unscreened.

    Every guardrail-skipping site calls this function, which makes it the single
    place to plug in a domestic content-safety check later.
    """
    if not guardrail_id:
        return
    key = (component, guardrail_id)
    if key in _WARNED_GUARDRAILS:
        return
    _WARNED_GUARDRAILS.add(key)
    logger.warning(
        "guardrail_unsupported_skipped",
        provider=llm_provider(),
        component=component,
        guardrail_id=guardrail_id,
    )
