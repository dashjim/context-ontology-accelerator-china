# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Where and how native Amazon Bedrock is reached: region, API-key auth, model ids.

A deployment in a region without Bedrock (AWS China) can call Bedrock in a
commercial region instead. Every site that builds a Bedrock client — the boto3
``bedrock-runtime`` clients, Strands ``BedrockModel`` and LlamaIndex
``BedrockConverse`` — goes through :func:`prepare_bedrock_client`, and every
chat/embedding model id is passed through :func:`map_bedrock_model_id`.

Configuration (environment); with none of these set, nothing changes:

* ``BEDROCK_REGION`` — region of every Bedrock client, e.g. ``us-east-1``. It
  wins over the region a call site passes in, since that is usually derived from
  the deployment region. Non-Bedrock clients never read it.
* ``BEDROCK_API_KEY_SECRET_ARN`` — Secrets Manager secret whose SecretString is a
  long-term Bedrock API key. Fetched once per process (with the process's own
  IAM role, in the secret's region) into ``AWS_BEARER_TOKEN_BEDROCK``, which
  botocore (>= 1.39) then sends as ``Authorization: Bearer`` on every Bedrock
  client created afterwards, in preference to SigV4 role credentials. An
  ``AWS_BEARER_TOKEN_BEDROCK`` already in the environment is left as is.
* ``BEDROCK_MODEL_MAP`` — JSON object mapping the model ids the code passes
  (env-configured ids, built-in defaults, per-request overrides, model ARNs) to
  the ids actually invoked. An ARN is also looked up by its resource id (the part
  after the last ``/``).
* ``BEDROCK_MODEL_DEFAULT`` — the id used for any Anthropic model id (one
  containing ``anthropic``) absent from the map.

The model map applies to the native Bedrock provider only; the
``LLM_PROVIDER=openai_compatible`` backend has its own (``OPENAI_COMPAT_MODEL_MAP``).
"""

from __future__ import annotations

import functools
import json
import os
import threading

import boto3
import structlog

from coa_common.aws_config import sync_boto_config
from coa_common.config import resolve_region
from coa_common.llm_provider import is_openai_compatible

logger = structlog.get_logger(__name__)

# botocore reads this per signing name ("bedrock" covers bedrock-runtime).
BEARER_TOKEN_ENV = "AWS_BEARER_TOKEN_BEDROCK"

_api_key_lock = threading.Lock()


def bedrock_region(region: str | None = None) -> str:
    """Region for a Bedrock client: ``BEDROCK_REGION``, else ``region``, else :func:`resolve_region`."""
    return os.environ.get("BEDROCK_REGION") or region or resolve_region()


def ensure_bedrock_api_key() -> None:
    """Load the Bedrock API key named by ``BEDROCK_API_KEY_SECRET_ARN`` into the environment.

    No-op when the ARN is unset or ``AWS_BEARER_TOKEN_BEDROCK`` is already set, so
    the secret is read at most once per process. Must run before a Bedrock client
    is created: botocore resolves the token at client creation.

    Raises:
        ValueError: The ARN is malformed or the secret is empty.
        botocore.exceptions.ClientError: Secrets Manager refused the read; the
            failure is not cached, so the next client creation retries.
    """
    secret_arn = os.environ.get("BEDROCK_API_KEY_SECRET_ARN", "").strip()
    if not secret_arn or os.environ.get(BEARER_TOKEN_ENV):
        return
    with _api_key_lock:
        if os.environ.get(BEARER_TOKEN_ENV):
            return
        parts = secret_arn.split(":")
        if len(parts) < 7 or parts[2] != "secretsmanager" or not parts[3]:
            raise ValueError(f"BEDROCK_API_KEY_SECRET_ARN is not a Secrets Manager secret ARN: {secret_arn!r}")
        # The secret's own region (the deployment region in AWS China), read with
        # the process's IAM role; only Bedrock itself is called with the key.
        client = boto3.client("secretsmanager", region_name=parts[3], config=sync_boto_config())
        key = (client.get_secret_value(SecretId=secret_arn).get("SecretString") or "").strip()
        if not key:
            raise ValueError(f"Secret {secret_arn} holds no Bedrock API key")
        os.environ[BEARER_TOKEN_ENV] = key
        logger.info("bedrock_api_key_loaded", secret_arn=secret_arn)


def prepare_bedrock_client(region: str | None = None) -> str:
    """Prepare to create a Bedrock client: load the API key if configured, return the region.

    Call it for the ``region_name`` of every Bedrock client (boto3, Strands,
    LlamaIndex) so each one honours ``BEDROCK_REGION`` and the API key.
    """
    ensure_bedrock_api_key()
    return bedrock_region(region)


@functools.cache
def _model_map(raw: str) -> dict[str, str]:
    mapping = json.loads(raw) if raw else {}
    if not isinstance(mapping, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in mapping.items()):
        raise ValueError("BEDROCK_MODEL_MAP must be a JSON object of model-id strings")
    return mapping


def map_bedrock_model_id(model_id: str) -> str:
    """Return the Bedrock model id to invoke for ``model_id`` (see module docstring).

    Raises:
        ValueError: ``BEDROCK_MODEL_MAP`` is not a JSON object of strings.
    """
    raw_map = os.environ.get("BEDROCK_MODEL_MAP", "").strip()
    default = os.environ.get("BEDROCK_MODEL_DEFAULT", "").strip()
    if not (raw_map or default) or is_openai_compatible():
        return model_id
    mapping = _model_map(raw_map)
    if model_id in mapping:
        return mapping[model_id]
    if model_id.startswith("arn:") and "/" in model_id:
        resource_id = model_id.rsplit("/", 1)[1]
        if resource_id in mapping:
            return mapping[resource_id]
    if default and "anthropic" in model_id.lower():
        return default
    return model_id
