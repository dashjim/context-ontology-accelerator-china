# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the IdP-agnostic token authorizer factory."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

from coa_common.auth import CognitoTokenAuthorizer, OIDCTokenAuthorizer
from coa_common.auth.factory import build_token_authorizer, is_cognito_issuer


class TestIsCognitoIssuer:
    @pytest.mark.parametrize(
        "issuer",
        [
            "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_ABC123",
            "https://cognito-idp.eu-west-1.amazonaws.com/eu-west-1_XyZ789",
            "https://cognito-idp.cn-north-1.amazonaws.com.cn/cn-north-1_ABC",
        ],
    )
    def test_real_cognito_issuers_detected(self, issuer):
        assert is_cognito_issuer(issuer) is True

    @pytest.mark.parametrize(
        "issuer",
        [
            # Look-alike host that merely ends with the Cognito service name.
            "https://cognito-idp.us-east-1.amazonaws.com.evil.test/us-east-1_ABC123",
            # Cognito host embedded in the path rather than the host.
            "https://evil.test/cognito-idp.us-east-1.amazonaws.com/us-east-1_ABC123",
            # Cognito host smuggled into the query string.
            "https://evil.test/pool?x=cognito-idp.us-east-1.amazonaws.com/",
            # Userinfo trick: the real host is evil.test.
            "https://cognito-idp.us-east-1.amazonaws.com@evil.test/us-east-1_ABC",
            # Right shape but plaintext — must not be treated as Cognito.
            "http://cognito-idp.us-east-1.amazonaws.com/us-east-1_ABC123",
            # Generic OIDC issuers.
            "https://login.okta.com/oauth2/default",
            "https://login.microsoftonline.com/tenant-id/v2.0",
        ],
    )
    def test_non_cognito_issuers_rejected(self, issuer):
        assert is_cognito_issuer(issuer) is False


class TestBuildTokenAuthorizer:
    def test_cognito_issuer_builds_cognito_authorizer(self):
        authorizer = build_token_authorizer(
            issuer="https://cognito-idp.us-east-1.amazonaws.com/us-east-1_ABC123",
            region="us-east-1",
            client_id="my-client-id",
        )
        assert isinstance(authorizer, CognitoTokenAuthorizer)

    def test_cognito_issuer_in_another_region_keeps_the_pool_region(self):
        # An AWS China deployment trusting a commercial user pool: issuer and
        # JWKS must point at the pool's region, not the deploy region.
        issuer = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_ABC123"
        authorizer = build_token_authorizer(issuer=issuer, region="cn-northwest-1", client_id="c")
        assert isinstance(authorizer, CognitoTokenAuthorizer)
        assert authorizer._issuer == issuer
        assert authorizer._configured_jwks_uri == f"{issuer}/.well-known/jwks.json"

    def test_okta_issuer_builds_oidc_authorizer(self):
        authorizer = build_token_authorizer(
            issuer="https://login.okta.com/oauth2/default",
            region="us-east-1",
            jwks_uri="https://login.okta.com/oauth2/default/v1/keys",
        )
        assert isinstance(authorizer, OIDCTokenAuthorizer)

    def test_entra_id_issuer_builds_oidc_authorizer_with_custom_group_claim(self):
        # EntraID (Azure AD) commonly puts group memberships under "roles"
        # rather than the OIDC-conventional "groups" claim.
        authorizer = build_token_authorizer(
            issuer="https://login.microsoftonline.com/tenant-id/v2.0",
            region="us-east-1",
            jwks_uri="https://login.microsoftonline.com/tenant-id/discovery/v2.0/keys",
            group_claim_name="roles",
        )
        assert isinstance(authorizer, OIDCTokenAuthorizer)
        assert authorizer._group_claim_name == "roles"

    def test_oidc_falls_back_to_discovery_when_jwks_uri_omitted(self):
        authorizer = build_token_authorizer(
            issuer="https://auth0-tenant.us.auth0.com/",
            region="us-east-1",
        )
        assert isinstance(authorizer, OIDCTokenAuthorizer)
        # No explicit jwks_uri configured — falls back to OIDC discovery at
        # request time rather than raising eagerly.
        assert authorizer._configured_jwks_uri is None
