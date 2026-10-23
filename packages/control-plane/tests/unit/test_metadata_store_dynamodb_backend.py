# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Namespace create/delete on the DynamoDB metadata store backend, with no DataZone domain id."""

from __future__ import annotations

from unittest.mock import MagicMock

import boto3
import pytest
from coa_common.dao import PaginatedResult
from coa_common.metadata_store import DynamoDBMetadataStoreClient
from coa_common.metadata_store.dynamodb import PROJECT_ASSETS_INDEX
from coa_control_plane.namespace import cleanup, create_handler
from coa_control_plane.namespace.service import NamespaceService
from coa_control_plane_server.models.create_namespace_request_content import CreateNamespaceRequestContent
from moto import mock_aws

pytestmark = pytest.mark.unit

TABLE = "test-metadata-store"
REGION = "us-east-1"
ROLE_ARN = "arn:aws:iam::123456789012:role/project-access"
ROLE_ARN_PARAM = "/coa/smus/dz-project-access-role-arn"


@pytest.fixture
def dynamodb_backend(monkeypatch):
    """DynamoDB backend selected, no DataZone domain id anywhere, moto-backed AWS."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_REGION", REGION)
    monkeypatch.setenv("METADATA_STORE_BACKEND", "dynamodb")
    monkeypatch.setenv("METADATA_STORE_TABLE", TABLE)
    monkeypatch.setenv("PROJECT_ACCESS_ROLE_ARN_SSM", ROLE_ARN_PARAM)
    monkeypatch.delenv("DATAZONE_DOMAIN_ID", raising=False)
    monkeypatch.delenv("PROJECT_ACCESS_ROLE_ARN", raising=False)
    monkeypatch.delenv("ATHENA_RESULTS_BUCKET_SSM", raising=False)
    with mock_aws():
        boto3.client("dynamodb", region_name=REGION).create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[
                {"AttributeName": n, "AttributeType": "S"} for n in ("PK", "SK", "projectId", "name")
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": PROJECT_ASSETS_INDEX,
                    "KeySchema": [
                        {"AttributeName": "projectId", "KeyType": "HASH"},
                        {"AttributeName": "name", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        boto3.client("ssm", region_name=REGION).put_parameter(Name=ROLE_ARN_PARAM, Value=ROLE_ARN, Type="String")
        yield


def _items() -> list[dict]:
    return boto3.resource("dynamodb", region_name=REGION).Table(TABLE).scan()["Items"]


def test_create_handler_builds_dynamodb_store_without_domain_id(dynamodb_backend, monkeypatch):
    for var in ("NAMESPACES_TABLE", "ROLES_TABLE", "RESOURCE_ROLE_MAPPINGS_TABLE"):
        monkeypatch.setenv(var, f"test-{var.lower()}")
    monkeypatch.setattr(create_handler, "_service", None)

    service = create_handler._get_service()

    assert isinstance(service._metadata_store, DynamoDBMetadataStoreClient)


def test_namespace_create_then_delete_without_domain_id(dynamodb_backend):
    """Create registers the project + project-access membership; teardown removes both."""
    roles_dao = MagicMock()
    roles_dao.query.return_value = PaginatedResult(
        items=[{"PK": "NAMESPACE_TEMPLATE", "SK": "ROLE#namespace-owner", "name": "Owner", "cedarPolicy": "p"}]
    )
    ns_dao = MagicMock()
    ns_dao.query.return_value = PaginatedResult(items=[])
    service = NamespaceService(
        namespaces_dao=ns_dao,
        roles_dao=roles_dao,
        mappings_dao=MagicMock(),
        metadata_store=DynamoDBMetadataStoreClient(table_name=TABLE, region_name=REGION),
    )

    service.create(CreateNamespaceRequestContent(name="sales", owner="alice@co.com"))

    project = next(i for i in _items() if i["SK"] == "PROJECT")
    project_id = project["PK"].removeprefix("PROJECT#")
    member = next(i for i in _items() if i["SK"].startswith("MEMBER#"))
    assert member["PK"] == f"PROJECT#{project_id}"
    assert member["userIdentifier"] == ROLE_ARN

    service._metadata_store.create_asset(project_id=project_id, name="DS#s1:db.t", type_identifier="T")
    cleanup.delete_smu_project("ns-1", {"dataZoneProjectId": project_id})

    assert _items() == []
