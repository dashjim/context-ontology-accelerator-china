# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for coa_common.bedrock_access (BEDROCK_REGION, API key, model map).

The bearer-precedence tests build REAL botocore clients and capture the signed
request in a ``before-send`` hook, so they check what botocore actually sends
when IAM role credentials and a Bedrock API key are both present.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import boto3
import pytest
from botocore.exceptions import ClientError
from coa_common import bedrock_access
from coa_common.aws_user_agent import install_user_agent
from coa_common.bedrock import BedrockClient
from coa_common.bedrock_access import (
    BEARER_TOKEN_ENV,
    bedrock_region,
    ensure_bedrock_api_key,
    map_bedrock_model_id,
    prepare_bedrock_client,
)
from coa_common.embeddings import BedrockEmbedder
from coa_common.guardrail_screener import GuardrailScreener

pytestmark = pytest.mark.unit

SECRET_ARN = "arn:aws-cn:secretsmanager:cn-northwest-1:111122223333:secret:coa/bedrock-api-key-AbCdEf"
FAKE_KEY = "bedrock-api-key-test-value"

_ENV = (
    "BEDROCK_REGION",
    "BEDROCK_API_KEY_SECRET_ARN",
    BEARER_TOKEN_ENV,
    "BEDROCK_MODEL_MAP",
    "BEDROCK_MODEL_DEFAULT",
    "LLM_PROVIDER",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_REGION", "cn-northwest-1")


def _secrets_client(secret_string: str = FAKE_KEY) -> MagicMock:
    client = MagicMock()
    client.get_secret_value.return_value = {"SecretString": secret_string}
    return client


def _patch_boto3(**client_kwargs):
    """Patch boto3 as seen by bedrock_access only (the Bedrock clients stay real)."""
    fake = MagicMock()
    fake.client = MagicMock(**client_kwargs)
    return patch.object(bedrock_access, "boto3", fake)


class TestBedrockRegion:
    def test_falls_back_to_the_deployment_region(self):
        assert bedrock_region() == "cn-northwest-1"

    def test_explicit_region_beats_the_deployment_region(self):
        assert bedrock_region("eu-west-1") == "eu-west-1"

    def test_bedrock_region_wins_over_everything(self, monkeypatch):
        monkeypatch.setenv("BEDROCK_REGION", "us-east-1")
        assert bedrock_region() == "us-east-1"
        assert bedrock_region("cn-northwest-1") == "us-east-1"

    def test_empty_bedrock_region_is_ignored(self, monkeypatch):
        monkeypatch.setenv("BEDROCK_REGION", "")
        assert bedrock_region("eu-west-1") == "eu-west-1"


class TestEnsureBedrockApiKey:
    def test_noop_without_secret_arn(self):
        with _patch_boto3() as fake_boto3:
            ensure_bedrock_api_key()
        fake_boto3.client.assert_not_called()
        assert BEARER_TOKEN_ENV not in os.environ

    def test_fetches_once_from_the_secret_region(self, monkeypatch):
        monkeypatch.setenv("BEDROCK_API_KEY_SECRET_ARN", SECRET_ARN)
        client = _secrets_client()
        with _patch_boto3(return_value=client) as fake_boto3:
            ensure_bedrock_api_key()
            ensure_bedrock_api_key()
        fake_boto3.client.assert_called_once()
        assert fake_boto3.client.call_args.args == ("secretsmanager",)
        assert fake_boto3.client.call_args.kwargs["region_name"] == "cn-northwest-1"
        client.get_secret_value.assert_called_once_with(SecretId=SECRET_ARN)
        assert os.environ[BEARER_TOKEN_ENV] == FAKE_KEY

    def test_strips_whitespace_around_the_key(self, monkeypatch):
        monkeypatch.setenv("BEDROCK_API_KEY_SECRET_ARN", SECRET_ARN)
        with _patch_boto3(return_value=_secrets_client(f"  {FAKE_KEY}\n")):
            ensure_bedrock_api_key()
        assert os.environ[BEARER_TOKEN_ENV] == FAKE_KEY

    def test_existing_bearer_token_is_kept(self, monkeypatch):
        monkeypatch.setenv("BEDROCK_API_KEY_SECRET_ARN", SECRET_ARN)
        monkeypatch.setenv(BEARER_TOKEN_ENV, "already-set")
        with _patch_boto3() as fake_boto3:
            ensure_bedrock_api_key()
        fake_boto3.client.assert_not_called()
        assert os.environ[BEARER_TOKEN_ENV] == "already-set"

    def test_malformed_arn_raises(self, monkeypatch):
        monkeypatch.setenv("BEDROCK_API_KEY_SECRET_ARN", "coa/bedrock-api-key")
        with _patch_boto3() as fake_boto3, pytest.raises(ValueError, match="not a"):
            ensure_bedrock_api_key()
        fake_boto3.client.assert_not_called()

    def test_empty_secret_raises_without_setting_the_env(self, monkeypatch):
        monkeypatch.setenv("BEDROCK_API_KEY_SECRET_ARN", SECRET_ARN)
        with (
            _patch_boto3(return_value=_secrets_client("   ")),
            pytest.raises(ValueError, match="no Bedrock API key"),
        ):
            ensure_bedrock_api_key()
        assert BEARER_TOKEN_ENV not in os.environ

    def test_secrets_manager_error_propagates_and_is_retried(self, monkeypatch):
        monkeypatch.setenv("BEDROCK_API_KEY_SECRET_ARN", SECRET_ARN)
        failing = MagicMock()
        failing.get_secret_value.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "denied"}}, "GetSecretValue"
        )
        with _patch_boto3(side_effect=[failing, _secrets_client()]):
            with pytest.raises(ClientError):
                ensure_bedrock_api_key()
            assert BEARER_TOKEN_ENV not in os.environ
            ensure_bedrock_api_key()
        assert os.environ[BEARER_TOKEN_ENV] == FAKE_KEY

    def test_prepare_bedrock_client_loads_the_key_and_returns_the_region(self, monkeypatch):
        monkeypatch.setenv("BEDROCK_API_KEY_SECRET_ARN", SECRET_ARN)
        monkeypatch.setenv("BEDROCK_REGION", "us-east-1")
        with _patch_boto3(return_value=_secrets_client()):
            assert prepare_bedrock_client("cn-northwest-1") == "us-east-1"
        assert os.environ[BEARER_TOKEN_ENV] == FAKE_KEY


class TestMapBedrockModelId:
    SONNET = "us.anthropic.claude-sonnet-5"
    HAIKU = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

    def test_unset_is_identity(self):
        assert map_bedrock_model_id(self.SONNET) == self.SONNET

    def test_mapped_id(self, monkeypatch):
        monkeypatch.setenv("BEDROCK_MODEL_MAP", f'{{"{self.SONNET}": "openai.gpt-oss-120b-1:0"}}')
        assert map_bedrock_model_id(self.SONNET) == "openai.gpt-oss-120b-1:0"
        assert map_bedrock_model_id(self.HAIKU) == self.HAIKU

    def test_arn_is_looked_up_by_resource_id(self, monkeypatch):
        monkeypatch.setenv("BEDROCK_MODEL_MAP", f'{{"{self.HAIKU}": "openai.gpt-oss-20b-1:0"}}')
        arn = f"arn:aws-cn:bedrock:cn-northwest-1:111122223333:inference-profile/{self.HAIKU}"
        assert map_bedrock_model_id(arn) == "openai.gpt-oss-20b-1:0"

    def test_exact_arn_key_wins(self, monkeypatch):
        arn = f"arn:aws:bedrock:us-east-1:111122223333:inference-profile/{self.HAIKU}"
        monkeypatch.setenv("BEDROCK_MODEL_MAP", f'{{"{arn}": "a", "{self.HAIKU}": "b"}}')
        assert map_bedrock_model_id(arn) == "a"

    def test_default_applies_to_unmapped_anthropic_ids_only(self, monkeypatch):
        monkeypatch.setenv("BEDROCK_MODEL_MAP", f'{{"{self.HAIKU}": "openai.gpt-oss-20b-1:0"}}')
        monkeypatch.setenv("BEDROCK_MODEL_DEFAULT", "openai.gpt-oss-120b-1:0")
        assert map_bedrock_model_id(self.HAIKU) == "openai.gpt-oss-20b-1:0"
        assert map_bedrock_model_id("global.anthropic.claude-sonnet-4-6") == "openai.gpt-oss-120b-1:0"
        # Embedding and non-Anthropic ids pass through unless mapped explicitly.
        assert map_bedrock_model_id("us.cohere.embed-v4:0") == "us.cohere.embed-v4:0"
        assert map_bedrock_model_id("amazon.nova-pro-v1:0") == "amazon.nova-pro-v1:0"

    def test_default_without_map(self, monkeypatch):
        monkeypatch.setenv("BEDROCK_MODEL_DEFAULT", "openai.gpt-oss-120b-1:0")
        assert map_bedrock_model_id(self.SONNET) == "openai.gpt-oss-120b-1:0"

    @pytest.mark.parametrize("raw", ["not json", "[]", '{"a": 1}'])
    def test_invalid_map_raises(self, monkeypatch, raw):
        monkeypatch.setenv("BEDROCK_MODEL_MAP", raw)
        with pytest.raises(ValueError):
            map_bedrock_model_id(self.SONNET)

    def test_not_applied_on_the_openai_compatible_backend(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "openai_compatible")
        monkeypatch.setenv("BEDROCK_MODEL_DEFAULT", "openai.gpt-oss-120b-1:0")
        assert map_bedrock_model_id(self.SONNET) == self.SONNET

    def test_bedrock_client_and_embedder_use_mapped_ids(self, monkeypatch):
        monkeypatch.setenv("BEDROCK_MODEL_DEFAULT", "openai.gpt-oss-120b-1:0")
        monkeypatch.setenv("BEDROCK_MODEL_MAP", '{"us.cohere.embed-v4:0": "cohere.embed-v4:0"}')
        with patch("coa_common.bedrock.boto3"):
            assert BedrockClient(model_id=self.HAIKU)._model_id == "openai.gpt-oss-120b-1:0"
        assert BedrockEmbedder(model_id="us.cohere.embed-v4:0").model_id == "cohere.embed-v4:0"


# ── bearer token vs SigV4, on real botocore clients ─────────────────────────


class _Sent(Exception):
    """Raised from before-send to stop the request once it has been signed."""

    def __init__(self, request):
        super().__init__("request captured")
        self.request = request


def _capture(client) -> None:
    def _before_send(request, **_kwargs):
        raise _Sent(request)

    client.meta.events.register("before-send.bedrock-runtime", _before_send)


def _authorization(call) -> tuple[str, str]:
    """Run ``call`` and return the signed request's (Authorization header, URL)."""
    with pytest.raises(_Sent) as sent:
        call()
    request = sent.value.request
    auth = request.headers["Authorization"]
    return (auth.decode() if isinstance(auth, bytes) else auth), request.url


@pytest.fixture
def _role_credentials(monkeypatch):
    """IAM role credentials (as an ECS task / AgentCore runtime in AWS China has)."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAFAKEFAKEFAKEFAKE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "fake-secret-access-key")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "fake-session-token")
    monkeypatch.setenv("BEDROCK_REGION", "us-east-1")
    # Fresh default session, so these env credentials are the ones resolved.
    monkeypatch.setattr(boto3, "DEFAULT_SESSION", None)
    # A real process installs this at import (session-wide default client config),
    # so the precedence must hold with it in place.
    install_user_agent()


def _converse(client):
    return lambda: client.converse(
        modelId="openai.gpt-oss-120b-1:0", messages=[{"role": "user", "content": [{"text": "hi"}]}]
    )


def _invoke(client):
    return lambda: client.invoke_model(modelId="us.cohere.embed-v4:0", body=b"{}")


def _apply_guardrail(client):
    return lambda: client.apply_guardrail(
        guardrailIdentifier="gr", guardrailVersion="1", source="INPUT", content=[{"text": {"text": "x"}}]
    )


@pytest.mark.usefixtures("_role_credentials")
class TestBearerOverSigV4:
    @pytest.mark.parametrize(
        ("build", "call"),
        [
            (lambda: BedrockClient(region="cn-northwest-1")._client, _converse),
            (lambda: BedrockEmbedder(region="cn-northwest-1").client, _invoke),
            (lambda: GuardrailScreener(guardrail_id="gr", region="cn-northwest-1")._get_client(), _apply_guardrail),
        ],
        ids=["BedrockClient", "BedrockEmbedder", "GuardrailScreener"],
    )
    def test_api_key_from_secret_is_sent_as_bearer(self, monkeypatch, build, call):
        monkeypatch.setenv("BEDROCK_API_KEY_SECRET_ARN", SECRET_ARN)
        with _patch_boto3(return_value=_secrets_client()):
            client = build()
        _capture(client)
        auth, url = _authorization(call(client))
        assert auth == f"Bearer {FAKE_KEY}"
        assert url.startswith("https://bedrock-runtime.us-east-1.amazonaws.com/")

    def test_converse_stream_is_sent_as_bearer(self, monkeypatch):
        monkeypatch.setenv(BEARER_TOKEN_ENV, FAKE_KEY)
        client = BedrockClient()._client
        _capture(client)
        auth, _ = _authorization(
            lambda: client.converse_stream(
                modelId="openai.gpt-oss-120b-1:0", messages=[{"role": "user", "content": [{"text": "hi"}]}]
            )
        )
        assert auth == f"Bearer {FAKE_KEY}"

    def test_plain_boto3_session_client_also_prefers_bearer(self, monkeypatch):
        # Strands and LlamaIndex build their own boto3 sessions; the env token
        # reaches those clients the same way.
        monkeypatch.setenv(BEARER_TOKEN_ENV, FAKE_KEY)
        client = boto3.Session().client("bedrock-runtime", region_name=prepare_bedrock_client())
        _capture(client)
        auth, _ = _authorization(_converse(client))
        assert auth == f"Bearer {FAKE_KEY}"

    def test_without_a_key_the_role_credentials_sign_sigv4(self):
        # Control: same setup minus the key is plain SigV4 (unchanged default).
        client = BedrockClient()._client
        _capture(client)
        auth, _ = _authorization(_converse(client))
        assert auth.startswith("AWS4-HMAC-SHA256 Credential=AKIAFAKEFAKEFAKEFAKE/")
