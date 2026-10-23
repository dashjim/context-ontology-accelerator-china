# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the DynamoDB metadata store backend and backend selection (moto)."""

from __future__ import annotations

import json
from unittest.mock import patch

import boto3
import pytest
from coa_common.datazone_forms import FORM_TYPE_NAME
from coa_common.metadata_store import (
    DynamoDBMetadataStoreClient,
    MetadataStoreError,
    MetadataStoreNotFoundError,
    SMUSClient,
    build_metadata_store_client,
    metadata_store_backend,
    missing_metadata_store_config,
)
from coa_common.metadata_store.dynamodb import PROJECT_ASSETS_INDEX
from moto import mock_aws

pytestmark = pytest.mark.unit

TABLE = "test-metadata-store"
REGION = "us-east-1"
ROLE_ARN = "arn:aws:iam::123456789012:role/project-access"


def _create_table() -> None:
    boto3.client("dynamodb", region_name=REGION).create_table(
        TableName=TABLE,
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
            {"AttributeName": "projectId", "AttributeType": "S"},
            {"AttributeName": "name", "AttributeType": "S"},
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


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        _create_table()
        yield DynamoDBMetadataStoreClient(table_name=TABLE, region_name=REGION)


def _form(content: dict) -> list[dict[str, str]]:
    return [{"formName": FORM_TYPE_NAME, "content": json.dumps(content), "typeIdentifier": FORM_TYPE_NAME}]


def _all_items() -> list[dict]:
    return boto3.resource("dynamodb", region_name=REGION).Table(TABLE).scan()["Items"]


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------


class TestProjects:
    def test_create_and_get(self, store):
        created = store.create_project(name="ns-1", description="d", project_profile_id="pp")
        assert created.name == "ns-1"
        assert store.get_project(project_id=created.project_id) == created

    def test_get_missing_raises_not_found(self, store):
        with pytest.raises(MetadataStoreNotFoundError):
            store.get_project(project_id="nope")

    def test_delete_missing_raises_not_found(self, store):
        with pytest.raises(MetadataStoreNotFoundError):
            store.delete_project(project_id="nope", force=True)

    def test_delete_refuses_project_with_assets_unless_forced(self, store):
        pid = store.create_project(name="ns-1").project_id
        store.create_asset(project_id=pid, name="DS#s1:db.t1", type_identifier="T")

        with pytest.raises(MetadataStoreError, match="still owns 1 asset"):
            store.delete_project(project_id=pid)
        assert store.get_project(project_id=pid).project_id == pid

    def test_force_delete_removes_project_assets_and_memberships(self, store):
        pid = store.create_project(name="ns-1").project_id
        other = store.create_project(name="ns-2").project_id
        store.create_project_membership(project_id=pid, user_identifier=ROLE_ARN)
        for i in range(30):  # > one batch_write chunk of 25
            store.create_asset(project_id=pid, name=f"DS#s1:db.t{i}", type_identifier="T")
        kept = store.create_asset(project_id=other, name="DS#s1:db.t0", type_identifier="T")

        store.delete_project(project_id=pid, force=True)

        with pytest.raises(MetadataStoreNotFoundError):
            store.get_project(project_id=pid)
        remaining = _all_items()
        assert {i["PK"] for i in remaining} == {f"PROJECT#{other}", f"ASSET#{kept.asset_id}"}

    def test_delete_empty_project_without_force(self, store):
        pid = store.create_project(name="ns-1").project_id
        store.delete_project(project_id=pid)
        assert _all_items() == []


# ---------------------------------------------------------------------------
# Assets
# ---------------------------------------------------------------------------


class TestAssets:
    def test_create_get_and_forms(self, store):
        pid = store.create_project(name="ns-1").project_id
        asset = store.create_asset(
            project_id=pid,
            name="DS#s1:db.orders",
            type_identifier="CoaRelationalTable",
            description="orders table",
            forms_input=_form({"tableName": "orders"}),
        )

        assert store.get_asset(asset_id=asset.asset_id) == asset
        detail = store.get_asset_forms(asset_id=asset.asset_id)
        assert detail["id"] == asset.asset_id
        assert detail["owningProjectId"] == pid
        assert detail["revision"] == "1"
        assert detail["formsOutput"] == [
            {"formName": FORM_TYPE_NAME, "typeName": FORM_TYPE_NAME, "content": json.dumps({"tableName": "orders"})}
        ]

    def test_create_in_missing_project_raises_not_found(self, store):
        with pytest.raises(MetadataStoreNotFoundError):
            store.create_asset(project_id="nope", name="DS#s1:db.t", type_identifier="T")
        assert _all_items() == []

    def test_revision_replaces_fields_merges_forms_and_bumps_revision(self, store):
        pid = store.create_project(name="ns-1").project_id
        other_form = {"formName": "Other", "content": "{}", "typeIdentifier": "Other"}
        asset = store.create_asset(
            project_id=pid,
            name="DS#s1:db.orders",
            type_identifier="T",
            description="old",
            forms_input=[*_form({"v": 1}), other_form],
        )

        revised = store.create_asset_revision(
            asset_id=asset.asset_id, name="DS#s1:db.orders", description="new", forms_input=_form({"v": 2})
        )

        assert revised.project_id == pid
        detail = store.get_asset_forms(asset_id=asset.asset_id)
        assert detail["description"] == "new"
        assert detail["revision"] == "2"
        forms = {f["formName"]: f["content"] for f in detail["formsOutput"]}
        assert forms == {FORM_TYPE_NAME: json.dumps({"v": 2}), "Other": "{}"}

    def test_revision_of_missing_asset_raises_not_found(self, store):
        with pytest.raises(MetadataStoreNotFoundError):
            store.create_asset_revision(asset_id="nope", name="x")
        assert _all_items() == []

    def test_delete_asset(self, store):
        pid = store.create_project(name="ns-1").project_id
        asset = store.create_asset(project_id=pid, name="DS#s1:db.t", type_identifier="T")

        store.delete_asset(asset_id=asset.asset_id)

        with pytest.raises(MetadataStoreNotFoundError):
            store.get_asset(asset_id=asset.asset_id)
        with pytest.raises(MetadataStoreNotFoundError):
            store.delete_asset(asset_id=asset.asset_id)


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


class TestSearch:
    def _seed(self, store) -> tuple[str, str]:
        pid = store.create_project(name="ns-1").project_id
        other = store.create_project(name="ns-2").project_id
        for name in ["DS#s1:db.orders", "DS#s1:db.order_items", "DS#s10:db.orders", "DS#s2:db.users"]:
            store.create_asset(project_id=pid, name=name, type_identifier="T", forms_input=_form({"n": name}))
        store.create_asset(project_id=other, name="DS#s1:db.orders", type_identifier="T")
        return pid, other

    def _search_all(self, store, pid: str, text: str, page_size: int) -> list[str]:
        names: list[str] = []
        token = None
        while True:
            result = store.search_assets(project_id=pid, search_text=text, max_results=page_size, next_token=token)
            names.extend(a.name for a in result.items)
            token = result.next_token
            if not token:
                return names

    def test_prefix_search_scoped_to_project_and_paginated(self, store):
        pid, _ = self._seed(store)
        # Page size 1 forces the token round-trip on every item. Results are in
        # byte order of the name ("0" sorts before ":").
        assert self._search_all(store, pid, "DS#s1", page_size=1) == [
            "DS#s10:db.orders",
            "DS#s1:db.order_items",
            "DS#s1:db.orders",
        ]
        assert self._search_all(store, pid, "DS#s1:", page_size=50) == ["DS#s1:db.order_items", "DS#s1:db.orders"]

    def test_empty_search_text_lists_all_project_assets(self, store):
        pid, _ = self._seed(store)
        assert len(self._search_all(store, pid, "", page_size=2)) == 4

    def test_include_forms(self, store):
        pid, _ = self._seed(store)
        with_forms = store.search_assets(project_id=pid, search_text="DS#s2", include_forms=True)
        without = store.search_assets(project_id=pid, search_text="DS#s2")

        assert json.loads(with_forms.items[0].forms_output[0]["content"]) == {"n": "DS#s2:db.users"}
        assert without.items[0].forms_output is None

    def test_find_asset_by_name_is_exact(self, store):
        pid, other = self._seed(store)
        found = store.find_asset_by_name(project_id=pid, name="DS#s1:db.orders")

        assert found is not None
        assert found.name == "DS#s1:db.orders"
        assert found.project_id == pid
        assert store.find_asset_by_name(project_id=pid, name="DS#s1:db.order") is None

    def test_name_attribute_restriction_is_accepted(self, store):
        pid, _ = self._seed(store)
        result = store.search_assets(project_id=pid, search_text="DS#s2", search_in_attributes=["name"])
        assert [a.name for a in result.items] == ["DS#s2:db.users"]

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"filters": {"filter": {"attribute": "name", "value": "x"}}},
            {"search_in_attributes": ["description"]},
            {"next_token": "not-a-token"},
        ],
    )
    def test_unsupported_options_raise(self, store, kwargs):
        with pytest.raises(MetadataStoreError):
            store.search_assets(project_id="p", search_text="x", **kwargs)


# ---------------------------------------------------------------------------
# Memberships
# ---------------------------------------------------------------------------


class TestMemberships:
    def test_records_membership(self, store):
        pid = store.create_project(name="ns-1").project_id
        store.create_project_membership(project_id=pid, user_identifier=ROLE_ARN, designation="PROJECT_OWNER")

        member = next(i for i in _all_items() if i["SK"].startswith("MEMBER#"))
        assert member["PK"] == f"PROJECT#{pid}"
        assert member["userIdentifier"] == ROLE_ARN
        assert member["designation"] == "PROJECT_OWNER"

    def test_missing_project_raises_not_found(self, store):
        """A membership must never be recorded against a project that does not exist."""
        with pytest.raises(MetadataStoreNotFoundError):
            store.create_project_membership(project_id="nope", user_identifier=ROLE_ARN)
        assert _all_items() == []

    def test_duplicate_membership_raises(self, store):
        """Same contract as SMUS: re-adding an existing member is an error, not a silent success."""
        pid = store.create_project(name="ns-1").project_id
        store.create_project_membership(project_id=pid, user_identifier=ROLE_ARN)

        with pytest.raises(MetadataStoreError, match="already a member"):
            store.create_project_membership(project_id=pid, user_identifier=ROLE_ARN)


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------


class TestBuildMetadataStoreClient:
    def test_defaults_to_smus(self, monkeypatch):
        monkeypatch.delenv("METADATA_STORE_BACKEND", raising=False)
        # Mocked boto3: a real client build normalizes the shared module-level
        # retry Config in place, which test_smus_extra asserts on.
        with patch("coa_common.metadata_store.smus.boto3") as mock_boto3:
            client = build_metadata_store_client(domain_id="dzd_1", region_name=REGION)
        assert isinstance(client, SMUSClient)
        mock_boto3.client.assert_called_once()

    def test_dynamodb_backend(self, monkeypatch):
        monkeypatch.setenv("METADATA_STORE_BACKEND", "dynamodb")
        monkeypatch.setenv("METADATA_STORE_TABLE", TABLE)
        client = build_metadata_store_client(
            domain_id="ignored", region_name=REGION, assume_role_arn=ROLE_ARN, session_name="s"
        )
        assert isinstance(client, DynamoDBMetadataStoreClient)

    def test_dynamodb_backend_requires_table(self, monkeypatch):
        monkeypatch.setenv("METADATA_STORE_BACKEND", "dynamodb")
        monkeypatch.delenv("METADATA_STORE_TABLE", raising=False)
        with pytest.raises(MetadataStoreError, match="METADATA_STORE_TABLE"):
            build_metadata_store_client(domain_id="d", region_name=REGION)

    def test_unknown_backend_raises(self, monkeypatch):
        monkeypatch.setenv("METADATA_STORE_BACKEND", "glue")
        with pytest.raises(MetadataStoreError, match="Unknown METADATA_STORE_BACKEND"):
            build_metadata_store_client(domain_id="d", region_name=REGION)


class TestMissingMetadataStoreConfig:
    def test_smus_needs_domain_id(self, monkeypatch):
        monkeypatch.delenv("METADATA_STORE_BACKEND", raising=False)
        assert missing_metadata_store_config(domain_id="dzd_1") is None
        assert missing_metadata_store_config(domain_id="") == "SMUS_DOMAIN_ID"
        assert missing_metadata_store_config(domain_id=None, domain_id_var="DATAZONE_DOMAIN_ID") == "DATAZONE_DOMAIN_ID"

    def test_dynamodb_needs_table_not_domain_id(self, monkeypatch):
        monkeypatch.setenv("METADATA_STORE_BACKEND", "dynamodb")
        monkeypatch.setenv("METADATA_STORE_TABLE", TABLE)
        assert missing_metadata_store_config(domain_id="") is None

        monkeypatch.delenv("METADATA_STORE_TABLE")
        assert missing_metadata_store_config(domain_id="dzd_1") == "METADATA_STORE_TABLE"

    def test_unknown_backend_raises(self, monkeypatch):
        monkeypatch.setenv("METADATA_STORE_BACKEND", "glue")
        with pytest.raises(MetadataStoreError, match="Unknown METADATA_STORE_BACKEND"):
            missing_metadata_store_config(domain_id="d")

    def test_backend_is_normalized(self, monkeypatch):
        monkeypatch.setenv("METADATA_STORE_BACKEND", " DynamoDB ")
        assert metadata_store_backend() == "dynamodb"
