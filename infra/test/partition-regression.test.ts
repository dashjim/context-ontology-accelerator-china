// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Regression: no synthesized template may carry a literal `arn:aws:` ARN.
 *
 * A hardcoded `aws` partition synthesizes cleanly and deploys fine in the
 * commercial partition, then fails in `aws-cn` (cn-north-1 / cn-northwest-1)
 * — usually as a silent IAM AccessDenied, because a policy naming the wrong
 * partition matches nothing. Every ARN must instead resolve its partition from
 * `AWS::Partition` (`Aws.PARTITION` / `Stack.partition` / `formatArn`).
 *
 * The stacks are synthesized in cn-north-1 with China-shaped inputs, so any
 * `arn:aws:` left in a template came from the code under test, not from a
 * test fixture. They also take the China defaults of the deployment switches
 * (llm_provider=openai_compatible with a domestic provider,
 * metadata_store_backend=dynamodb, vector_store=opensearch, AgentCore Memory
 * off), which is asserted below — including that nothing reaches into the
 * commercial partition. An explicit cross-region Bedrock opt-in is covered
 * separately.
 */
import * as cdk from "aws-cdk-lib";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as ec2 from "aws-cdk-lib/aws-ec2";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as s3 from "aws-cdk-lib/aws-s3";
import { Match, Template } from "aws-cdk-lib/assertions";
import { NetworkStack } from "../lib/stacks/foundation/network-stack";
import { StorageStack } from "../lib/stacks/foundation/storage-stack";
import { IdpAuthenticationStack } from "../lib/stacks/foundation/idp-authentication-stack";
import { WebStack } from "../lib/stacks/foundation/web-stack";
import { SourcesStack } from "../lib/stacks/services/sources-stack";
import { MetricServiceStack } from "../lib/stacks/services/metric-service-stack";
import { NamespaceStack } from "../lib/stacks/services/namespace-stack";
import { ServeStack } from "../lib/stacks/services/serve-stack";
import { McpStack } from "../lib/stacks/services/mcp-stack";
import { ApiStack } from "../lib/stacks/services/api-stack";
import { OntologyStack } from "../lib/stacks/services/ontology-stack";
import { VkgStack } from "../lib/stacks/services/vkg-stack";
import { NamespaceDeletionPipeline } from "../lib/stacks/services/namespace-deletion-pipeline";
import { DEFAULT_RESOURCE_PREFIX, DEFAULT_ENV } from "../lib/constants";
import {
  resolveEdgeWafEnabled,
  usesInRegionBedrock,
} from "../lib/utils/deployment-switches";

jest.mock("../lib/utils/python-bundling", () => ({
  bundlePython: () =>
    lambda.Code.fromInline("def handler(event, context): pass"),
}));

const CN_ENV = { account: "123456789012", region: "cn-north-1" };
const ECR_HOST = "123456789012.dkr.ecr.cn-north-1.amazonaws.com.cn";
const PREFIX = `${DEFAULT_RESOURCE_PREFIX}-${DEFAULT_ENV}`;
const BEDROCK_SECRET_ARN =
  "arn:aws-cn:secretsmanager:cn-north-1:123456789012:secret:coa/bedrock-api-key-AbCdEf";
const OPENAI_COMPAT_BASE_URL = "https://api.siliconflow.cn/v1";
const OPENAI_COMPAT_SECRET_ARN =
  "arn:aws-cn:secretsmanager:cn-north-1:123456789012:secret:coa/siliconflow-api-key-AbCdEf";
const OPENAI_COMPAT_ENV = {
  LLM_PROVIDER: "openai_compatible",
  OPENAI_COMPAT_BASE_URL: OPENAI_COMPAT_BASE_URL,
  OPENAI_COMPAT_API_KEY_SECRET_ARN: OPENAI_COMPAT_SECRET_ARN,
  OPENAI_COMPAT_CHAT_MODEL: "deepseek-ai/DeepSeek-V3.2",
  OPENAI_COMPAT_EMBED_MODEL: "Qwen/Qwen3-Embedding-8B",
};
/** Explicit opt-in to native Bedrock in us-east-1; never a cn- default. */
const CROSS_REGION_BEDROCK_CONTEXT = {
  llm_provider: "bedrock",
  bedrock_region: "us-east-1",
  bedrock_api_key_secret_arn: BEDROCK_SECRET_ARN,
};

const CN_CONTEXT = {
  resource_prefix: DEFAULT_RESOURCE_PREFIX,
  env: DEFAULT_ENV,
  ecr_repository_arn:
    "arn:aws-cn:ecr:cn-north-1:123456789012:repository/coa-test",
  ecr_repository_name: "coa-test",
  // A real China ECR host: the AgentCore L2's own URI check rejects
  // `.amazonaws.com.cn`, so this also proves agentRuntimeArtifactFromImageUri
  // gets the Serve/MCP runtimes past it.
  context_manager_image_uri: `${ECR_HOST}/coa-dev:latest`,
  ontology_engine_image_uri: `${ECR_HOST}/coa-test:ontology-engine-test`,
  sources_db_enrichment_image_uri: `${ECR_HOST}/coa-test:db-enrichment-test`,
  sources_preprocessing_image_uri: `${ECR_HOST}/coa-test:preprocessing-test`,
  sources_kg_build_image_uri: `${ECR_HOST}/coa-test:kg-build-test`,
  // cn- regions default to llm_provider=openai_compatible (SiliconFlow here).
  openai_compat_base_url: OPENAI_COMPAT_BASE_URL,
  openai_compat_api_key_secret_arn: OPENAI_COMPAT_SECRET_ARN,
  openai_compat_chat_model: OPENAI_COMPAT_ENV.OPENAI_COMPAT_CHAT_MODEL,
  openai_compat_embed_model: OPENAI_COMPAT_ENV.OPENAI_COMPAT_EMBED_MODEL,
  "aws:cdk:bundling-stacks": [],
};

const NEPTUNE_ARN =
  "arn:aws-cn:neptune-db:cn-north-1:123456789012:cluster-abc123/*";
const NEPTUNE_ENDPOINT =
  "test-neptune.cluster-abc.cn-north-1.neptune.amazonaws.com.cn";

/**
 * Known `arn:aws:` text that reaches the API stack through the Smithy-generated
 * OpenAPI spec (`models/src/main/smithy/common.smithy`, S3 bucket ARN shape):
 * a schema `pattern` and its doc string. It lives outside infra, so it is
 * excluded here by exact text rather than by skipping the spec body — the
 * body also carries the integration URIs this test must cover.
 */
const KNOWN_SPEC_LITERALS = [
  '"pattern":"^arn:aws:s3:::',
  "Format: arn:aws:s3:::{bucket-name}",
];

/** Every `arn:aws:` occurrence in a stack's template, with a little context. */
function literalAwsArns(stack: cdk.Stack): string[] {
  let json = JSON.stringify(Template.fromStack(stack).toJSON());
  for (const known of KNOWN_SPEC_LITERALS) {
    json = json.split(known).join("<known>");
  }
  return [...json.matchAll(/.{0,40}arn:aws:.{0,60}/g)].map((m) => m[0]);
}

/** Build every stack under test in one cn-north-1 app. */
function buildStacks(context: Record<string, unknown> = {}): cdk.Stack[] {
  const app = new cdk.App({ context: { ...CN_CONTEXT, ...context } });
  const network = new NetworkStack(app, "CnNetwork", { env: CN_ENV });
  const storage = new StorageStack(app, "CnStorage", { network, env: CN_ENV });

  const depStack = new cdk.Stack(app, "CnDeps", { env: CN_ENV });
  const vpc = new ec2.Vpc(depStack, "Vpc", { maxAzs: 2 });
  const sg = (id: string) => new ec2.SecurityGroup(depStack, id, { vpc });
  const table = (id: string) =>
    new dynamodb.Table(depStack, id, {
      partitionKey: { name: "PK", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "SK", type: dynamodb.AttributeType.STRING },
      stream: dynamodb.StreamViewType.NEW_AND_OLD_IMAGES,
    });
  const rolesTable = table("Roles");
  const rrmTable = table("RRM");
  const ontologyBucket = new s3.Bucket(depStack, "OntologyBucket", {
    bucketName: `${PREFIX}-ontology-artifacts`,
  });

  const pipelineHost = new cdk.Stack(app, "CnDeletionPipeline", {
    env: CN_ENV,
  });
  new NamespaceDeletionPipeline(pipelineHost, "DeletionPipeline", {
    prefixed: (name: string) => `${PREFIX}-${name}`,
    lambdaCode: lambda.Code.fromInline("def handler(event, context): pass"),
    vpc,
    vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
    namespacesTable: table("Namespaces"),
    resourceRoleMappingsTable: rrmTable,
    rolesTable,
    sourcesApiFnName: `${PREFIX}-sources-api`,
    metricApiFnName: `${PREFIX}-metric-api`,
    datazoneDomainId: "dzd-123",
    namespaceApiFnRoleArn: `arn:aws-cn:iam::123456789012:role/${PREFIX}-namespace-api`,
    vkgClusterArn: "arn:aws-cn:ecs:cn-north-1:123456789012:cluster/vkg",
    cloudMapNamespaceId: "ns-abc123",
    ontologyBucketName: `${PREFIX}-ontology-artifacts`,
    opensearchEndpoint:
      "https://vpc-coa-dev-vector-store-abc.cn-north-1.es.amazonaws.com.cn",
    opensearchCollectionName: `${PREFIX}-vector-store`,
    resourcePrefix: `${PREFIX}-`,
  });

  return [
    network,
    storage,
    pipelineHost,
    new IdpAuthenticationStack(app, "CnAuth", { env: CN_ENV }),
    new WebStack(app, "CnWeb", {
      env: CN_ENV,
      isCognitoMode: true,
      apiRestApiId: "abc123",
      apiStageName: "prod",
      // No webAclId / autoWebAclParam: edge_waf is off in cn- regions, so
      // bin/app.ts creates no *-edge-waf stack and passes neither.
    }),
    new SourcesStack(app, "CnSources", {
      network,
      storage,
      allowedOrigin: "https://test.example.com",
      env: CN_ENV,
    }),
    new MetricServiceStack(app, "CnMetricService", {
      vpc: network.vpc,
      lambdaSecurityGroup: network.lambdaSecurityGroup,
      aossSecurityGroup: network.aossSecurityGroup,
      neptuneEndpoint: NEPTUNE_ENDPOINT,
      neptuneClusterArn: NEPTUNE_ARN,
      env: CN_ENV,
    }),
    new NamespaceStack(app, "CnNamespace", {
      vpc,
      lambdaSecurityGroup: sg("NamespaceLambdaSG"),
      rolesTable,
      resourceRoleMappingsTable: rrmTable,
      env: CN_ENV,
    }),
    new ServeStack(app, "CnServe", {
      env: CN_ENV,
      vpc,
      aossSecurityGroup: sg("AossSG"),
      neptuneSecurityGroup: sg("NeptuneSG"),
      lambdaSecurityGroup: sg("LambdaSG"),
      neptuneClusterArn: NEPTUNE_ARN,
      neptuneEndpoint: NEPTUNE_ENDPOINT,
      ontologyBucketArn: `arn:aws-cn:s3:::${PREFIX}-ontology-artifacts`,
      vkgEndpoint: `http://vkg.${PREFIX}-services.local:8080`,
      rolesTable,
      resourceRoleMappingsTable: rrmTable,
    }),
    new McpStack(app, "CnMcp", {
      env: CN_ENV,
      vpc,
      rolesTable,
      resourceRoleMappingsTable: rrmTable,
    }),
    new ApiStack(app, "CnApi", {
      env: CN_ENV,
      allowedOrigin: "*",
      vpc,
      rolesTable,
      resourceRoleMappingsTable: rrmTable,
      cacheInvalidationTable: table("Cache"),
    }),
    new OntologyStack(app, "CnOntology", {
      env: CN_ENV,
      network,
      serviceNamespace: network.serviceNamespace,
      neptuneClusterArn: NEPTUNE_ARN,
      neptuneClusterEndpoint: NEPTUNE_ENDPOINT,
      ontologyArtifactsBucket: ontologyBucket,
      // No smusDomainId: the dynamodb metadata store has no DataZone domain.
      allowedOrigin: "https://test.example.com",
    }),
    new VkgStack(app, "CnVkg", {
      env: CN_ENV,
      network,
      serviceNamespace: network.serviceNamespace,
      ontologyBucket,
    }),
  ];
}

type Env = Record<string, unknown>;

const templateIn = (stacks: cdk.Stack[], stackName: string): Template =>
  Template.fromStack(stacks.find((s) => s.stackName === stackName)!);

/** Environment of the Lambda or ECS task family named `${PREFIX}-${name}`. */
function consumerEnvIn(
  stacks: cdk.Stack[],
  stackName: string,
  name: string,
): Env {
  const template = templateIn(stacks, stackName);
  const fullName = `${PREFIX}-${name}`;
  const fns = Object.values(
    template.findResources("AWS::Lambda::Function", {
      Properties: { FunctionName: fullName },
    }),
  );
  if (fns.length === 1) return fns[0].Properties.Environment.Variables;
  const taskDefs = Object.values(
    template.findResources("AWS::ECS::TaskDefinition", {
      Properties: { Family: fullName },
    }),
  );
  expect(taskDefs).toHaveLength(1);
  return Object.fromEntries(
    (
      taskDefs[0].Properties.ContainerDefinitions[0].Environment as Array<{
        Name: string;
        Value: unknown;
      }>
    ).map((e) => [e.Name, e.Value]),
  );
}

/** Environment of the stack's single AgentCore Runtime (serve). */
function runtimeEnvIn(stacks: cdk.Stack[], stackName: string): Env {
  const runtimes = Object.values(
    templateIn(stacks, stackName).findResources(
      "AWS::BedrockAgentCore::Runtime",
    ),
  );
  expect(runtimes).toHaveLength(1);
  return runtimes[0].Properties.EnvironmentVariables;
}

/** Every IAM statement, across `stacks`, that names `secretArn`. */
function secretGrantsIn(stacks: cdk.Stack[], secretArn: string): unknown[] {
  return stacks.flatMap((stack) =>
    Object.values(
      Template.fromStack(stack).findResources("AWS::IAM::Policy"),
    ).flatMap((p) =>
      (p.Properties.PolicyDocument.Statement as unknown[]).filter((st) =>
        JSON.stringify(st).includes(secretArn),
      ),
    ),
  );
}

/**
 * Every value of a `BEDROCK_REGION` environment variable in the stack: Lambda /
 * AgentCore Runtime maps and ECS `{ Name, Value }` entries.
 */
function bedrockRegionValues(stack: cdk.Stack): unknown[] {
  const values: unknown[] = [];
  const walk = (node: unknown): void => {
    if (Array.isArray(node)) return node.forEach(walk);
    if (!node || typeof node !== "object") return;
    const obj = node as Record<string, unknown>;
    if ("BEDROCK_REGION" in obj) values.push(obj.BEDROCK_REGION);
    if (obj.Name === "BEDROCK_REGION") values.push(obj.Value);
    Object.values(obj).forEach(walk);
  };
  walk(Template.fromStack(stack).toJSON());
  return values;
}

/** Lambdas / ECS task families (besides serve) that call an LLM or embeddings. */
const LLM_CONSUMERS: Array<[string, string]> = [
  ["CnOntology", "ontology-engine"],
  ["CnSources", "sources-db-enrichment-agent"],
  ["CnSources", "sources-doc-kg-build"],
  ["CnMetricService", "metric-api"],
  ["CnMetricService", "metric-import-worker"],
];

describe("partition regression (aws-cn)", () => {
  const stacks = buildStacks();

  it.each(stacks.map((s) => [s.stackName, s] as const))(
    "%s synthesizes no literal arn:aws: ARN",
    (_name, stack) => {
      expect(literalAwsArns(stack)).toEqual([]);
    },
  );

  it.each(["CnServe", "CnMcp"])(
    "%s runs the China ECR image URI verbatim",
    (stackName) => {
      const stack = stacks.find((s) => s.stackName === stackName)!;
      Template.fromStack(stack).hasResourceProperties(
        "AWS::BedrockAgentCore::Runtime",
        {
          AgentRuntimeArtifact: {
            ContainerConfiguration: {
              ContainerUri: `${ECR_HOST}/coa-dev:latest`,
            },
          },
        },
      );
    },
  );

  it.each(["CnServe", "CnMcp"])(
    "%s pins AgentCore Runtime PlatformVersion V2",
    (stackName) => {
      const stack = stacks.find((s) => s.stackName === stackName)!;
      const runtimes = Template.fromStack(stack).findResources(
        "AWS::BedrockAgentCore::Runtime",
      );
      expect(Object.keys(runtimes)).toHaveLength(1);
      for (const runtime of Object.values(runtimes)) {
        expect(runtime.Properties.PlatformVersion).toBe("V2");
      }
    },
  );

  it("CnServe omits AgentCore Memory by default in a cn- region", () => {
    const template = Template.fromStack(
      stacks.find((s) => s.stackName === "CnServe")!,
    );
    template.resourceCountIs("AWS::BedrockAgentCore::Memory", 0);
    for (const runtime of Object.values(
      template.findResources("AWS::BedrockAgentCore::Runtime"),
    )) {
      expect(runtime.Properties.EnvironmentVariables).not.toHaveProperty(
        "MEMORY_ID",
      );
    }
  });

  // ── China defaults of the deployment switches ──────────────────────
  const templateOf = (stackName: string): Template =>
    templateIn(stacks, stackName);
  const consumerEnv = (stackName: string, name: string): Env =>
    consumerEnvIn(stacks, stackName, name);

  /** Actions of each IAM policy in the stack that names the metadata table. */
  function metadataStorePolicyActions(stackName: string): string[][] {
    return Object.values(
      templateOf(stackName).findResources("AWS::IAM::Policy"),
    ).flatMap((policy) => {
      const statements = (
        policy.Properties.PolicyDocument.Statement as Array<{
          Action: string | string[];
        }>
      ).filter((st) =>
        /MetadataStoreTable|metadata-store/.test(JSON.stringify(st)),
      );
      return statements.length
        ? [statements.flatMap((st) => [st.Action].flat())]
        : [];
    });
  }

  const WRITE_ACTIONS = [
    "dynamodb:PutItem",
    "dynamodb:UpdateItem",
    "dynamodb:DeleteItem",
    "dynamodb:BatchWriteItem",
  ];
  const METADATA_STORE_WRITERS: Array<[string, string]> = [
    ["CnNamespace", "namespace-api"],
    ["CnNamespace", "ns-deletion-platform"],
    ["CnSources", "sources-db-connector"],
    ["CnSources", "sources-db-enrichment-agent"],
    ["CnSources", "sources-bulk-review-worker"],
    ["CnSources", "sources-delete-worker"],
    ["CnSources", "sources-api"],
  ];
  const METADATA_STORE_READERS: Array<[string, string]> = [
    ["CnMetricService", "metric-api"],
    ["CnMetricService", "metric-import-worker"],
    ["CnOntology", "ontology-engine"],
  ];

  it("CnNamespace creates the DynamoDB metadata-store table and its GSI", () => {
    templateOf("CnNamespace").hasResourceProperties("AWS::DynamoDB::Table", {
      TableName: `${PREFIX}-metadata-store`,
      BillingMode: "PAY_PER_REQUEST",
      KeySchema: [
        { AttributeName: "PK", KeyType: "HASH" },
        { AttributeName: "SK", KeyType: "RANGE" },
      ],
      GlobalSecondaryIndexes: [
        {
          IndexName: "ProjectAssetsIndex",
          KeySchema: [
            { AttributeName: "projectId", KeyType: "HASH" },
            { AttributeName: "name", KeyType: "RANGE" },
          ],
          Projection: { ProjectionType: "ALL" },
        },
      ],
    });
  });

  it.each([...METADATA_STORE_WRITERS, ...METADATA_STORE_READERS])(
    "%s %s uses the DynamoDB metadata store, not DataZone",
    (stackName, name) => {
      const env = consumerEnv(stackName, name);
      expect(env.METADATA_STORE_BACKEND).toBe("dynamodb");
      if (stackName !== "CnNamespace") {
        expect(env.METADATA_STORE_TABLE).toBe(`${PREFIX}-metadata-store`);
      } else {
        expect(env.METADATA_STORE_TABLE).toBeDefined();
      }
      expect(env).not.toHaveProperty("SMUS_DOMAIN_ID");
      expect(env).not.toHaveProperty("DATAZONE_DOMAIN_ID");
    },
  );

  it.each([
    ["CnNamespace", 2, true],
    ["CnSources", 5, true],
    ["CnMetricService", 2, false],
    ["CnOntology", 1, false],
  ] as const)(
    "%s grants %d role(s) metadata-store access (write: %s)",
    (stackName, count, write) => {
      const policies = metadataStorePolicyActions(stackName);
      expect(policies).toHaveLength(count);
      for (const actions of policies) {
        expect(actions).toContain("dynamodb:Query");
        for (const action of WRITE_ACTIONS) {
          expect(actions.includes(action)).toBe(write);
        }
      }
    },
  );

  it.each(stacks.map((s) => [s.stackName, s] as const))(
    "%s has no DataZone/SMUS resources or grants",
    (_name, stack) => {
      const json = JSON.stringify(Template.fromStack(stack).toJSON());
      expect(json).not.toMatch(
        /AWS::DataZone::|"datazone:|datazone\.amazonaws/,
      );
      expect(json).not.toContain("/smus/domain-id");
      expect(json).not.toContain(`${PREFIX}-asset-type-cr`);
      expect(json).not.toContain("SageMakerStudioAdminIAMConsolePolicy");
    },
  );

  it.each(LLM_CONSUMERS)(
    "%s %s calls the OpenAI-compatible provider",
    (stackName, name) => {
      expect(consumerEnv(stackName, name)).toMatchObject(OPENAI_COMPAT_ENV);
    },
  );

  it("CnServe calls the OpenAI-compatible provider", () => {
    expect(runtimeEnvIn(stacks, "CnServe")).toMatchObject(OPENAI_COMPAT_ENV);
  });

  it.each(stacks.map((s) => [s.stackName, s] as const))(
    "%s depends on no commercial-partition Bedrock (region, API key, model)",
    (_name, stack) => {
      const json = JSON.stringify(Template.fromStack(stack).toJSON());
      expect(json).not.toMatch(
        /BEDROCK_API_KEY_SECRET_ARN|BEDROCK_MODEL_DEFAULT|BEDROCK_MODEL_MAP/,
      );
      // Pre-existing BEDROCK_REGION variables only ever name the deploy region.
      for (const value of bedrockRegionValues(stack)) {
        expect([{ Ref: "AWS::Region" }, "cn-north-1"]).toContainEqual(value);
      }
      const policies = [
        ...Object.values(
          Template.fromStack(stack).findResources("AWS::IAM::Policy"),
        ),
        ...Object.values(
          Template.fromStack(stack).findResources("AWS::IAM::ManagedPolicy"),
        ),
      ];
      expect(JSON.stringify(policies)).not.toMatch(/bedrock[-_]api[-_]key/i);
    },
  );

  it("CnOntology keeps LLM_REGION on the deployment region", () => {
    expect(consumerEnv("CnOntology", "ontology-engine").LLM_REGION).toEqual({
      Ref: "AWS::Region",
    });
  });

  it("grants GetSecretValue on exactly the provider API-key secret, once per LLM consumer", () => {
    const grants = secretGrantsIn(stacks, OPENAI_COMPAT_SECRET_ARN);
    // serve + every LLM_CONSUMERS entry
    expect(grants).toHaveLength(LLM_CONSUMERS.length + 1);
    for (const grant of grants) {
      expect(grant).toEqual({
        Action: "secretsmanager:GetSecretValue",
        Effect: "Allow",
        Resource: OPENAI_COMPAT_SECRET_ARN,
      });
    }
  });

  it("deploys no GuardrailStack (bin/app.ts gates it on in-region Bedrock)", () => {
    expect(usesInRegionBedrock(stacks[0])).toBe(false);
  });

  it("deploys no edge-waf stack, and CnWeb's distribution has no WebACL", () => {
    // bin/app.ts creates the us-east-1 *-edge-waf stack only when edge_waf is on.
    expect(resolveEdgeWafEnabled(stacks[0])).toBe(false);
    const web = templateOf("CnWeb");
    const distributions = Object.values(
      web.findResources("AWS::CloudFront::Distribution"),
    );
    expect(distributions).toHaveLength(1);
    expect(distributions[0].Properties.DistributionConfig).not.toHaveProperty(
      "WebACLId",
    );
    expect(
      Object.keys(web.findResources("Custom::AWS")).filter((id) =>
        id.startsWith("ReadCloudFrontWebAcl"),
      ),
    ).toEqual([]);
  });

  it.each(stacks.map((s) => [s.stackName, s] as const))(
    "%s reads no Bedrock guardrail SSM params at deploy time",
    (_name, stack) => {
      expect(
        JSON.stringify(Template.fromStack(stack).toJSON().Parameters ?? {}),
      ).not.toContain("guardrail");
    },
  );

  it("CnSources doc-kg-build reads no Bedrock guardrail SSM params", () => {
    const env = consumerEnv("CnSources", "sources-doc-kg-build");
    expect(env.RETRIEVAL_GUARDRAIL_ID).toBe("");
    expect(env.RETRIEVAL_GUARDRAIL_VERSION).toBe("");
    expect(
      JSON.stringify(templateOf("CnSources").toJSON().Parameters ?? {}),
    ).not.toContain("guardrail");
  });

  it("CnNetwork creates no Bedrock runtime or DataZone VPC endpoint", () => {
    const serviceNames = Object.values(
      templateOf("CnNetwork").findResources("AWS::EC2::VPCEndpoint"),
    ).map((e) => JSON.stringify(e.Properties.ServiceName));
    expect(serviceNames.some((n) => n.includes("bedrock-agentcore"))).toBe(
      true,
    );
    expect(
      serviceNames.filter((n) => /bedrock-runtime|datazone/.test(n)),
    ).toEqual([]);
  });

  it.each(stacks.map((s) => [s.stackName, s] as const))(
    "%s trusts no bedrock.amazonaws.com principal (IAM in AWS China rejects it)",
    (_name, stack) => {
      const trusts = Object.values(
        Template.fromStack(stack).findResources("AWS::IAM::Role"),
      ).map((r) => JSON.stringify(r.Properties.AssumeRolePolicyDocument));
      expect(
        trusts.filter((t) => t.includes('"bedrock.amazonaws.com"')),
      ).toEqual([]);
    },
  );

  it.each(stacks.map((s) => [s.stackName, s] as const))(
    "%s tags no Lambda event source mapping (rejected in AWS China)",
    (_name, stack) => {
      const tagged = Object.values(
        Template.fromStack(stack).findResources(
          "AWS::Lambda::EventSourceMapping",
        ),
      ).filter((r) => r.Properties.Tags !== undefined);
      expect(tagged).toEqual([]);
    },
  );

  it("CnApi uses a REGIONAL endpoint (AWS China has no EDGE)", () => {
    templateOf("CnApi").hasResourceProperties("AWS::ApiGateway::RestApi", {
      EndpointConfiguration: { Types: ["REGIONAL"] },
    });
  });

  it("CnWeb reaches its bucket through an OAI (AWS China has no OAC)", () => {
    const template = templateOf("CnWeb");
    template.resourceCountIs("AWS::CloudFront::OriginAccessControl", 0);
    template.resourceCountIs(
      "AWS::CloudFront::CloudFrontOriginAccessIdentity",
      1,
    );
  });

  it("CnNetwork names China endpoint services with their real prefix", () => {
    const serviceNames = Object.values(
      templateOf("CnNetwork").findResources("AWS::EC2::VPCEndpoint"),
    ).map((e) => e.Properties.ServiceName as unknown);
    for (const name of ["sts", "states", "lambda", "sqs", "athena"]) {
      expect(serviceNames).toContain(`cn.com.amazonaws.cn-north-1.${name}`);
    }
    for (const name of ["logs", "ssm", "secretsmanager", "bedrock-agentcore"]) {
      expect(serviceNames).toContain(`com.amazonaws.cn-north-1.${name}`);
    }
    // Neptune Analytics is not offered in AWS China.
    expect(
      serviceNames.filter((n) => JSON.stringify(n).includes("neptune-graph")),
    ).toEqual([]);
  });
  // ── vector_store=opensearch (managed domain, no AOSS) ───────────────
  const DOMAIN_PATH = `:domain/${PREFIX}-vector-store`;

  /**
   * Every IAM statement in `stackName` that grants an `es:` action (large
   * roles spill into an overflow managed policy).
   */
  function esStatements(
    stackName: string,
  ): Array<{ Action: string | string[]; Resource: unknown }> {
    const template = templateOf(stackName);
    return [
      ...Object.values(template.findResources("AWS::IAM::Policy")),
      ...Object.values(template.findResources("AWS::IAM::ManagedPolicy")),
    ].flatMap((p) =>
      (
        p.Properties.PolicyDocument.Statement as Array<{
          Action: string | string[];
          Resource: unknown;
        }>
      ).filter((st) => [st.Action].flat().some((a) => a.startsWith("es:"))),
    );
  }

  it("CnStorage creates a small VPC-only, encrypted OpenSearch domain", () => {
    const template = templateOf("CnStorage");
    template.resourceCountIs("AWS::OpenSearchService::Domain", 1);
    template.hasResourceProperties("AWS::OpenSearchService::Domain", {
      DomainName: `${PREFIX}-vector-store`,
      EngineVersion: "OpenSearch_3.7",
      ClusterConfig: {
        InstanceType: "t3.small.search",
        InstanceCount: 1,
        DedicatedMasterEnabled: false,
        ZoneAwarenessEnabled: false,
        MultiAZWithStandbyEnabled: false,
      },
      EBSOptions: { EBSEnabled: true, VolumeType: "gp3", VolumeSize: 10 },
      VPCOptions: {
        SubnetIds: [Match.anyValue()],
        SecurityGroupIds: [Match.anyValue()],
      },
      EncryptionAtRestOptions: { Enabled: true },
      NodeToNodeEncryptionOptions: { Enabled: true },
      DomainEndpointOptions: {
        EnforceHTTPS: true,
        TLSSecurityPolicy: "Policy-Min-TLS-1-2-2019-07",
      },
    });
    const domain = Object.values(
      template.findResources("AWS::OpenSearchService::Domain"),
    )[0];
    // No fine-grained access control; the access policy only delegates to
    // IAM principals of this account.
    expect(domain.Properties).not.toHaveProperty("AdvancedSecurityOptions");
    const statements = domain.Properties.AccessPolicies.Statement;
    expect(statements).toHaveLength(1);
    expect(JSON.stringify(statements[0].Principal)).toMatch(/:root"/);
    expect(JSON.stringify(statements[0].Principal)).not.toContain('"*"');
    expect(statements[0].Action).toBe("es:ESHttp*");
    expect(JSON.stringify(statements[0].Resource)).toContain(
      `${DOMAIN_PATH}/*`,
    );
  });

  it("CnStorage publishes the domain endpoint through the same SSM parameter", () => {
    templateOf("CnStorage").hasResourceProperties("AWS::SSM::Parameter", {
      Name: Match.stringLikeRegexp("/opensearch/endpoint$"),
      Value: {
        "Fn::Join": [
          "",
          ["https://", { "Fn::GetAtt": [Match.anyValue(), "DomainEndpoint"] }],
        ],
      },
    });
  });

  it.each(stacks.map((s) => [s.stackName, s] as const))(
    "%s has no OpenSearch Serverless resources, endpoints or grants",
    (_name, stack) => {
      const json = JSON.stringify(Template.fromStack(stack).toJSON());
      expect(json).not.toMatch(/AWS::OpenSearchServerless::|"aoss:|\.aoss/);
    },
  );

  it("CnNetwork attaches no AOSS endpoint; the domain SG accepts only SG-scoped HTTPS", () => {
    const template = templateOf("CnNetwork");
    const sgs = template.findResources("AWS::EC2::SecurityGroup", {
      Properties: { GroupName: `${PREFIX}-opensearch-sg` },
    });
    expect(Object.keys(sgs)).toHaveLength(1);
    const [sgId] = Object.keys(sgs);
    expect(JSON.stringify(sgs[sgId])).not.toContain("0.0.0.0/0");
    const ingress = Object.values(
      template.findResources("AWS::EC2::SecurityGroupIngress", {
        Properties: { GroupId: { "Fn::GetAtt": [sgId, "GroupId"] } },
      }),
    );
    expect(ingress.length).toBeGreaterThan(0);
    for (const rule of ingress) {
      expect(rule.Properties).toMatchObject({ FromPort: 443, ToPort: 443 });
      expect(rule.Properties.SourceSecurityGroupId).toBeDefined();
      expect(rule.Properties).not.toHaveProperty("CidrIp");
    }
    // The domain sits behind that SG.
    templateOf("CnStorage").hasResourceProperties(
      "AWS::OpenSearchService::Domain",
      {
        VPCOptions: {
          SecurityGroupIds: [
            { "Fn::ImportValue": Match.stringLikeRegexp(sgId) },
          ],
        },
      },
    );
  });

  it.each([
    ["CnServe", 2, ["es:ESHttpGet", "es:ESHttpHead", "es:ESHttpPost"]],
    ["CnMetricService", 2, ["es:ESHttp*"]],
    ["CnOntology", 1, ["es:ESHttp*"]],
    ["CnSources", 1, ["es:ESHttp*"]],
  ] as const)(
    "%s grants %d role(s) data-plane access scoped to the domain",
    (stackName, count, actions) => {
      const grants = esStatements(stackName).filter((st) =>
        [st.Action].flat().some((a) => a.startsWith("es:ESHttp")),
      );
      expect(grants).toHaveLength(count);
      for (const grant of grants) {
        expect([grant.Action].flat()).toEqual(actions);
        const resources = [grant.Resource].flat();
        expect(resources).toHaveLength(1);
        expect(JSON.stringify(resources[0])).toContain(`${DOMAIN_PATH}/*`);
      }
    },
  );

  it("CnOntology health check describes only the vector-store domain", () => {
    const describe = esStatements("CnOntology").filter(
      (st) => st.Action === "es:DescribeDomain",
    );
    expect(describe).toHaveLength(1);
    expect(JSON.stringify(describe[0].Resource)).toMatch(
      new RegExp(`${DOMAIN_PATH}"`),
    );
    expect(consumerEnv("CnOntology", "ontology-api-proxy")).toMatchObject({
      OPENSEARCH_DOMAIN_NAME: `${PREFIX}-vector-store`,
    });
  });

  it("CnDeletionPipeline may only drop the GraphRAG indexes", () => {
    const grants = esStatements("CnDeletionPipeline");
    expect(grants).toHaveLength(1);
    expect(grants[0].Action).toEqual([
      "es:ESHttpHead",
      "es:ESHttpGet",
      "es:ESHttpDelete",
    ]);
    const resources = JSON.stringify(grants[0].Resource);
    for (const index of ["chunk_*", "topic_*", "statement_*"]) {
      expect(resources).toContain(`${DOMAIN_PATH}/${index}`);
    }
    expect(resources).not.toContain(`${DOMAIN_PATH}/*`);
  });
});

describe("partition regression (aws-cn, explicit cross-region Bedrock opt-in)", () => {
  // Still synthesizes when passed explicitly; a China deployment must not use
  // it by default (commercial-partition resource).
  const stacks = buildStacks(CROSS_REGION_BEDROCK_CONTEXT);
  const CROSS_REGION_BEDROCK_ENV = {
    BEDROCK_REGION: "us-east-1",
    BEDROCK_API_KEY_SECRET_ARN: BEDROCK_SECRET_ARN,
  };

  it.each(LLM_CONSUMERS)(
    "%s %s calls native Bedrock in us-east-1 with the API key",
    (stackName, name) => {
      const env = consumerEnvIn(stacks, stackName, name);
      expect(env).toMatchObject(CROSS_REGION_BEDROCK_ENV);
      expect(env).not.toHaveProperty("BEDROCK_MODEL_DEFAULT");
      expect(env).not.toHaveProperty("LLM_PROVIDER");
      expect(Object.keys(env).filter((k) => k.startsWith("OPENAI_"))).toEqual(
        [],
      );
    },
  );

  it("CnServe calls native Bedrock in us-east-1 with the API key", () => {
    const env = runtimeEnvIn(stacks, "CnServe");
    expect(env).toMatchObject(CROSS_REGION_BEDROCK_ENV);
    expect(env).not.toHaveProperty("LLM_PROVIDER");
  });

  it("grants GetSecretValue on exactly the Bedrock API-key secret, once per LLM consumer", () => {
    const grants = secretGrantsIn(stacks, BEDROCK_SECRET_ARN);
    expect(grants).toHaveLength(LLM_CONSUMERS.length + 1);
    for (const grant of grants) {
      expect(grant).toEqual({
        Action: "secretsmanager:GetSecretValue",
        Effect: "Allow",
        Resource: BEDROCK_SECRET_ARN,
      });
    }
    expect(secretGrantsIn(stacks, OPENAI_COMPAT_SECRET_ARN)).toEqual([]);
  });

  it("skips Guardrails and the Bedrock runtime VPC endpoint", () => {
    expect(usesInRegionBedrock(stacks[0])).toBe(false);
    const env = consumerEnvIn(stacks, "CnSources", "sources-doc-kg-build");
    expect(env.RETRIEVAL_GUARDRAIL_ID).toBe("");
    const serviceNames = Object.values(
      templateIn(stacks, "CnNetwork").findResources("AWS::EC2::VPCEndpoint"),
    ).map((e) => JSON.stringify(e.Properties.ServiceName));
    expect(serviceNames.filter((n) => n.includes("bedrock-runtime"))).toEqual(
      [],
    );
  });
});
