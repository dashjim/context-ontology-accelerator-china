# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for api_proxy_handler — Lambda proxy that forwards to ECS."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _set_env(monkeypatch):
    monkeypatch.setenv("ONTOLOGY_ENGINE_ENDPOINT", "http://ontology-engine.ns:8001")
    monkeypatch.setenv("ALLOWED_ORIGIN", "https://app.example.com")


@pytest.fixture()
def mod():
    from coa_ontology import api_proxy_handler

    return api_proxy_handler


class TestPathMapping:
    def test_induce_path(self, mod):
        assert mod._PATH_MAP["/namespaces/{namespaceId}/induce"] == "/induce/"

    def test_proposals_path(self, mod):
        assert mod._PATH_MAP["/namespaces/{namespaceId}/proposals"] == "/ontology/proposals/"

    def test_graph_search_path(self, mod):
        assert mod._PATH_MAP["/namespaces/{namespaceId}/graph/search"] == "/graph/search"


class TestSchemaQueryTranslation:
    """MCP's describe_schema spells its query params in camelCase; FastAPI binds
    snake_case. Unrenamed they are forwarded, ignored, and replaced by the route
    defaults — a silent wrong answer rather than an error.
    """

    @pytest.fixture()
    def forward(self, mod):
        """Run the handler against a stubbed ECS call and return the forwarded URL."""

        def _run(event: dict) -> str:
            mock_resp = MagicMock()
            mock_resp.status = 200
            mock_resp.read.return_value = b"{}"
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
                assert mod.handler(event, None)["statusCode"] == 200
                return mock_urlopen.call_args[0][0].full_url

        return _run

    def _event(self, **query) -> dict:
        return {
            "httpMethod": "GET",
            "resource": "/namespaces/{namespaceId}/graph/schema",
            "pathParameters": {"namespaceId": "ns-1"},
            "queryStringParameters": query or None,
            "body": "",
        }

    def test_max_results_renamed(self, forward):
        url = forward(self._event(maxResults="500"))
        assert "max_results=500" in url
        assert "maxResults" not in url

    def test_include_properties_renamed(self, forward):
        url = forward(self._event(includeProperties="false"))
        assert "include_properties=false" in url

    def test_namespace_still_threaded(self, forward):
        assert "namespace=ns-1" in forward(self._event(maxResults="10"))

    def test_empty_value_dropped(self, forward):
        """API Gateway supplies "" for a valueless param; forwarding it would
        override the route default with a blank and 422."""
        url = forward(self._event(maxResults=""))
        assert "max_results" not in url

    def test_aliases_do_not_leak_to_other_routes(self, forward):
        """The rename is scoped per backend path — a route that genuinely takes a
        camelCase param must keep receiving it verbatim."""
        event = {
            "httpMethod": "GET",
            "resource": "/namespaces/{namespaceId}/graph/search",
            "pathParameters": {"namespaceId": "ns-1"},
            "queryStringParameters": {"maxResults": "5"},
            "body": "",
        }
        assert "maxResults=5" in forward(event)


class TestHandler:
    @patch("urllib.request.urlopen")
    def test_successful_forward(self, mock_urlopen, mod):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"result": "ok"}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        event = {
            "httpMethod": "POST",
            "resource": "/namespaces/{namespaceId}/induce",
            "pathParameters": {"namespaceId": "ns-123"},
            "queryStringParameters": None,
            "body": '{"datasourceId": "ds-1"}',
        }
        result = mod.handler(event, None)

        assert result["statusCode"] == 200
        assert json.loads(result["body"]) == {"result": "ok"}
        call_args = mock_urlopen.call_args[0][0]
        assert "http://ontology-engine.ns:8001/induce/" in call_args.full_url
        assert "namespace=ns-123" in call_args.full_url

    @patch("urllib.request.urlopen")
    def test_path_param_substitution(self, mock_urlopen, mod):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"id": "p-1"}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        event = {
            "httpMethod": "GET",
            "resource": "/namespaces/{namespaceId}/proposals/{proposalId}",
            "pathParameters": {"namespaceId": "ns-1", "proposalId": "p-1"},
            "queryStringParameters": {},
            "body": "",
        }
        result = mod.handler(event, None)

        assert result["statusCode"] == 200
        call_url = mock_urlopen.call_args[0][0].full_url
        assert "/ontology/proposals/p-1" in call_url
        assert "namespace=ns-1" in call_url

    @patch("urllib.request.urlopen")
    def test_http_error_forwarded(self, mock_urlopen, mod):
        from urllib.error import HTTPError

        err = HTTPError(url="http://test", code=404, msg="Not Found", hdrs={}, fp=None)
        mock_urlopen.side_effect = err

        event = {
            "httpMethod": "GET",
            "resource": "/namespaces/{namespaceId}/ontologies",
            "pathParameters": {"namespaceId": "ns-1"},
            "queryStringParameters": None,
            "body": "",
        }
        result = mod.handler(event, None)

        assert result["statusCode"] == 404

    @patch("urllib.request.urlopen")
    def test_url_error_returns_502(self, mock_urlopen, mod):
        from urllib.error import URLError

        mock_urlopen.side_effect = URLError("Connection refused")

        event = {
            "httpMethod": "GET",
            "resource": "/namespaces/{namespaceId}/ontologies",
            "pathParameters": {"namespaceId": "ns-1"},
            "queryStringParameters": None,
            "body": "",
        }
        result = mod.handler(event, None)

        assert result["statusCode"] == 502
        assert "Cannot reach ontology-engine" in result["body"]

    @patch("urllib.request.urlopen")
    def test_query_params_forwarded(self, mock_urlopen, mod):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b"[]"
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        event = {
            "httpMethod": "GET",
            "resource": "/namespaces/{namespaceId}/graph/search",
            "pathParameters": {"namespaceId": "ns-1"},
            "queryStringParameters": {"q": "Customer", "limit": "10"},
            "body": "",
        }
        result = mod.handler(event, None)

        assert result["statusCode"] == 200
        call_url = mock_urlopen.call_args[0][0].full_url
        assert "q=Customer" in call_url
        assert "limit=10" in call_url
        assert "namespace=ns-1" in call_url


class TestSystemHealth:
    """Test /system-health endpoint security — no infrastructure details leak on error."""

    @patch("boto3.client")
    @patch("coa_ontology.api_proxy_handler.resolve_region", return_value="us-east-1")
    def test_dynamodb_error_returns_opaque_message(self, mock_region, mock_boto_client, mod, monkeypatch):
        """DynamoDB ClientError with ARN must not leak to response body."""
        from botocore.exceptions import ClientError

        error_resp = {
            "Error": {
                "Code": "AccessDeniedException",
                "Message": (
                    "User: arn:aws:iam::123456789012:assumed-role/OntologyLambdaRole/session "
                    "is not authorized to perform: dynamodb:DescribeTable on resource: "
                    "arn:aws:dynamodb:us-east-1:123456789012:table/semantic-sources-prod"
                ),
            }
        }
        ddb_client = MagicMock()
        ddb_client.describe_table.side_effect = ClientError(error_resp, "DescribeTable")
        mock_boto_client.return_value = ddb_client

        monkeypatch.setenv("SOURCES_TABLE", "semantic-sources-prod")
        monkeypatch.setenv("NEPTUNE_ENDPOINT", "")
        monkeypatch.setenv("OPENSEARCH_ENDPOINT", "")

        event = {"httpMethod": "GET", "resource": "/system-health"}
        result = mod.handler(event, None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["dynamodb"] == "error"
        assert "123456789012" not in result["body"]
        assert "arn:aws" not in result["body"]
        assert "semantic-sources-prod" not in result["body"]

    @patch("boto3.client")
    @patch("coa_ontology.api_proxy_handler.resolve_region", return_value="us-east-1")
    def test_neptune_error_returns_opaque_message(self, mock_region, mock_boto_client, mod, monkeypatch):
        """Neptune connectivity failure must not leak endpoint hostname."""
        neptune_client = MagicMock()
        neptune_client.execute_open_cypher_query.side_effect = Exception(
            "Could not connect to neptune-cluster-abc123.us-east-1.neptune.amazonaws.com:8182"
        )

        def client_factory(service, **kwargs):
            if service == "neptunedata":
                return neptune_client
            return MagicMock()

        mock_boto_client.side_effect = client_factory

        monkeypatch.setenv("SOURCES_TABLE", "")
        monkeypatch.setenv("NEPTUNE_ENDPOINT", "neptune-cluster-abc123.us-east-1.neptune.amazonaws.com")
        monkeypatch.setenv("OPENSEARCH_ENDPOINT", "")

        event = {"httpMethod": "GET", "resource": "/system-health"}
        result = mod.handler(event, None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["neptuneKg"] == "error"
        assert "neptune-cluster-abc123" not in result["body"]
        assert ".neptune.amazonaws.com" not in result["body"]

    @patch("boto3.client")
    @patch("coa_ontology.api_proxy_handler.resolve_region", return_value="us-east-1")
    def test_opensearch_error_returns_opaque_message(self, mock_region, mock_boto_client, mod, monkeypatch):
        """AOSS batch_get_collection failure must not leak cluster ID."""
        from botocore.exceptions import ClientError

        error_resp = {
            "Error": {
                "Code": "ValidationException",
                "Message": "Collection xyz789 does not exist in account 123456789012",
            }
        }
        aoss_client = MagicMock()
        aoss_client.batch_get_collection.side_effect = ClientError(error_resp, "BatchGetCollection")
        mock_boto_client.return_value = aoss_client

        monkeypatch.setenv("SOURCES_TABLE", "")
        monkeypatch.setenv("NEPTUNE_ENDPOINT", "")
        monkeypatch.setenv("OPENSEARCH_ENDPOINT", "https://xyz789.us-east-1.aoss.amazonaws.com")

        event = {"httpMethod": "GET", "resource": "/system-health"}
        result = mod.handler(event, None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["opensearch"] == "error"
        assert "xyz789" not in result["body"]
        assert "123456789012" not in result["body"]

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            ({"Created": True, "Deleted": False, "Processing": True}, "ok"),
            ({"Created": False, "Deleted": False}, "domain_status: creating"),
            ({"Created": True, "Deleted": True}, "domain_status: deleted"),
        ],
    )
    @patch("boto3.client")
    @patch("coa_ontology.api_proxy_handler.resolve_region", return_value="cn-north-1")
    def test_managed_domain_health_describes_the_domain(
        self, mock_region, mock_boto_client, mod, monkeypatch, status, expected
    ):
        """vector_store=opensearch: describe the domain, never call AOSS."""
        es_client = MagicMock()
        es_client.describe_domain.return_value = {"DomainStatus": status}

        def client_factory(service, **kwargs):
            assert service != "opensearchserverless"
            return es_client if service == "opensearch" else MagicMock()

        mock_boto_client.side_effect = client_factory
        monkeypatch.setenv("SOURCES_TABLE", "")
        monkeypatch.setenv("NEPTUNE_ENDPOINT", "")
        monkeypatch.setenv("OPENSEARCH_ENDPOINT", "https://vpc-coa-dev-vector-store-abc.cn-north-1.es.amazonaws.com.cn")
        monkeypatch.setenv("OPENSEARCH_DOMAIN_NAME", "coa-dev-vector-store")

        result = mod.handler({"httpMethod": "GET", "resource": "/system-health"}, None)

        assert json.loads(result["body"])["opensearch"] == expected
        es_client.describe_domain.assert_called_once_with(DomainName="coa-dev-vector-store")

    @patch("urllib.request.urlopen")
    @patch("coa_ontology.api_proxy_handler.resolve_region", return_value="us-east-1")
    def test_ontology_induction_readiness_error_opaque(self, mock_region, mock_urlopen, mod, monkeypatch):
        """Ontology-engine /readiness failure must not leak internal endpoint."""
        from urllib.error import URLError

        mock_urlopen.side_effect = URLError("Connection refused to http://ontology-engine.internal:8001/readiness")

        monkeypatch.setenv("SOURCES_TABLE", "")
        monkeypatch.setenv("NEPTUNE_ENDPOINT", "")
        monkeypatch.setenv("OPENSEARCH_ENDPOINT", "")

        event = {"httpMethod": "GET", "resource": "/system-health"}
        result = mod.handler(event, None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        ont_ind = body["ontologyInduction"]
        assert ont_ind["graphStore"] == "error"
        assert "ontology-engine.internal" not in result["body"]
        assert "8001" not in result["body"]


class TestCorsHeaders:
    """Test that CORS headers are set from ALLOWED_ORIGIN environment variable."""

    @patch("urllib.request.urlopen")
    def test_cors_header_in_success_response(self, mock_urlopen, mod):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"result": "ok"}'
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        event = {
            "httpMethod": "GET",
            "resource": "/namespaces/{namespaceId}/ontologies",
            "pathParameters": {"namespaceId": "ns-1"},
            "queryStringParameters": None,
            "body": "",
        }
        result = mod.handler(event, None)

        assert result["statusCode"] == 200
        assert result["headers"]["Access-Control-Allow-Origin"] == "https://app.example.com"

    @patch("urllib.request.urlopen")
    def test_cors_header_in_http_error_response(self, mock_urlopen, mod):
        from urllib.error import HTTPError

        err = HTTPError(url="http://test", code=404, msg="Not Found", hdrs={}, fp=None)
        mock_urlopen.side_effect = err

        event = {
            "httpMethod": "GET",
            "resource": "/namespaces/{namespaceId}/ontologies",
            "pathParameters": {"namespaceId": "ns-1"},
            "queryStringParameters": None,
            "body": "",
        }
        result = mod.handler(event, None)

        assert result["statusCode"] == 404
        assert result["headers"]["Access-Control-Allow-Origin"] == "https://app.example.com"

    @patch("urllib.request.urlopen")
    def test_cors_header_in_url_error_response(self, mock_urlopen, mod):
        from urllib.error import URLError

        mock_urlopen.side_effect = URLError("Connection refused")

        event = {
            "httpMethod": "GET",
            "resource": "/namespaces/{namespaceId}/ontologies",
            "pathParameters": {"namespaceId": "ns-1"},
            "queryStringParameters": None,
            "body": "",
        }
        result = mod.handler(event, None)

        assert result["statusCode"] == 502
        assert result["headers"]["Access-Control-Allow-Origin"] == "https://app.example.com"

    def test_cors_header_in_system_health_response(self, mod):
        """Test that system-health endpoint also uses configured CORS headers."""
        with patch("boto3.client") as mock_boto:
            mock_ddb = MagicMock()
            mock_ddb.describe_table.return_value = {"Table": {"TableStatus": "ACTIVE"}}
            mock_boto.return_value = mock_ddb

            result = mod._handle_system_health()

        assert result["statusCode"] == 200
        assert result["headers"]["Access-Control-Allow-Origin"] == "https://app.example.com"

    def test_missing_allowed_origin_defaults_to_empty(self, monkeypatch):
        """Missing ALLOWED_ORIGIN defaults to empty string (doesn't crash at import)."""
        monkeypatch.setenv("ONTOLOGY_ENGINE_ENDPOINT", "http://test:8001")
        monkeypatch.delenv("ALLOWED_ORIGIN", raising=False)

        import importlib

        from coa_ontology import api_proxy_handler

        importlib.reload(api_proxy_handler)
        assert api_proxy_handler._CORS_HEADERS["Access-Control-Allow-Origin"] == ""
