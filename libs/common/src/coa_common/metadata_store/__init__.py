# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Metadata store abstraction and implementations."""

from .base import AssetResult, MetadataStoreClient, ProjectResult, SearchResult
from .catalog_reader import read_approved_catalog
from .dynamodb import DynamoDBMetadataStoreClient
from .exceptions import MetadataStoreError, MetadataStoreNotFoundError
from .factory import build_metadata_store_client, metadata_store_backend, missing_metadata_store_config
from .reader import read_assets_for_datasource
from .smus import SMUSClient

__all__ = [
    "AssetResult",
    "DynamoDBMetadataStoreClient",
    "MetadataStoreClient",
    "MetadataStoreError",
    "MetadataStoreNotFoundError",
    "ProjectResult",
    "SMUSClient",
    "SearchResult",
    "build_metadata_store_client",
    "metadata_store_backend",
    "missing_metadata_store_config",
    "read_approved_catalog",
    "read_assets_for_datasource",
]
