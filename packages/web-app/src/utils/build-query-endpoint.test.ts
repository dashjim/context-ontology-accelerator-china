// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect } from "vitest";
import { buildQueryEndpoint } from "./build-query-endpoint";
import { S3_ARN_RE } from "./helpers";

describe("buildQueryEndpoint", () => {
  it("uses amazonaws.com in commercial regions", () => {
    expect(
      buildQueryEndpoint({ region: "us-east-1", serveRuntimeArn: "arn:aws:x" }),
    ).toBe(
      "https://bedrock-agentcore.us-east-1.amazonaws.com/runtimes/arn%3Aaws%3Ax/invocations?qualifier=DEFAULT",
    );
  });

  it("uses amazonaws.com.cn in China regions", () => {
    expect(
      buildQueryEndpoint({
        region: "cn-north-1",
        serveRuntimeArn: "arn:aws-cn:x",
      }),
    ).toBe(
      "https://bedrock-agentcore.cn-north-1.amazonaws.com.cn/runtimes/arn%3Aaws-cn%3Ax/invocations?qualifier=DEFAULT",
    );
  });
});

describe("S3_ARN_RE", () => {
  it("accepts bucket ARNs in every partition", () => {
    expect(S3_ARN_RE.test("arn:aws:s3:::my-bucket")).toBe(true);
    expect(S3_ARN_RE.test("arn:aws-cn:s3:::my-bucket")).toBe(true);
    expect(S3_ARN_RE.test("arn:aws-us-gov:s3:::my-bucket")).toBe(true);
  });

  it("rejects an unknown partition", () => {
    expect(S3_ARN_RE.test("arn:evil:s3:::my-bucket")).toBe(false);
  });
});
