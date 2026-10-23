// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { Template } from "aws-cdk-lib/assertions";

/**
 * Pseudo parameters substituted by {@link resolveCommercialPartition}, with the
 * values CloudFormation gives them in the commercial `aws` partition.
 */
const COMMERCIAL_PSEUDO_PARAMS: Record<string, string> = {
  "AWS::Partition": "aws",
  "AWS::URLSuffix": "amazonaws.com",
};

function resolveNode(node: unknown): unknown {
  if (Array.isArray(node)) return node.map(resolveNode);
  if (node === null || typeof node !== "object") return node;

  const obj = node as Record<string, unknown>;
  const keys = Object.keys(obj);
  if (
    keys.length === 1 &&
    keys[0] === "Ref" &&
    typeof obj.Ref === "string" &&
    obj.Ref in COMMERCIAL_PSEUDO_PARAMS
  ) {
    return COMMERCIAL_PSEUDO_PARAMS[obj.Ref];
  }

  if (keys.length === 1 && keys[0] === "Fn::Join") {
    const [sep, parts] = obj["Fn::Join"] as [string, unknown[]];
    const merged: unknown[] = [];
    for (const part of (parts as unknown[]).map(resolveNode)) {
      const last = merged[merged.length - 1];
      if (sep === "" && typeof part === "string" && typeof last === "string") {
        merged[merged.length - 1] = last + part;
      } else {
        merged.push(part);
      }
    }
    if (merged.every((p) => typeof p === "string")) {
      return (merged as string[]).join(sep);
    }
    return { "Fn::Join": [sep, merged] };
  }

  return Object.fromEntries(keys.map((k) => [k, resolveNode(obj[k])]));
}

/**
 * The template as it renders in the commercial partition.
 *
 * Stacks build ARNs and endpoint hosts from `AWS::Partition` / `AWS::URLSuffix`
 * so they also deploy to aws-cn, which turns every such literal into an
 * `Fn::Join`. Assertions written against the commercial rendering (`arn:aws:…`,
 * `s3.us-east-1.amazonaws.com`) keep working on the result; the absence of a
 * hardcoded partition is asserted separately in `partition-regression.test.ts`.
 */
export function resolveCommercialPartition(template: Template): Template {
  return Template.fromJSON(
    resolveNode(template.toJSON()) as Record<string, unknown>,
  );
}
