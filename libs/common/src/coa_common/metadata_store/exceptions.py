# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Metadata store exceptions."""

from coa_common.exceptions import SCLError


class MetadataStoreError(SCLError):
    """Raised when a metadata store operation fails."""

    def __init__(self, message: str) -> None:
        """Initialize with ``message`` and the METADATA_STORE_ERROR code.

        Args:
            message: Human-readable description of the metadata store failure.
        """
        super().__init__(message, code="METADATA_STORE_ERROR")


class MetadataStoreNotFoundError(MetadataStoreError):
    """Raised when the targeted project or asset does not exist.

    Subclasses :class:`MetadataStoreError` so existing ``except
    MetadataStoreError`` handlers keep catching it; callers that treat
    absence as success (e.g. idempotent teardown) can catch this narrower type.
    """
