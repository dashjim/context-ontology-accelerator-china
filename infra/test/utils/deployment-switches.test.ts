// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as cdk from "aws-cdk-lib";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as iam from "aws-cdk-lib/aws-iam";
import { Template } from "aws-cdk-lib/assertions";
import { Construct } from "constructs";
import { DEFAULT_ENV, DEFAULT_RESOURCE_PREFIX } from "../../lib/constants";
import {
  applyAgentCorePlatformVersion,
  grantLlmProviderSecret,
  grantVectorStoreDomain,
  imageBuildArgs,
  importMetadataStoreTable,
  interfaceEndpointService,
  isChinaRegion,
  isCrossRegionBedrock,
  llmProviderEnv,
  metadataStoreEnv,
  resolveAgentCoreMemoryEnabled,
  resolveAgentCorePlatformVersion,
  resolveBedrockModelDefault,
  resolveBedrockRegion,
  resolveEdgeWafEnabled,
  resolveLlmProvider,
  resolveMetadataStoreBackend,
  resolveVectorStore,
  usesInRegionBedrock,
  VECTOR_STORE_READ_ACTIONS,
  vectorStoreDomainName,
} from "../../lib/utils/deployment-switches";

const TABLE_NAME = `${DEFAULT_RESOURCE_PREFIX}-${DEFAULT_ENV}-metadata-store`;
const SECRET_ARN =
  "arn:aws-cn:secretsmanager:cn-north-1:123456789012:secret:coa/llm-key-AbCdEf";
const BEDROCK_SECRET_ARN =
  "arn:aws-cn:secretsmanager:cn-north-1:123456789012:secret:coa/bedrock-key-AbCdEf";
const KMS_KEY_ARN =
  "arn:aws-cn:kms:cn-north-1:123456789012:key/1234abcd-12ab-34cd-56ef-1234567890ab";
/** Explicit opt-in to Bedrock in another region; never a cn- default. */
const CROSS_REGION_BEDROCK = {
  llm_provider: "bedrock",
  bedrock_region: "us-east-1",
  bedrock_api_key_secret_arn: BEDROCK_SECRET_ARN,
};

function stack(
  context: Record<string, unknown> = {},
  region: string | null = "us-east-1",
): cdk.Stack {
  const app = new cdk.App({ context });
  return new cdk.Stack(app, "S", {
    env: region ? { account: "123456789012", region } : undefined,
  });
}

describe("deployment switches", () => {
  const savedRegion = process.env.CDK_DEFAULT_REGION;
  afterEach(() => {
    if (savedRegion === undefined) delete process.env.CDK_DEFAULT_REGION;
    else process.env.CDK_DEFAULT_REGION = savedRegion;
  });

  it("keeps the current backends outside China", () => {
    const s = stack();
    expect(resolveLlmProvider(s)).toBe("bedrock");
    expect(resolveMetadataStoreBackend(s)).toBe("smus");
    expect(resolveAgentCoreMemoryEnabled(s)).toBe(true);
    expect(resolveVectorStore(s)).toBe("aoss");
    expect(resolveAgentCorePlatformVersion(s)).toBeUndefined();
    expect(resolveBedrockRegion(s)).toBeUndefined();
    expect(resolveBedrockModelDefault(s)).toBeUndefined();
    expect(isCrossRegionBedrock(s)).toBe(false);
    expect(usesInRegionBedrock(s)).toBe(true);
    expect(llmProviderEnv(s)).toEqual({});
    expect(importMetadataStoreTable(s)).toBeUndefined();
    expect(metadataStoreEnv(undefined)).toEqual({});
  });

  it("defaults to the China backends in a cn- region", () => {
    const s = stack(
      { openai_compat_base_url: "https://llm.example.cn/v1" },
      "cn-north-1",
    );
    expect(resolveLlmProvider(s)).toBe("openai_compatible");
    expect(resolveBedrockRegion(s)).toBeUndefined();
    expect(resolveBedrockModelDefault(s)).toBeUndefined();
    expect(isCrossRegionBedrock(s)).toBe(false);
    expect(usesInRegionBedrock(s)).toBe(false);
    expect(llmProviderEnv(s)).toEqual({
      LLM_PROVIDER: "openai_compatible",
      OPENAI_COMPAT_BASE_URL: "https://llm.example.cn/v1",
    });
    expect(resolveMetadataStoreBackend(s)).toBe("dynamodb");
    expect(resolveAgentCoreMemoryEnabled(s)).toBe(false);
    expect(resolveVectorStore(s)).toBe("opensearch");
    expect(resolveAgentCorePlatformVersion(s)).toBe("V2");
  });

  it("uses CDK_DEFAULT_REGION for an environment-agnostic stack", () => {
    process.env.CDK_DEFAULT_REGION = "cn-northwest-1";
    expect(isChinaRegion(stack({}, null))).toBe(true);
    process.env.CDK_DEFAULT_REGION = "us-west-2";
    expect(isChinaRegion(stack({}, null))).toBe(false);
  });

  it("spells out China endpoint service names for an environment-agnostic stack", () => {
    const sts = ec2.InterfaceVpcEndpointAwsService.STS;
    process.env.CDK_DEFAULT_REGION = "cn-northwest-1";
    const s = stack({}, null);
    expect(interfaceEndpointService(s, "sts", sts).name).toBe(
      "cn.com.amazonaws.cn-northwest-1.sts",
    );
    expect(
      interfaceEndpointService(
        s,
        "logs",
        ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS,
      ).name,
    ).toBe("com.amazonaws.cn-northwest-1.logs");
    process.env.CDK_DEFAULT_REGION = "us-east-1";
    expect(interfaceEndpointService(stack({}, null), "sts", sts)).toBe(sts);
  });

  it("resolves a switch on the App itself (before any stack exists)", () => {
    process.env.CDK_DEFAULT_REGION = "cn-north-1";
    expect(resolveVectorStore(new cdk.App())).toBe("opensearch");
    expect(
      resolveVectorStore(new cdk.App({ context: { vector_store: "aoss" } })),
    ).toBe("aoss");
    delete process.env.CDK_DEFAULT_REGION;
    expect(resolveVectorStore(new cdk.App())).toBe("aoss");
  });

  it("lets explicit context win over the region default", () => {
    const cn = stack(
      {
        llm_provider: "bedrock",
        metadata_store_backend: "smus",
        vector_store: "aoss",
      },
      "cn-north-1",
    );
    expect(resolveLlmProvider(cn)).toBe("bedrock");
    expect(resolveMetadataStoreBackend(cn)).toBe("smus");
    expect(resolveVectorStore(cn)).toBe("aoss");
    const us = stack({
      llm_provider: " OpenAI_Compatible ",
      metadata_store_backend: "dynamodb",
      vector_store: "OpenSearch",
    });
    expect(resolveLlmProvider(us)).toBe("openai_compatible");
    expect(resolveMetadataStoreBackend(us)).toBe("dynamodb");
    expect(resolveVectorStore(us)).toBe("opensearch");
  });

  it.each([
    ["llm_provider", "openai", resolveLlmProvider],
    ["metadata_store_backend", "datazone", resolveMetadataStoreBackend],
    ["vector_store", "es", resolveVectorStore],
  ])("rejects an unknown %s", (key, value, resolve) => {
    expect(() => resolve(stack({ [key]: value }))).toThrow(key);
  });

  it("fails synth when openai_compatible has no base URL", () => {
    expect(() =>
      llmProviderEnv(stack({ llm_provider: "openai_compatible" })),
    ).toThrow(/openai_compat_base_url/);
  });

  it("emits every configured OPENAI_COMPAT_* variable", () => {
    const s = stack({
      llm_provider: "openai_compatible",
      openai_compat_base_url: "https://llm.example.cn/v1",
      openai_compat_api_key_secret_arn: SECRET_ARN,
      // An object from cdk.json is passed through as JSON.
      openai_compat_model_map: { "us.amazon.nova-pro-v1:0": "qwen-max" },
      openai_compat_chat_model: "qwen-plus",
      openai_compat_embed_model: "text-embedding-v3",
    });
    expect(llmProviderEnv(s)).toEqual({
      LLM_PROVIDER: "openai_compatible",
      OPENAI_COMPAT_BASE_URL: "https://llm.example.cn/v1",
      OPENAI_COMPAT_API_KEY_SECRET_ARN: SECRET_ARN,
      OPENAI_COMPAT_MODEL_MAP: '{"us.amazon.nova-pro-v1:0":"qwen-max"}',
      OPENAI_COMPAT_CHAT_MODEL: "qwen-plus",
      OPENAI_COMPAT_EMBED_MODEL: "text-embedding-v3",
    });
  });

  it.each([
    ["openai_compat_model_map", "[1]"],
    ["openai_compat_api_key_secret_arn", "*"],
    [
      "openai_compat_api_key_secret_arn",
      "arn:aws-cn:secretsmanager:cn-north-1:123456789012:secret:*",
    ],
  ])("rejects %s=%s", (key, value) => {
    const s = stack({
      llm_provider: "openai_compatible",
      openai_compat_base_url: "https://llm.example.cn/v1",
      [key]: value,
    });
    expect(() => llmProviderEnv(s)).toThrow(key);
  });

  it("grants GetSecretValue (and kms:Decrypt) on exactly the configured ARNs", () => {
    const kmsKeyArn =
      "arn:aws-cn:kms:cn-north-1:123456789012:key/1234abcd-12ab-34cd-56ef-1234567890ab";
    const s = stack({
      llm_provider: "openai_compatible",
      openai_compat_base_url: "https://llm.example.cn/v1",
      openai_compat_api_key_secret_arn: SECRET_ARN,
      openai_compat_api_key_kms_key_arn: kmsKeyArn,
    });
    const role = new iam.Role(s, "R", {
      assumedBy: new iam.ServicePrincipal("lambda.amazonaws.com"),
    });
    grantLlmProviderSecret(s, role);
    Template.fromStack(s).hasResourceProperties("AWS::IAM::Policy", {
      PolicyDocument: {
        Statement: [
          {
            Action: "secretsmanager:GetSecretValue",
            Effect: "Allow",
            Resource: SECRET_ARN,
          },
          { Action: "kms:Decrypt", Effect: "Allow", Resource: kmsKeyArn },
        ],
      },
    });
  });

  it("grants nothing on bedrock", () => {
    const s = stack({ openai_compat_api_key_secret_arn: SECRET_ARN });
    const role = new iam.Role(s, "R", {
      assumedBy: new iam.ServicePrincipal("lambda.amazonaws.com"),
    });
    grantLlmProviderSecret(s, role);
    Template.fromStack(s).resourceCountIs("AWS::IAM::Policy", 0);
  });

  it("fails synth on cross-region Bedrock without an API-key secret", () => {
    const role = (s: cdk.Stack) =>
      new iam.Role(s, "R", {
        assumedBy: new iam.ServicePrincipal("lambda.amazonaws.com"),
      });
    const cn = stack(
      { llm_provider: "bedrock", bedrock_region: "us-east-1" },
      "cn-north-1",
    );
    expect(() => llmProviderEnv(cn)).toThrow(/bedrock_api_key_secret_arn/);
    expect(() => grantLlmProviderSecret(cn, role(cn))).toThrow(
      /bedrock_api_key_secret_arn/,
    );
    // Any bedrock_region other than the deployment region needs the key.
    const us = stack({ bedrock_region: "us-west-2" });
    expect(isCrossRegionBedrock(us)).toBe(true);
    expect(() => llmProviderEnv(us)).toThrow(/bedrock_api_key_secret_arn/);
    // So does an unknown deployment region.
    delete process.env.CDK_DEFAULT_REGION;
    expect(
      isCrossRegionBedrock(stack({ bedrock_region: "us-east-1" }, null)),
    ).toBe(true);
  });

  it("treats bedrock_region equal to the deployment region as in-region", () => {
    const s = stack({ bedrock_region: "us-east-1" });
    expect(isCrossRegionBedrock(s)).toBe(false);
    expect(usesInRegionBedrock(s)).toBe(true);
    expect(llmProviderEnv(s)).toEqual({ BEDROCK_REGION: "us-east-1" });
    // llm_provider=bedrock in a cn- region gets no bedrock_region or
    // bedrock_model_default: never cross-partition Bedrock by default.
    const cn = stack({ llm_provider: "bedrock" }, "cn-north-1");
    expect(isCrossRegionBedrock(cn)).toBe(false);
    expect(llmProviderEnv(cn)).toEqual({});
  });

  it("applies the China LLM default to an environment-agnostic stack", () => {
    process.env.CDK_DEFAULT_REGION = "cn-northwest-1";
    const s = stack(
      { openai_compat_base_url: "https://llm.example.cn/v1" },
      null,
    );
    expect(isCrossRegionBedrock(s)).toBe(false);
    expect(llmProviderEnv(s)).toEqual({
      LLM_PROVIDER: "openai_compatible",
      OPENAI_COMPAT_BASE_URL: "https://llm.example.cn/v1",
    });
  });

  it("emits every configured BEDROCK_* variable", () => {
    const s = stack(
      {
        llm_provider: "bedrock",
        bedrock_region: "us-west-2",
        bedrock_api_key_secret_arn: BEDROCK_SECRET_ARN,
        bedrock_model_default: "openai.gpt-oss-20b-1:0",
        // An object from cdk.json is passed through as JSON.
        bedrock_model_map: {
          "us.anthropic.claude-sonnet-5-v1:0": "openai.gpt-oss-120b-1:0",
        },
      },
      "cn-north-1",
    );
    expect(llmProviderEnv(s)).toEqual({
      BEDROCK_REGION: "us-west-2",
      BEDROCK_API_KEY_SECRET_ARN: BEDROCK_SECRET_ARN,
      BEDROCK_MODEL_DEFAULT: "openai.gpt-oss-20b-1:0",
      BEDROCK_MODEL_MAP:
        '{"us.anthropic.claude-sonnet-5-v1:0":"openai.gpt-oss-120b-1:0"}',
    });
    // bedrock_model_default=none keeps Anthropic model ids as they are.
    expect(
      llmProviderEnv(
        stack(
          { ...CROSS_REGION_BEDROCK, bedrock_model_default: "none" },
          "cn-north-1",
        ),
      ),
    ).not.toHaveProperty("BEDROCK_MODEL_DEFAULT");
  });

  it.each([
    ["bedrock_model_map", "[1]"],
    ["bedrock_model_map", "not json"],
    ["bedrock_api_key_secret_arn", "*"],
    [
      "bedrock_api_key_secret_arn",
      "arn:aws-cn:secretsmanager:cn-north-1:123456789012:secret:*",
    ],
    ["bedrock_region", "US East"],
  ])("rejects %s=%s", (key, value) => {
    const s = stack({ ...CROSS_REGION_BEDROCK, [key]: value }, "cn-north-1");
    expect(() => llmProviderEnv(s)).toThrow(key);
  });

  it("grants GetSecretValue (and kms:Decrypt) on exactly the Bedrock API-key ARNs", () => {
    const s = stack(
      {
        ...CROSS_REGION_BEDROCK,
        bedrock_api_key_kms_key_arn: KMS_KEY_ARN,
        // Ignored on bedrock.
        openai_compat_api_key_secret_arn: SECRET_ARN,
      },
      "cn-north-1",
    );
    const role = new iam.Role(s, "R", {
      assumedBy: new iam.ServicePrincipal("lambda.amazonaws.com"),
    });
    grantLlmProviderSecret(s, role);
    const statements = Object.values(
      Template.fromStack(s).findResources("AWS::IAM::Policy"),
    )[0].Properties.PolicyDocument.Statement;
    expect(statements).toEqual([
      {
        Action: "secretsmanager:GetSecretValue",
        Effect: "Allow",
        Resource: BEDROCK_SECRET_ARN,
      },
      { Action: "kms:Decrypt", Effect: "Allow", Resource: KMS_KEY_ARN },
    ]);
  });

  it("rejects a malformed bedrock_api_key_kms_key_arn", () => {
    const s = stack(
      {
        ...CROSS_REGION_BEDROCK,
        bedrock_api_key_kms_key_arn: "alias/coa",
      },
      "cn-north-1",
    );
    const role = new iam.Role(s, "R", {
      assumedBy: new iam.ServicePrincipal("lambda.amazonaws.com"),
    });
    expect(() => grantLlmProviderSecret(s, role)).toThrow(
      "bedrock_api_key_kms_key_arn",
    );
  });

  it("ignores the Bedrock switches on openai_compatible", () => {
    const s = stack(
      {
        llm_provider: "openai_compatible",
        openai_compat_base_url: "https://llm.example.cn/v1",
        bedrock_api_key_secret_arn: BEDROCK_SECRET_ARN,
      },
      "cn-north-1",
    );
    expect(isCrossRegionBedrock(s)).toBe(false);
    expect(usesInRegionBedrock(s)).toBe(false);
    expect(llmProviderEnv(s)).toEqual({
      LLM_PROVIDER: "openai_compatible",
      OPENAI_COMPAT_BASE_URL: "https://llm.example.cn/v1",
    });
    const role = new iam.Role(s, "R", {
      assumedBy: new iam.ServicePrincipal("lambda.amazonaws.com"),
    });
    grantLlmProviderSecret(s, role);
    Template.fromStack(s).resourceCountIs("AWS::IAM::Policy", 0);
  });

  it("imports the metadata-store table with its GSI on dynamodb", () => {
    const s = stack({ metadata_store_backend: "dynamodb" });
    const table = importMetadataStoreTable(s)!;
    expect(metadataStoreEnv(table)).toEqual({
      METADATA_STORE_BACKEND: "dynamodb",
      METADATA_STORE_TABLE: TABLE_NAME,
    });
    const role = new iam.Role(s, "R", {
      assumedBy: new iam.ServicePrincipal("lambda.amazonaws.com"),
    });
    table.grantReadData(role);
    const json = JSON.stringify(Template.fromStack(s).toJSON());
    expect(json).toContain(`table/${TABLE_NAME}/index/*`);
  });
  it("grants domain data-plane access scoped to its index paths", () => {
    const s = stack({}, "cn-north-1");
    const role = new iam.Role(s, "R", {
      assumedBy: new iam.ServicePrincipal("lambda.amazonaws.com"),
    });
    grantVectorStoreDomain(s, role, VECTOR_STORE_READ_ACTIONS);
    grantVectorStoreDomain(s, role, ["es:ESHttpDelete"], ["chunk_*"]);
    const domain = `:es:cn-north-1:123456789012:domain/${vectorStoreDomainName(s)}`;
    expect(vectorStoreDomainName(s)).toBe(
      `${DEFAULT_RESOURCE_PREFIX}-${DEFAULT_ENV}-vector-store`,
    );
    const statements = Object.values(
      Template.fromStack(s).findResources("AWS::IAM::Policy"),
    )[0].Properties.PolicyDocument.Statement;
    expect(statements).toHaveLength(2);
    expect(statements[0].Action).toEqual(VECTOR_STORE_READ_ACTIONS);
    expect(JSON.stringify(statements[0].Resource)).toContain(`${domain}/*"`);
    expect(statements[1].Action).toBe("es:ESHttpDelete");
    expect(JSON.stringify(statements[1].Resource)).toContain(
      `${domain}/chunk_*"`,
    );
  });

  it.each([
    [{}, "us-east-1", undefined],
    [{}, "cn-north-1", "V2"],
    [{ agentcore_platform_version: "none" }, "cn-north-1", undefined],
    [{ agentcore_platform_version: "" }, "cn-north-1", undefined],
    [{ agentcore_platform_version: "V2" }, "us-east-1", "V2"],
  ])(
    "agentcore_platform_version %j in %s -> %s",
    (context, region, expected) => {
      const s = stack(context, region);
      expect(resolveAgentCorePlatformVersion(s)).toBe(expected);
      // Stand-in for the L2 Runtime: the CfnRuntime is its "Resource" child.
      const runtime = new Construct(s, "Runtime");
      new cdk.CfnResource(runtime, "Resource", {
        type: "AWS::BedrockAgentCore::Runtime",
        properties: { AgentRuntimeName: "r" },
      });
      applyAgentCorePlatformVersion(runtime);
      const props = Object.values(
        Template.fromStack(s).findResources("AWS::BedrockAgentCore::Runtime"),
      )[0].Properties;
      if (expected) expect(props.PlatformVersion).toBe(expected);
      else expect(props).not.toHaveProperty("PlatformVersion");
    },
  );

  it("adds no image build args unless a build_* mirror is set, in China too", () => {
    expect(imageBuildArgs(stack())).toBeUndefined();
    expect(imageBuildArgs(stack({}, "cn-northwest-1"))).toBeUndefined();
    expect(imageBuildArgs(stack({ build_apt_mirror: " " }))).toBeUndefined();
  });

  it("maps every build_* mirror to its Dockerfile build arg", () => {
    const s = stack({
      build_pip_index_url: "https://pypi.example.cn/simple",
      build_torch_index_url: "https://torch.example.cn/whl/cpu",
      build_torch_find_links: "https://torch.example.cn/wheels/cpu/",
      build_apt_mirror: "debian.example.cn",
    });
    expect(imageBuildArgs(s)).toEqual({
      PIP_INDEX_URL: "https://pypi.example.cn/simple",
      UV_DEFAULT_INDEX: "https://pypi.example.cn/simple",
      TORCH_INDEX_URL: "https://torch.example.cn/whl/cpu",
      TORCH_FIND_LINKS: "https://torch.example.cn/wheels/cpu/",
      APT_MIRROR: "debian.example.cn",
    });
    expect(imageBuildArgs(stack({ build_apt_mirror: "m.example.cn" }))).toEqual(
      { APT_MIRROR: "m.example.cn" },
    );
  });

  it.each([
    [{ build_pip_index_url: "pypi.example.cn/simple" }, /http\(s\) URL/],
    [{ build_torch_find_links: "https://x.cn/a b" }, /http\(s\) URL/],
    [{ build_apt_mirror: "https://debian.example.cn" }, /host name/],
    [{ build_apt_mirror: "debian.example.cn/debian" }, /host name/],
  ])("rejects a malformed build mirror %j", (context, message) => {
    expect(() => imageBuildArgs(stack(context))).toThrow(message);
  });

  it.each([
    [{}, "us-east-1", true],
    [{}, "cn-northwest-1", false],
    [{ edge_waf: "true" }, "cn-northwest-1", true],
    [{ edge_waf: false }, "us-east-1", false],
    [{ edge_waf: "off" }, "us-east-1", false],
  ])("edge_waf %j in %s -> %s", (context, region, expected) => {
    expect(resolveEdgeWafEnabled(stack(context, region))).toBe(expected);
  });
});
