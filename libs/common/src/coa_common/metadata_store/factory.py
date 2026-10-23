# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Metadata store backend selection.

``METADATA_STORE_BACKEND`` picks the implementation every service builds:

- ``smus`` (default): :class:`SMUSClient`, DataZone-backed.
- ``dynamodb``: :class:`DynamoDBMetadataStoreClient`, for partitions without
  DataZone (e.g. AWS China regions). Requires ``METADATA_STORE_TABLE``; no
  DataZone domain id is needed.

Callers that gate on "is the metadata store configured?" use
:func:`missing_metadata_store_config` rather than checking the domain id.
"""

from __future__ import annotations

import os

from coa_common.config import resolve_region

from .base import MetadataStoreClient
from .dynamodb import DynamoDBMetadataStoreClient
from .exceptions import MetadataStoreError
from .smus import SMUSClient

_BACKENDS = ("smus", "dynamodb")


def metadata_store_backend() -> str:
    """The backend selected by ``METADATA_STORE_BACKEND`` (default ``smus``).

    Raises:
        MetadataStoreError: If the configured backend is unknown.
    """
    backend = os.environ.get("METADATA_STORE_BACKEND", "smus").strip().lower()
    if backend not in _BACKENDS:
        raise MetadataStoreError(f"Unknown METADATA_STORE_BACKEND {backend!r}; expected 'smus' or 'dynamodb'")
    return backend


def missing_metadata_store_config(*, domain_id: str | None, domain_id_var: str = "SMUS_DOMAIN_ID") -> str | None:
    """Name of the setting the selected backend still needs, or ``None`` when configured.

    SMUS needs a DataZone domain id; DynamoDB needs ``METADATA_STORE_TABLE``
    and ignores the domain id.

    Args:
        domain_id: The caller's DataZone domain id value (may be empty).
        domain_id_var: Env var the caller reads *domain_id* from, reported
            when it is the missing setting.

    Returns:
        The missing env var name, or ``None``.

    Raises:
        MetadataStoreError: If the configured backend is unknown.
    """
    if metadata_store_backend() == "dynamodb":
        return None if os.environ.get("METADATA_STORE_TABLE") else "METADATA_STORE_TABLE"
    return None if domain_id else domain_id_var


def build_metadata_store_client(
    *,
    domain_id: str,
    region_name: str | None = None,
    assume_role_arn: str | None = None,
    session_name: str | None = None,
) -> MetadataStoreClient:
    """Construct the metadata store client selected by ``METADATA_STORE_BACKEND``.

    Takes the :class:`SMUSClient` constructor arguments so call sites switch
    backends without changing. The DynamoDB backend ignores *domain_id*,
    *assume_role_arn* and *session_name*: it is authorized by the caller's own
    IAM permissions on the table, not by DataZone project membership.

    Args:
        domain_id: SMUS domain identifier (SMUS backend only).
        region_name: AWS region; defaults to the runtime region.
        assume_role_arn: Optional role to assume for DataZone calls (SMUS only).
        session_name: Optional STS session name (SMUS only).

    Returns:
        The configured metadata store client.

    Raises:
        MetadataStoreError: If the backend is unknown or its configuration is missing.
    """
    if metadata_store_backend() == "smus":
        return SMUSClient(
            domain_id=domain_id,
            region_name=region_name,
            assume_role_arn=assume_role_arn,
            session_name=session_name,
        )
    table_name = os.environ.get("METADATA_STORE_TABLE", "")
    if not table_name:
        raise MetadataStoreError("METADATA_STORE_TABLE must be set when METADATA_STORE_BACKEND=dynamodb")
    return DynamoDBMetadataStoreClient(table_name=table_name, region_name=region_name or resolve_region())
