# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""AOSS Search Proxy Lambda.

Minimal proxy that executes opensearch-py queries against AOSS.
Deployed in VPC with IAM SigV4 auth. Called by AgentCore containers
that cannot directly access AOSS due to service principal restrictions.
Also serves a managed OpenSearch domain (vector_store=opensearch), signing
for ``es`` instead of ``aoss`` based on the endpoint host.

TODO(aoss-proxy): Remove this Lambda when AgentCore natively supports AOSS data-plane access.
"""

from __future__ import annotations

import os
import re

import boto3
from coa_common import opensearch_client_kwargs, opensearch_signing_service, resolve_region
from opensearchpy import NotFoundError, OpenSearch, RequestsHttpConnection
from opensearchpy.helpers.signer import RequestsAWSV4SignerAuth

_REGION = resolve_region()
_ENDPOINT = os.environ["OPENSEARCH_ENDPOINT"].replace("https://", "").replace("http://", "").rstrip("/")
_MAX_RESULT_SIZE = 50
_ALLOWED_ACTIONS = {"search", "health_check"}
_INDEX_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")

_client: OpenSearch | None = None


def _get_client() -> OpenSearch:
    global _client
    if _client is None:
        session = boto3.Session()
        credentials = session.get_credentials()
        if not credentials:
            raise RuntimeError("AWS credentials not available")
        auth = RequestsAWSV4SignerAuth(credentials, _REGION, opensearch_signing_service(_ENDPOINT))
        _client = OpenSearch(
            hosts=[{"host": _ENDPOINT, "port": 443}],
            http_auth=auth,
            use_ssl=True,
            verify_certs=True,
            connection_class=RequestsHttpConnection,
            **opensearch_client_kwargs(sync=True),
        )
    return _client


def handler(event, context):
    """Execute a validated AOSS query on behalf of an AgentCore caller.

    Dispatches on ``event["action"]``: ``health_check`` returns cluster info,
    ``search`` runs the provided query body against the requested index. The
    index name is validated against an allow-list pattern, the query is
    restricted to ``knn``/``bool``/``match_all`` shapes, and the result size is
    capped to guard against large data exfiltration.

    Args:
        event: Lambda event with ``action``, and for searches ``index`` and a
            ``body`` dict holding the OpenSearch query.
        context: Lambda runtime context (unused).

    Returns:
        A dict with a ``statusCode`` and either a ``body`` (on success) or an
        ``error`` message (on validation or execution failure). A missing index
        yields a 200 with an empty hit set.
    """
    try:
        action = event.get("action", "search")
        if action not in _ALLOWED_ACTIONS:
            return {"statusCode": 400, "error": f"Invalid action: {action}"}

        if action == "health_check":
            info = _get_client().info()
            return {"statusCode": 200, "body": info}

        index = event.get("index", "document-chunks")
        if not _INDEX_PATTERN.match(index):
            return {"statusCode": 400, "error": f"Invalid index name: {index}"}

        body = event.get("body", {})
        if not isinstance(body, dict):
            return {"statusCode": 400, "error": "body must be a dict"}

        # Validate query shape — only allow knn, bool, and match_all queries
        query = body.get("query", {})
        if query:
            allowed_keys = {"knn", "bool", "match_all"}
            if not set(query.keys()) & allowed_keys:
                return {"statusCode": 400, "error": "Only knn, bool, or match_all queries allowed"}

        # Cap result size to prevent large data exfiltration
        body["size"] = min(body.get("size", 10), _MAX_RESULT_SIZE)

        result = _get_client().search(index=index, body=body)
        return {"statusCode": 200, "body": result}
    except NotFoundError:
        return {"statusCode": 200, "body": {"hits": {"total": {"value": 0}, "hits": []}}}
    except Exception as e:
        import logging

        logging.exception("AOSS search error: %s", e)
        return {"statusCode": 500, "error": f"Internal search error: {str(e)[:200]}"}
