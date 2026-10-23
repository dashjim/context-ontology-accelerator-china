// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/** Build the AgentCore /invocations URL from runtime ARN + region.
 *
 * AgentCore Runtime exposes a single POST /invocations endpoint. The URL is
 * derived from the runtime ARN (stored in runtime-config.json) plus the
 * deployment region. Returns undefined if the ARN is missing or the region
 * format is invalid (prevents URL injection via tampered config).
 */
export function buildQueryEndpoint(
  ctx: { region: string; serveRuntimeArn?: string } | undefined,
): string | undefined {
  if (!ctx?.serveRuntimeArn) return undefined;
  // Validate region format to prevent URL injection via tampered config
  if (!/^[a-z]{2}-[a-z]+-\d+$/.test(ctx.region)) return undefined;
  const encoded = encodeURIComponent(ctx.serveRuntimeArn);
  const dnsSuffix = ctx.region.startsWith("cn-")
    ? "amazonaws.com.cn"
    : "amazonaws.com";
  return `https://bedrock-agentcore.${ctx.region}.${dnsSuffix}/runtimes/${encoded}/invocations?qualifier=DEFAULT`;
}
