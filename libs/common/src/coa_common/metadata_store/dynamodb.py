# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""DynamoDB implementation of MetadataStoreClient.

Stand-in for SageMaker Unified Studio (DataZone) in partitions where DataZone
is not available (e.g. AWS China regions). Single table, one sparse GSI:

==============  ==========================  ===============================
Item            PK                          SK
==============  ==========================  ===============================
Project         ``PROJECT#{projectId}``     ``PROJECT``
Membership      ``PROJECT#{projectId}``     ``MEMBER#{userIdentifier}``
Asset           ``ASSET#{assetId}``         ``ASSET``
==============  ==========================  ===============================

GSI ``ProjectAssetsIndex``: partition key ``projectId`` (S), sort key
``name`` (S), projection ALL. Only ASSET items carry a ``projectId``
attribute, so the index is sparse to assets. ``search_assets`` and
``find_asset_by_name`` are key-condition queries on it.

Attributes per item type:

- Project: ``entityType``, ``name``, ``description``, ``projectProfileId``,
  ``createdAt``, ``updatedAt``.
- Membership: ``entityType``, ``userIdentifier``, ``designation``, ``createdAt``.
- Asset: ``entityType``, ``assetId``, ``projectId``, ``name``, ``description``,
  ``typeIdentifier``, ``formsOutput`` (list of ``{formName, typeName,
  content}`` maps, the DataZone ``formsOutput`` shape), ``revision`` (N),
  ``createdAt``, ``updatedAt``.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import time
import uuid
from typing import Any

from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError

from coa_common.dao import DynamoDBDAO, QueryParams

from .base import AssetResult, MetadataStoreClient, ProjectResult, SearchResult
from .exceptions import MetadataStoreError, MetadataStoreNotFoundError

logger = logging.getLogger(__name__)

PROJECT_ASSETS_INDEX = "ProjectAssetsIndex"

_PROJECT_SK = "PROJECT"
_ASSET_SK = "ASSET"
_MEMBER_SK_PREFIX = "MEMBER#"


def _project_key(project_id: str) -> dict[str, str]:
    return {"PK": f"PROJECT#{project_id}", "SK": _PROJECT_SK}


def _asset_key(asset_id: str) -> dict[str, str]:
    return {"PK": f"ASSET#{asset_id}", "SK": _ASSET_SK}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _forms_output(forms_input: list[dict[str, str]] | None) -> list[dict[str, str]]:
    """Map DataZone ``formsInput`` entries to the ``formsOutput`` shape readers parse."""
    return [
        {
            "formName": form["formName"],
            "typeName": form.get("typeIdentifier", ""),
            "content": form.get("content", ""),
        }
        for form in forms_input or []
    ]


def _encode_token(last_evaluated_key: dict[str, Any] | None) -> str | None:
    if not last_evaluated_key:
        return None
    return base64.urlsafe_b64encode(json.dumps(last_evaluated_key).encode()).decode()


def _decode_token(token: str) -> dict[str, Any]:
    try:
        key = json.loads(base64.urlsafe_b64decode(token.encode()))
    except (binascii.Error, ValueError) as exc:
        raise MetadataStoreError("Invalid pagination token") from exc
    if not isinstance(key, dict):
        raise MetadataStoreError("Invalid pagination token")
    return key


def _error_message(exc: Exception) -> str:
    return exc.args[0] if exc.args else str(exc)


def _cancellation_codes(exc: ClientError) -> list[str]:
    """Per-item ``CancellationReasons`` codes of a TransactionCanceledException."""
    return [r.get("Code", "") for r in exc.response.get("CancellationReasons", [])]


class DynamoDBMetadataStoreClient(MetadataStoreClient):
    """MetadataStoreClient backed by a single DynamoDB table.

    Access control is plain IAM on the table: membership records are written
    and validated (the project must exist; a duplicate membership raises, as
    with SMUS) but reads and writes are not gated on them.

    Args:
        table_name: Metadata store table name.
        region_name: AWS region hosting the table.
    """

    def __init__(self, table_name: str, region_name: str) -> None:
        """Bind the client to the metadata store table.

        Args:
            table_name: Metadata store table name.
            region_name: AWS region hosting the table.
        """
        self._table_name = table_name
        self._dao = DynamoDBDAO(table_name, region=region_name)

    # ── Projects ──────────────────────────────────────────────────

    def create_project(self, *, name: str, description: str = "", project_profile_id: str = "") -> ProjectResult:
        """Create a project item with a generated id.

        Args:
            name: Display name for the new project.
            description: Optional project description.
            project_profile_id: Optional project profile id, stored as-is.

        Returns:
            The created project's id and name.

        Raises:
            MetadataStoreError: If the write fails.
        """
        project_id = uuid.uuid4().hex
        try:
            self._dao.put(
                {
                    **_project_key(project_id),
                    "entityType": "PROJECT",
                    "name": name,
                    "description": description,
                    "projectProfileId": project_profile_id,
                },
                condition=Attr("PK").not_exists(),
            )
        except Exception as exc:
            raise MetadataStoreError(f"Failed to create project: {_error_message(exc)}") from exc
        return ProjectResult(project_id=project_id, name=name)

    def delete_project(self, *, project_id: str, force: bool = False) -> None:
        """Delete a project and its memberships, plus its assets when *force* is set.

        The project item is deleted last so a retry after a partial failure
        still finds the project and finishes the job.

        Args:
            project_id: Identifier of the project to delete.
            force: Delete the project's assets too. Without it a project that
                still owns assets is refused, mirroring DataZone's deletion check.

        Raises:
            MetadataStoreNotFoundError: If the project does not exist.
            MetadataStoreError: If the project still owns assets and *force*
                is False, or a DynamoDB call fails.
        """
        try:
            if self._dao.get(_project_key(project_id)) is None:
                raise MetadataStoreNotFoundError(f"Project {project_id} not found")

            asset_keys = [
                {"PK": item["PK"], "SK": item["SK"]}
                for item in self._dao.query_all(
                    QueryParams(
                        key_condition="projectId = :pid",
                        expression_values={":pid": project_id},
                        index_name=PROJECT_ASSETS_INDEX,
                    ),
                    max_pages=1000,
                )
            ]
            if asset_keys and not force:
                raise MetadataStoreError(
                    f"Failed to delete project: project {project_id} still owns {len(asset_keys)} asset(s)"
                )

            member_keys = [
                {"PK": item["PK"], "SK": item["SK"]}
                for item in self._dao.query_all(
                    QueryParams(
                        key_condition="PK = :pk AND begins_with(SK, :member)",
                        expression_values={":pk": f"PROJECT#{project_id}", ":member": _MEMBER_SK_PREFIX},
                    ),
                )
            ]
            self._dao.batch_delete(asset_keys + member_keys)
            self._dao.delete(_project_key(project_id))
        except MetadataStoreError:
            raise
        except Exception as exc:
            raise MetadataStoreError(f"Failed to delete project: {_error_message(exc)}") from exc
        logger.info("Deleted project %s (%d assets, %d memberships)", project_id, len(asset_keys), len(member_keys))

    def get_project(self, *, project_id: str) -> ProjectResult:
        """Fetch a project by id.

        Args:
            project_id: Identifier of the project to fetch.

        Returns:
            The project's id and name.

        Raises:
            MetadataStoreNotFoundError: If the project does not exist.
            MetadataStoreError: If the read fails.
        """
        try:
            item = self._dao.get(_project_key(project_id))
        except Exception as exc:
            raise MetadataStoreError(f"Failed to get project: {_error_message(exc)}") from exc
        if item is None:
            raise MetadataStoreNotFoundError(f"Project {project_id} not found")
        return ProjectResult(project_id=project_id, name=item["name"])

    # ── Assets ────────────────────────────────────────────────────

    def create_asset(
        self,
        *,
        project_id: str,
        name: str,
        type_identifier: str,
        description: str = "",
        forms_input: list[dict[str, str]] | None = None,
    ) -> AssetResult:
        """Create a new asset in an existing project.

        Args:
            project_id: Owning project identifier.
            name: Asset name.
            type_identifier: Asset type identifier, stored as-is.
            description: Optional description.
            forms_input: Optional metadata form values to attach.

        Returns:
            The created asset's id, name, and owning project.

        Raises:
            MetadataStoreNotFoundError: If the project does not exist.
            MetadataStoreError: If the write fails.
        """
        asset_id = uuid.uuid4().hex
        now = _now()
        try:
            self._dao.transact_write(
                [
                    {
                        "ConditionCheck": {
                            "TableName": self._table_name,
                            "Key": _project_key(project_id),
                            "ConditionExpression": "attribute_exists(PK)",
                        }
                    },
                    {
                        "Put": {
                            "TableName": self._table_name,
                            "Item": {
                                **_asset_key(asset_id),
                                "entityType": "ASSET",
                                "assetId": asset_id,
                                "projectId": project_id,
                                "name": name,
                                "description": description,
                                "typeIdentifier": type_identifier,
                                "formsOutput": _forms_output(forms_input),
                                "revision": 1,
                                "createdAt": now,
                                "updatedAt": now,
                            },
                            "ConditionExpression": "attribute_not_exists(PK)",
                        }
                    },
                ]
            )
        except ClientError as exc:
            if _cancellation_codes(exc)[:1] == ["ConditionalCheckFailed"]:
                raise MetadataStoreNotFoundError(f"Project {project_id} not found") from exc
            raise MetadataStoreError(f"Failed to create asset: {_error_message(exc)}") from exc
        except Exception as exc:
            raise MetadataStoreError(f"Failed to create asset: {_error_message(exc)}") from exc
        return AssetResult(asset_id=asset_id, name=name, project_id=project_id)

    def create_asset_revision(
        self,
        *,
        asset_id: str,
        name: str,
        description: str = "",
        forms_input: list[dict[str, str]] | None = None,
    ) -> AssetResult:
        """Replace an asset's name/description/forms and bump its revision.

        Forms are merged by ``formName``: each given form replaces the stored
        form of the same name; stored forms not given are kept.

        Args:
            asset_id: Identifier of the asset to revise.
            name: Asset name for the revision.
            description: Description for the revision.
            forms_input: Optional metadata form values to attach.

        Returns:
            The revised asset's id, name, and owning project.

        Raises:
            MetadataStoreNotFoundError: If the asset does not exist.
            MetadataStoreError: If the write fails, including a concurrent
                revision of the same asset (optimistic lock on ``revision``).
        """
        try:
            item = self._dao.get(_asset_key(asset_id))
            if item is None:
                raise MetadataStoreNotFoundError(f"Asset {asset_id} not found")

            forms = {f["formName"]: f for f in item.get("formsOutput", [])}
            forms.update({f["formName"]: f for f in _forms_output(forms_input)})
            current_revision = int(item["revision"])
            self._dao.put(
                {
                    **item,
                    "name": name,
                    "description": description,
                    "formsOutput": list(forms.values()),
                    "revision": current_revision + 1,
                },
                condition=Attr("revision").eq(current_revision),
            )
        except MetadataStoreError:
            raise
        except Exception as exc:
            raise MetadataStoreError(f"Failed to create asset revision: {_error_message(exc)}") from exc
        return AssetResult(asset_id=asset_id, name=name, project_id=item["projectId"])

    def delete_asset(self, *, asset_id: str) -> None:
        """Delete an asset by id.

        Args:
            asset_id: Identifier of the asset to delete.

        Raises:
            MetadataStoreNotFoundError: If the asset does not exist.
            MetadataStoreError: If the delete fails.
        """
        try:
            if self._dao.get(_asset_key(asset_id), projection=["PK"]) is None:
                raise MetadataStoreNotFoundError(f"Asset {asset_id} not found")
            self._dao.delete(_asset_key(asset_id))
        except MetadataStoreError:
            raise
        except Exception as exc:
            raise MetadataStoreError(f"Failed to delete asset: {_error_message(exc)}") from exc

    def get_asset(self, *, asset_id: str) -> AssetResult:
        """Fetch an asset by id.

        Args:
            asset_id: Identifier of the asset to fetch.

        Returns:
            The asset's id, name, and owning project.

        Raises:
            MetadataStoreNotFoundError: If the asset does not exist.
            MetadataStoreError: If the read fails.
        """
        item = self._get_asset_item(asset_id, action="get asset")
        return AssetResult(asset_id=asset_id, name=item["name"], project_id=item["projectId"])

    def search_assets(
        self,
        *,
        project_id: str,
        search_text: str,
        max_results: int = 50,
        next_token: str | None = None,
        include_forms: bool = False,
        search_in_attributes: list[str] | None = None,
        filters: dict[str, Any] | None = None,
    ) -> SearchResult:
        """List a project's assets whose name starts with *search_text*.

        Every caller searches by an asset-name prefix (``DS#{sourceId}``) or a
        full asset name and re-checks the names itself, so prefix matching on
        the GSI sort key returns exactly the assets DataZone's token search
        would have to be filtered down to. Results are ordered by name.

        Args:
            project_id: Owning project to scope the search to.
            search_text: Asset-name prefix; empty lists every asset.
            max_results: Maximum results to return per page.
            next_token: Pagination cursor from a prior call.
            include_forms: Populate each result's ``forms_output``.
            search_in_attributes: Only ``["name"]`` is supported, which is
                what this backend always matches on.
            filters: Not supported.

        Returns:
            Matching assets plus the next pagination token, if any.

        Raises:
            MetadataStoreError: If an unsupported option is given or the query fails.
        """
        if filters:
            raise MetadataStoreError("Failed to search assets: filters are not supported by the DynamoDB backend")
        if search_in_attributes and set(search_in_attributes) != {"name"}:
            raise MetadataStoreError(
                f"Failed to search assets: only the 'name' attribute is searchable, got {search_in_attributes}"
            )

        key_condition = "projectId = :pid"
        values: dict[str, Any] = {":pid": project_id}
        if search_text:
            key_condition += " AND begins_with(#name, :prefix)"
            values[":prefix"] = search_text
        try:
            page = self._dao.query(
                QueryParams(
                    key_condition=key_condition,
                    expression_values=values,
                    expression_names={"#name": "name"} if search_text else None,
                    index_name=PROJECT_ASSETS_INDEX,
                    limit=max_results,
                    exclusive_start_key=_decode_token(next_token) if next_token else None,
                )
            )
        except MetadataStoreError:
            raise
        except Exception as exc:
            raise MetadataStoreError(f"Failed to search assets: {_error_message(exc)}") from exc

        items = [
            AssetResult(
                asset_id=item["assetId"],
                name=item["name"],
                project_id=item["projectId"],
                forms_output=item.get("formsOutput", []) if include_forms else None,
            )
            for item in page.items
        ]
        return SearchResult(items=items, next_token=_encode_token(page.last_evaluated_key))

    def find_asset_by_name(self, *, project_id: str, name: str, max_pages: int = 20) -> AssetResult | None:
        """Exact-name lookup on the GSI; *max_pages* is unused.

        Args:
            project_id: Owning project to scope the lookup to.
            name: Full asset name to match exactly.
            max_pages: Ignored; kept for interface compatibility.

        Returns:
            The asset whose name equals ``name``, or ``None``.

        Raises:
            MetadataStoreError: If the query fails.
        """
        try:
            page = self._dao.query(
                QueryParams(
                    key_condition="projectId = :pid AND #name = :name",
                    expression_values={":pid": project_id, ":name": name},
                    expression_names={"#name": "name"},
                    index_name=PROJECT_ASSETS_INDEX,
                    limit=1,
                )
            )
        except Exception as exc:
            raise MetadataStoreError(f"Failed to find asset by name: {_error_message(exc)}") from exc
        if not page.items:
            return None
        item = page.items[0]
        return AssetResult(asset_id=item["assetId"], name=item["name"], project_id=item["projectId"])

    def get_asset_forms(self, *, asset_id: str) -> dict[str, Any]:
        """Return the asset in the DataZone ``GetAsset`` response shape.

        Args:
            asset_id: Identifier of the asset to fetch.

        Returns:
            Dict with ``id``, ``name``, ``description``, ``owningProjectId``,
            ``typeIdentifier``, ``revision`` and ``formsOutput``.

        Raises:
            MetadataStoreNotFoundError: If the asset does not exist.
            MetadataStoreError: If the read fails.
        """
        item = self._get_asset_item(asset_id, action="get asset forms")
        return {
            "id": asset_id,
            "name": item["name"],
            "description": item.get("description", ""),
            "owningProjectId": item["projectId"],
            "typeIdentifier": item.get("typeIdentifier", ""),
            "revision": str(item["revision"]),
            "formsOutput": item.get("formsOutput", []),
        }

    def _get_asset_item(self, asset_id: str, *, action: str) -> dict[str, Any]:
        try:
            item = self._dao.get(_asset_key(asset_id))
        except Exception as exc:
            raise MetadataStoreError(f"Failed to {action}: {_error_message(exc)}") from exc
        if item is None:
            raise MetadataStoreNotFoundError(f"Asset {asset_id} not found")
        return item

    # ── User Profiles & Memberships ───────────────────────────────

    def create_project_membership(
        self, *, project_id: str, user_identifier: str, designation: str = "PROJECT_CONTRIBUTOR"
    ) -> None:
        """Record a project membership. Raises MetadataStoreError on failure.

        Written in one transaction with a check that the project exists, so a
        membership can never point at a missing project. Like the SMUS
        backend this is not idempotent: re-adding an existing member raises.

        Args:
            project_id: Project to add the member to.
            user_identifier: Member identity (an IAM role ARN for service roles).
            designation: Membership designation, e.g. ``PROJECT_CONTRIBUTOR``.

        Raises:
            MetadataStoreNotFoundError: If the project does not exist.
            MetadataStoreError: If the user is already a member or the write fails.
        """
        try:
            self._dao.transact_write(
                [
                    {
                        "ConditionCheck": {
                            "TableName": self._table_name,
                            "Key": _project_key(project_id),
                            "ConditionExpression": "attribute_exists(PK)",
                        }
                    },
                    {
                        "Put": {
                            "TableName": self._table_name,
                            "Item": {
                                "PK": f"PROJECT#{project_id}",
                                "SK": f"{_MEMBER_SK_PREFIX}{user_identifier}",
                                "entityType": "MEMBERSHIP",
                                "userIdentifier": user_identifier,
                                "designation": designation,
                                "createdAt": _now(),
                            },
                            "ConditionExpression": "attribute_not_exists(PK)",
                        }
                    },
                ]
            )
        except ClientError as exc:
            codes = _cancellation_codes(exc)
            if codes[:1] == ["ConditionalCheckFailed"]:
                raise MetadataStoreNotFoundError(f"Project {project_id} not found") from exc
            if codes[1:2] == ["ConditionalCheckFailed"]:
                raise MetadataStoreError(
                    f"Failed to create project membership: {user_identifier} is already a member of {project_id}"
                ) from exc
            raise MetadataStoreError(f"Failed to create project membership: {_error_message(exc)}") from exc
        except Exception as exc:
            raise MetadataStoreError(f"Failed to create project membership: {_error_message(exc)}") from exc
