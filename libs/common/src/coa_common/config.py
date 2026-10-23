# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Application configuration using Pydantic Settings."""

from __future__ import annotations

import os
import re
from urllib.parse import urlsplit

from pydantic import Field
from pydantic_settings import BaseSettings


def resolve_region(default: str = "us-east-1") -> str:
    """Resolve the active AWS region from the runtime environment.

    Checks ``AWS_REGION`` first, then falls back to ``AWS_DEFAULT_REGION``,
    then ``default``. In Lambda, ``AWS_REGION`` is always set by the runtime
    so the fallback never fires in production. Local dev and test environments
    benefit from the consistent fallback chain instead of each call site
    picking its own strategy.

    This is the single source of truth for region resolution across Python
    services. Do not inline ``os.environ.get("AWS_REGION", ...)`` at new call
    sites — import and use this helper instead.
    """
    return os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", default))


def aws_partition(region: str) -> str:
    """Return the ARN partition for ``region`` (``aws``, ``aws-cn`` or ``aws-us-gov``)."""
    if region.startswith("us-gov-"):
        return "aws-us-gov"
    if region.startswith("cn-"):
        return "aws-cn"
    return "aws"


def aws_dns_suffix(region: str) -> str:
    """Return the service endpoint DNS suffix for ``region`` (``amazonaws.com.cn`` in China)."""
    return "amazonaws.com.cn" if region.startswith("cn-") else "amazonaws.com"


# Managed OpenSearch Service domain endpoints, e.g.
# ``vpc-<name>-<id>.cn-north-1.es.amazonaws.com.cn``.
_MANAGED_OPENSEARCH_HOST = re.compile(r"\.es\.amazonaws\.com(\.cn)?$")


def opensearch_signing_service(endpoint: str) -> str:
    """Return the SigV4 service name for an OpenSearch ``endpoint`` (URL or bare host).

    ``es`` for a managed OpenSearch Service domain (``*.es.amazonaws.com[.cn]``,
    the ``vector_store=opensearch`` deployment in AWS China); ``aoss`` for
    everything else, i.e. an OpenSearch Serverless collection.
    """
    host = urlsplit(endpoint if "://" in endpoint else f"https://{endpoint}").hostname or ""
    return "es" if _MANAGED_OPENSEARCH_HOST.search(host) else "aoss"


class SCLConfig(BaseSettings):
    """Base configuration for all services.

    Values are loaded from environment variables with the ``SCL_`` prefix.
    """

    model_config = {"env_prefix": "SCL_"}

    # General
    project_name: str = "coa"
    environment: str = "dev"
    log_level: str = "INFO"

    # AWS — SCL_AWS_REGION wins when set; otherwise resolve from the runtime
    # environment (AWS_REGION, then AWS_DEFAULT_REGION, then us-east-1).
    # Nothing in infra/ sets SCL_AWS_REGION, so a hardcoded default here would
    # silently pin any consumer to us-east-1 in every other region — the same
    # mismatch that broke every MCP tool outside us-east-1 (#92).
    aws_region: str = Field(default_factory=resolve_region)

    # DynamoDB
    dynamodb_table_prefix: str = "coa"

    # S3
    s3_bucket_prefix: str = "coa"

    # OpenSearch
    opensearch_endpoint: str = ""

    # Neptune / SPARQL
    sparql_endpoint: str = ""
