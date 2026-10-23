// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * CDK context switches for services that are not offered in every partition
 * (AWS China has no Bedrock, DataZone/SMUS, AgentCore Memory or OpenSearch
 * Serverless).
 *
 * Each switch is a string context key. Unset, it defaults from the deploy
 * region: `cn-` regions get the China value, every other region keeps the
 * current behavior. An explicit `-c key=value` always wins.
 *
 * A China deployment must not depend on commercial-partition resources.
 */

import * as cdk from "aws-cdk-lib";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as iam from "aws-cdk-lib/aws-iam";
import { Construct } from "constructs";
import { TABLE_NAMES } from "@coa/shared";

import { VECTOR_COLLECTION_SUFFIX } from "../constants";
import { prefixed } from "../context";

/**
 * Whether `scope` deploys to an AWS China region.
 *
 * The stacks in bin/app.ts are environment-agnostic, so their region is a
 * token at synth time; fall back to the CLI's target region
 * (`CDK_DEFAULT_REGION`) so the China defaults also apply to a real deploy.
 * The same fallback applies to the App itself (bin/app.ts reads a switch
 * before any stack exists).
 */
export function isChinaRegion(scope: Construct): boolean {
  return deployRegion(scope)?.startsWith("cn-") ?? false;
}

/** The region `scope` deploys to, with the fallback of {@link isChinaRegion}. */
function deployRegion(scope: Construct): string | undefined {
  const region = cdk.Stage.isStage(scope)
    ? undefined
    : cdk.Stack.of(scope).region;
  return region === undefined || cdk.Token.isUnresolved(region)
    ? process.env.CDK_DEFAULT_REGION
    : region;
}

/**
 * Endpoint services AWS China names `cn.com.amazonaws.<region>.<name>`
 * (from `ec2 describe-vpc-endpoint-services` in cn-northwest-1); the rest
 * keep `com.amazonaws.<region>.<name>`.
 */
const CHINA_CN_PREFIXED_ENDPOINT_SERVICES = new Set([
  "athena",
  "ec2",
  "ecr.api",
  "ecr.dkr",
  "execute-api",
  "lambda",
  "monitoring",
  "redshift-data",
  "sqs",
  "states",
  "sts",
  "xray",
]);

/**
 * Interface endpoint service `name` (e.g. `"sts"`). CDK only applies the AWS
 * China `cn.com.amazonaws` prefix for a concrete stack region, which these
 * environment-agnostic stacks lack, so China regions get the full service name
 * spelled out. Elsewhere `service` (the CDK constant) is returned unchanged.
 */
export function interfaceEndpointService(
  scope: Construct,
  name: string,
  service: ec2.IInterfaceVpcEndpointService,
): ec2.IInterfaceVpcEndpointService {
  const region = deployRegion(scope);
  if (!region?.startsWith("cn-")) return service;
  const prefix = CHINA_CN_PREFIXED_ENDPOINT_SERVICES.has(name)
    ? "cn.com.amazonaws"
    : "com.amazonaws";
  return new ec2.InterfaceVpcEndpointService(
    `${prefix}.${region}.${name}`,
    443,
  );
}

function contextChoice<T extends string>(
  scope: Construct,
  key: string,
  allowed: readonly T[],
  defaultValue: T,
  chinaValue: T,
): T {
  const raw: unknown = scope.node.tryGetContext(key);
  const value = String(
    raw ?? (isChinaRegion(scope) ? chinaValue : defaultValue),
  )
    .trim()
    .toLowerCase();
  if (!(allowed as readonly string[]).includes(value)) {
    throw new Error(
      `${key} must be one of ${allowed.join(" | ")}, got: ${String(raw)}`,
    );
  }
  return value as T;
}

function contextString(scope: Construct, key: string): string | undefined {
  const raw: unknown = scope.node.tryGetContext(key);
  if (raw === undefined || raw === null) return undefined;
  const value = (typeof raw === "string" ? raw : JSON.stringify(raw)).trim();
  return value || undefined;
}

/**
 * Optional string context without a default, in cn- regions too. An explicit
 * empty value or `none` means unset.
 */
function contextOptional(scope: Construct, key: string): string | undefined {
  const raw: unknown = scope.node.tryGetContext(key);
  if (raw === undefined || raw === null) return undefined;
  const value = String(raw).trim();
  return value === "" || value.toLowerCase() === "none" ? undefined : value;
}

/** Context holding a JSON object (an object in cdk.json arrives as one). */
function contextJsonObject(scope: Construct, key: string): string | undefined {
  const value = contextString(scope, key);
  if (value === undefined) return undefined;
  let parsed: unknown;
  try {
    parsed = JSON.parse(value);
  } catch {
    parsed = undefined;
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error(`${key} must be a JSON object, got: ${value}`);
  }
  return value;
}

// ── AgentCore Memory ─────────────────────────────────────────────────

/**
 * `agentcore_memory_enabled` (default on; off in cn- regions). Any value other
 * than false/0/off/no means enabled.
 */
export function resolveAgentCoreMemoryEnabled(scope: Construct): boolean {
  const raw: unknown =
    scope.node.tryGetContext("agentcore_memory_enabled") ??
    (isChinaRegion(scope) ? "false" : "true");
  return !["false", "0", "off", "no"].includes(
    String(raw).trim().toLowerCase(),
  );
}

// ── CloudFront edge WAF ──────────────────────────────────────────────

/**
 * `edge_waf` (default on; off in cn- regions): auto-create the CLOUDFRONT-scope
 * WebACL of the web distribution in the `*-edge-waf` stack, which always
 * deploys to us-east-1 — outside the aws-cn partition, so a China deployment
 * cannot create it. Off, the distribution gets no WebACL unless
 * `cloudfront_web_acl_id` names one. Any value other than false/0/off/no means
 * enabled.
 */
export function resolveEdgeWafEnabled(scope: Construct): boolean {
  const raw: unknown =
    scope.node.tryGetContext("edge_waf") ??
    (isChinaRegion(scope) ? "false" : "true");
  return !["false", "0", "off", "no"].includes(
    String(raw).trim().toLowerCase(),
  );
}

// ── AgentCore Runtime platform version ───────────────────────────────

/**
 * `agentcore_platform_version` (unset; `V2` in cn- regions, whose AgentCore
 * Runtime requires it). An explicit empty value or `none` means unset.
 */
export function resolveAgentCorePlatformVersion(
  scope: Construct,
): string | undefined {
  const raw: unknown = scope.node.tryGetContext("agentcore_platform_version");
  if (raw === undefined || raw === null) {
    return isChinaRegion(scope) ? "V2" : undefined;
  }
  const value = String(raw).trim();
  return value === "" || value.toLowerCase() === "none" ? undefined : value;
}

/**
 * Set `PlatformVersion` on an AgentCore Runtime's CfnRuntime (aws-cdk-lib has
 * no prop for it). No-op when the switch is unset.
 */
export function applyAgentCorePlatformVersion(runtime: Construct): void {
  const version = resolveAgentCorePlatformVersion(runtime);
  if (version === undefined) return;
  (runtime.node.defaultChild as cdk.CfnResource).addPropertyOverride(
    "PlatformVersion",
    version,
  );
}

// ── LLM provider ─────────────────────────────────────────────────────

export type LlmProvider = "bedrock" | "openai_compatible";

/** `llm_provider`: `bedrock` (default) | `openai_compatible` (cn- default). */
export function resolveLlmProvider(scope: Construct): LlmProvider {
  return contextChoice<LlmProvider>(
    scope,
    "llm_provider",
    ["bedrock", "openai_compatible"],
    "bedrock",
    "openai_compatible",
  );
}

const SECRET_ARN_RE =
  /^arn:aws[a-z-]*:secretsmanager:[a-z0-9-]+:\d{12}:secret:[^*?]+$/;
const KMS_KEY_ARN_RE = /^arn:aws[a-z-]*:kms:[a-z0-9-]+:\d{12}:key\/[^*?]+$/;

/**
 * LLM-backend environment for an LLM consumer.
 *
 * On `bedrock`, the `BEDROCK_*` access settings of {@link bedrockAccessEnv}
 * (empty unless set, so commercial templates are unchanged). On
 * `openai_compatible`, `LLM_PROVIDER` / `OPENAI_COMPAT_*` from context
 * `openai_compat_base_url` (required), `openai_compat_api_key_secret_arn`,
 * `openai_compat_model_map` (JSON object), `openai_compat_chat_model` and
 * `openai_compat_embed_model`.
 */
export function llmProviderEnv(scope: Construct): Record<string, string> {
  if (resolveLlmProvider(scope) === "bedrock") return bedrockAccessEnv(scope);
  const baseUrl = contextString(scope, "openai_compat_base_url");
  if (!baseUrl) {
    throw new Error(
      "llm_provider=openai_compatible requires -c openai_compat_base_url=<https://host/v1>",
    );
  }
  const secretArn = secretArnContext(scope, "openai_compat_api_key_secret_arn");
  const modelMap = contextJsonObject(scope, "openai_compat_model_map");
  const chatModel = contextString(scope, "openai_compat_chat_model");
  const embedModel = contextString(scope, "openai_compat_embed_model");
  return {
    LLM_PROVIDER: "openai_compatible",
    OPENAI_COMPAT_BASE_URL: baseUrl,
    ...(secretArn && { OPENAI_COMPAT_API_KEY_SECRET_ARN: secretArn }),
    ...(modelMap && { OPENAI_COMPAT_MODEL_MAP: modelMap }),
    ...(chatModel && { OPENAI_COMPAT_CHAT_MODEL: chatModel }),
    ...(embedModel && { OPENAI_COMPAT_EMBED_MODEL: embedModel }),
  };
}

function secretArnContext(scope: Construct, key: string): string | undefined {
  const arn = contextString(scope, key);
  if (arn !== undefined && !SECRET_ARN_RE.test(arn)) {
    throw new Error(
      `${key} must be a full Secrets Manager secret ARN without wildcards, got: ${arn}`,
    );
  }
  return arn;
}

/**
 * Let `grantee` read the LLM API-key secret — `bedrock_api_key_secret_arn` on
 * `bedrock`, `openai_compat_api_key_secret_arn` on `openai_compatible`:
 * GetSecretValue on exactly that ARN, plus kms:Decrypt on the matching
 * `*_api_key_kms_key_arn` when the secret uses a customer-managed key. No-op
 * when no secret is configured.
 */
export function grantLlmProviderSecret(
  scope: Construct,
  grantee: iam.IGrantable,
): void {
  const bedrock = resolveLlmProvider(scope) === "bedrock";
  const secretArn = bedrock
    ? bedrockApiKeySecretArn(scope)
    : secretArnContext(scope, "openai_compat_api_key_secret_arn");
  if (!secretArn) return;
  grantee.grantPrincipal.addToPrincipalPolicy(
    new iam.PolicyStatement({
      actions: ["secretsmanager:GetSecretValue"],
      resources: [secretArn],
    }),
  );
  const kmsKey = bedrock
    ? "bedrock_api_key_kms_key_arn"
    : "openai_compat_api_key_kms_key_arn";
  const kmsKeyArn = contextString(scope, kmsKey);
  if (kmsKeyArn === undefined) return;
  if (!KMS_KEY_ARN_RE.test(kmsKeyArn)) {
    throw new Error(`${kmsKey} must be a full KMS key ARN, got: ${kmsKeyArn}`);
  }
  grantee.grantPrincipal.addToPrincipalPolicy(
    new iam.PolicyStatement({
      actions: ["kms:Decrypt"],
      resources: [kmsKeyArn],
    }),
  );
}

// ── Native Bedrock access ────────────────────────────────────────────
// Native Bedrock in another region, authenticated with a long-term Bedrock API
// key (see libs/common/src/coa_common/bedrock_access.py for the runtime side).
// Never a default: AWS China has no Bedrock, and a China deployment must not
// call Bedrock in a commercial region — it uses llm_provider=openai_compatible.

const REGION_RE = /^[a-z]{2}(-[a-z]+)+-\d+$/;

/**
 * `bedrock_region`: region of every native Bedrock client (unset = the
 * deployment region; no default in cn- regions either). An explicit empty
 * value or `none` means unset.
 */
export function resolveBedrockRegion(scope: Construct): string | undefined {
  const region = contextOptional(scope, "bedrock_region");
  if (region !== undefined && !REGION_RE.test(region)) {
    throw new Error(`bedrock_region must be an AWS region, got: ${region}`);
  }
  return region;
}

/**
 * `bedrock_model_default` (unset, in cn- regions too): the model invoked
 * instead of any Anthropic model id absent from `bedrock_model_map`.
 */
export function resolveBedrockModelDefault(
  scope: Construct,
): string | undefined {
  return contextOptional(scope, "bedrock_model_default");
}

/**
 * Whether llm_provider=bedrock calls Bedrock outside the deployment region:
 * `bedrock_region` is set and differs from it (or the deployment region is
 * unknown). Those calls authenticate with `bedrock_api_key_secret_arn` (role
 * credentials of the aws-cn partition cannot sign them), and Bedrock
 * Guardrails / the bedrock-runtime VPC endpoint of the deployment region do
 * not apply to them.
 */
export function isCrossRegionBedrock(scope: Construct): boolean {
  if (resolveLlmProvider(scope) !== "bedrock") return false;
  const region = resolveBedrockRegion(scope);
  return region !== undefined && region !== deployRegion(scope);
}

/**
 * Whether the deployment calls Bedrock in its own region — the only case that
 * deploys GuardrailStack and the bedrock-runtime VPC endpoint.
 */
export function usesInRegionBedrock(scope: Construct): boolean {
  return (
    resolveLlmProvider(scope) === "bedrock" && !isCrossRegionBedrock(scope)
  );
}

/**
 * `bedrock_api_key_secret_arn`: Secrets Manager secret holding a long-term
 * Bedrock API key. Required for cross-region Bedrock.
 */
function bedrockApiKeySecretArn(scope: Construct): string | undefined {
  const arn = secretArnContext(scope, "bedrock_api_key_secret_arn");
  if (arn === undefined && isCrossRegionBedrock(scope)) {
    throw new Error(
      `llm_provider=bedrock with bedrock_region=${resolveBedrockRegion(scope)} ` +
        `outside the deployment region ${deployRegion(scope) ?? "(unknown)"} requires ` +
        "-c bedrock_api_key_secret_arn=<full Secrets Manager secret ARN holding a Bedrock API key>",
    );
  }
  return arn;
}

/**
 * `BEDROCK_REGION` / `BEDROCK_API_KEY_SECRET_ARN` / `BEDROCK_MODEL_DEFAULT` /
 * `BEDROCK_MODEL_MAP` for a native Bedrock consumer, each only when its switch
 * (`bedrock_region`, `bedrock_api_key_secret_arn`, `bedrock_model_default`,
 * `bedrock_model_map` — a JSON object) has a value.
 */
function bedrockAccessEnv(scope: Construct): Record<string, string> {
  const region = resolveBedrockRegion(scope);
  const secretArn = bedrockApiKeySecretArn(scope);
  const modelDefault = resolveBedrockModelDefault(scope);
  const modelMap = contextJsonObject(scope, "bedrock_model_map");
  return {
    ...(region && { BEDROCK_REGION: region }),
    ...(secretArn && { BEDROCK_API_KEY_SECRET_ARN: secretArn }),
    ...(modelDefault && { BEDROCK_MODEL_DEFAULT: modelDefault }),
    ...(modelMap && { BEDROCK_MODEL_MAP: modelMap }),
  };
}

// ── Metadata store ───────────────────────────────────────────────────

export type MetadataStoreBackend = "smus" | "dynamodb";

/** `metadata_store_backend`: `smus` (default) | `dynamodb` (cn- default). */
export function resolveMetadataStoreBackend(
  scope: Construct,
): MetadataStoreBackend {
  return contextChoice<MetadataStoreBackend>(
    scope,
    "metadata_store_backend",
    ["smus", "dynamodb"],
    "smus",
    "dynamodb",
  );
}

/**
 * GSI on the metadata-store table; must match PROJECT_ASSETS_INDEX in
 * libs/common/src/coa_common/metadata_store/dynamodb.py.
 */
export const METADATA_STORE_PROJECT_ASSETS_INDEX = "ProjectAssetsIndex";

/**
 * The metadata-store table NamespaceStack creates, imported by name for a
 * consumer in another stack. `undefined` on `smus`.
 */
export function importMetadataStoreTable(
  scope: Construct,
): dynamodb.ITable | undefined {
  if (resolveMetadataStoreBackend(scope) !== "dynamodb") return undefined;
  return dynamodb.Table.fromTableAttributes(scope, "MetadataStoreTable", {
    tableName: prefixed(scope.node, TABLE_NAMES.METADATA_STORE),
    globalIndexes: [METADATA_STORE_PROJECT_ASSETS_INDEX],
  });
}

/** `METADATA_STORE_*` environment for a consumer; empty when `table` is unset. */
export function metadataStoreEnv(
  table: dynamodb.ITable | undefined,
): Record<string, string> {
  return table
    ? {
        METADATA_STORE_BACKEND: "dynamodb",
        METADATA_STORE_TABLE: table.tableName,
      }
    : {};
}

// ── Vector store ─────────────────────────────────────────────────────

export type VectorStore = "aoss" | "opensearch";

/**
 * `vector_store`: `aoss` (default; OpenSearch Serverless collection) |
 * `opensearch` (cn- default; a small managed OpenSearch Service domain inside
 * the VPC, see StorageStack). Services find either through the same
 * `/opensearch/*` SSM parameters and pick the SigV4 service from the endpoint
 * host.
 */
export function resolveVectorStore(scope: Construct): VectorStore {
  return contextChoice<VectorStore>(
    scope,
    "vector_store",
    ["aoss", "opensearch"],
    "aoss",
    "opensearch",
  );
}

/** Name of the managed vector-store domain (`vector_store=opensearch`). */
export function vectorStoreDomainName(scope: Construct): string {
  return prefixed(scope.node, VECTOR_COLLECTION_SUFFIX);
}

/** ARN of the managed vector-store domain, built from its fixed name. */
export function vectorStoreDomainArn(scope: Construct): string {
  return cdk.Stack.of(scope).formatArn({
    service: "es",
    resource: "domain",
    resourceName: vectorStoreDomainName(scope),
    arnFormat: cdk.ArnFormat.SLASH_RESOURCE_NAME,
  });
}

/** Data-plane actions for searching the domain (`_search` is a POST). */
export const VECTOR_STORE_READ_ACTIONS = [
  "es:ESHttpGet",
  "es:ESHttpHead",
  "es:ESHttpPost",
];

/** Data-plane actions for creating, writing and deleting indexes. */
export const VECTOR_STORE_WRITE_ACTIONS = ["es:ESHttp*"];

/**
 * Let `grantee` call the managed vector-store domain's data plane: `actions`
 * on `<domain-arn>/<pattern>` for each index-path pattern (default: every
 * path). The domain access policy only delegates to IAM in this account, so
 * this identity grant is what authorizes a caller.
 */
export function grantVectorStoreDomain(
  scope: Construct,
  grantee: iam.IGrantable,
  actions: readonly string[],
  indexPatterns: readonly string[] = ["*"],
): void {
  const domainArn = vectorStoreDomainArn(scope);
  grantee.grantPrincipal.addToPrincipalPolicy(
    new iam.PolicyStatement({
      actions: [...actions],
      resources: indexPatterns.map((p) => `${domainArn}/${p}`),
    }),
  );
}

// ── Container image build mirrors ────────────────────────────────────

const BUILD_URL_RE = /^https?:\/\/[^\s"'`$\\]+$/;
const BUILD_HOST_RE = /^[A-Za-z0-9.-]+(:\d+)?$/;

/**
 * Docker build args for the package-mirror ARGs of the COA Dockerfiles:
 *
 * - `build_pip_index_url` → `PIP_INDEX_URL` and `UV_DEFAULT_INDEX` (a PyPI
 *   simple index, e.g. a regional mirror)
 * - `build_torch_index_url` → `TORCH_INDEX_URL` (a PEP 503 index replacing the
 *   PyTorch CPU index the preprocessing image installs torch from)
 * - `build_torch_find_links` → `TORCH_FIND_LINKS` (a flat page of torch wheels
 *   used with pip `--find-links` instead of that index)
 * - `build_apt_mirror` → `APT_MIRROR` (a Debian mirror host serving `/debian`
 *   and `/debian-security` over HTTPS, replacing deb.debian.org)
 *
 * All unset by default, in cn- regions too: which mirrors are reachable depends
 * on the build host, not the deploy region. Returns `undefined` when none is
 * set, so the image assets and their hashes are unchanged; an image ignores
 * args its Dockerfile does not declare.
 */
export function imageBuildArgs(
  scope: Construct,
): Record<string, string> | undefined {
  const url = (key: string): string | undefined => {
    const value = contextString(scope, key);
    if (value !== undefined && !BUILD_URL_RE.test(value)) {
      throw new Error(`${key} must be an http(s) URL, got: ${value}`);
    }
    return value;
  };
  const pipIndex = url("build_pip_index_url");
  const torchIndex = url("build_torch_index_url");
  const torchFindLinks = url("build_torch_find_links");
  const aptMirror = contextString(scope, "build_apt_mirror");
  if (aptMirror !== undefined && !BUILD_HOST_RE.test(aptMirror)) {
    throw new Error(
      `build_apt_mirror must be a host name (no scheme or path), got: ${aptMirror}`,
    );
  }
  const args = {
    ...(pipIndex && { PIP_INDEX_URL: pipIndex, UV_DEFAULT_INDEX: pipIndex }),
    ...(torchIndex && { TORCH_INDEX_URL: torchIndex }),
    ...(torchFindLinks && { TORCH_FIND_LINKS: torchFindLinks }),
    ...(aptMirror && { APT_MIRROR: aptMirror }),
  };
  return Object.keys(args).length > 0 ? args : undefined;
}
