// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import * as cdk from "aws-cdk-lib";
import * as bedrockagentcore from "aws-cdk-lib/aws-bedrockagentcore";

/**
 * `AgentRuntimeArtifact.fromImageUri` that also accepts China ECR URIs.
 *
 * The L2 checks a literal container URI twice (in `fromImageUri` and again
 * when `Runtime` renders it) against a pattern that hard-codes the commercial
 * `.amazonaws.com` host, so a `*.dkr.ecr.cn-north-1.amazonaws.com.cn` URI
 * fails synth. Both checks skip unresolved tokens, so a China URI is wrapped
 * in a Lazy that resolves to the same literal string — the template is
 * unchanged. Every other URI goes through the L2 untouched and keeps its
 * validation.
 */
export function agentRuntimeArtifactFromImageUri(
  imageUri: string,
): bedrockagentcore.AgentRuntimeArtifact {
  const isChinaEcrHost =
    !cdk.Token.isUnresolved(imageUri) &&
    /^\d{12}\.dkr\.ecr\.cn-[a-z0-9-]+\.amazonaws\.com\.cn\//.test(imageUri);
  return bedrockagentcore.AgentRuntimeArtifact.fromImageUri(
    isChinaEcrHost ? cdk.Lazy.string({ produce: () => imageUri }) : imageUri,
  );
}

/**
 * Parse an ECR image URI into its component parts.
 *
 * @param imageUri - Full ECR image URI, e.g.
 *   `123456789012.dkr.ecr.us-east-1.amazonaws.com/my-repo:latest`
 *   (China regions use the `amazonaws.com.cn` suffix; the ARN partition is
 *   derived from the region).
 * @throws Error if the URI format is invalid.
 */
export function parseEcrImageUri(imageUri: string): {
  accountId: string;
  region: string;
  repositoryName: string;
  tag: string;
  repositoryArn: string;
} {
  const slashIndex = imageUri.indexOf("/");
  if (slashIndex === -1) {
    throw new Error(
      `Invalid ECR image URI: missing repository path. Got: ${imageUri}`,
    );
  }

  const host = imageUri.substring(0, slashIndex);
  const imageRef = imageUri.substring(slashIndex + 1);

  const hostParts = host.split(".");
  if (
    hostParts.length < 6 ||
    hostParts[1] !== "dkr" ||
    hostParts[2] !== "ecr"
  ) {
    throw new Error(
      `Invalid ECR image URI: host must be <account>.dkr.ecr.<region>.amazonaws.com. Got: ${host}`,
    );
  }

  const accountId = hostParts[0];
  const region = hostParts[3];
  const partition = region.startsWith("cn-")
    ? "aws-cn"
    : region.startsWith("us-gov-")
      ? "aws-us-gov"
      : "aws";

  const colonIndex = imageRef.lastIndexOf(":");
  const repositoryName =
    colonIndex !== -1 ? imageRef.substring(0, colonIndex) : imageRef;
  const tag = colonIndex !== -1 ? imageRef.substring(colonIndex + 1) : "latest";

  if (!repositoryName) {
    throw new Error(
      `Invalid ECR image URI: empty repository name. Got: ${imageUri}`,
    );
  }

  return {
    accountId,
    region,
    repositoryName,
    tag,
    repositoryArn: `arn:${partition}:ecr:${region}:${accountId}:repository/${repositoryName}`,
  };
}
