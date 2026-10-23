#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Invoke the AgentCore Runtime via REST API (JWT auth).

Usage:
    ./invoke.py '{"query":"...","namespace":"..."}'
    ./invoke.py --user admin@example.com --password 'P@ss' '{"query":"..."}'

Environment:
    AWS_REGION      (default: us-east-1)
    SCL_PREFIX      (default: coa)
    SCL_USERNAME    Cognito username (default: from Secrets Manager)
    SCL_PASSWORD    Cognito password (default: from Secrets Manager)
"""

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
import uuid

import boto3

region = os.environ.get("AWS_REGION", "us-east-1")
prefix = os.environ.get("SCL_PREFIX", "coa")


def _consume_agentcore(resp):
    """Consume an AgentCore SSE response incrementally, stopping at the terminal
    event. The serve runtime keeps the connection open after the final event,
    so reading to EOF would block until the socket timeout."""
    for raw_line in resp:
        line = raw_line.decode("utf-8", "replace") if isinstance(raw_line, (bytes, bytearray)) else raw_line
        line = line.strip()
        if not line:
            continue
        data_str = line[len("data: ") :] if line.startswith("data: ") else line
        try:
            event = json.loads(data_str)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(event, str):
            try:
                event = json.loads(event)
            except (json.JSONDecodeError, ValueError):
                continue
        if not isinstance(event, dict):
            continue
        ftype = event.get("type")
        if ftype in ("step", "token", "stage_start"):
            continue
        if ftype == "done" and isinstance(event.get("payload"), dict):
            return {"result": event["payload"].get("result"), "requestId": event.get("requestId")}
        if ftype == "error" and isinstance(event.get("payload"), dict):
            return {**event["payload"], "requestId": event.get("requestId")}
        if "result" in event or "error" in event or "statusCode" in event:
            return event
        if any(k in event for k in ("resultRows", "result_rows")):
            return event
    return {"error": "no terminal event in stream", "statusCode": 504}


def main():
    parser = argparse.ArgumentParser(description="Invoke SCL AgentCore Runtime")
    parser.add_argument("payload", help="JSON payload to send")
    parser.add_argument("--user", default=os.environ.get("SCL_USERNAME", ""), help="Cognito username")
    parser.add_argument("--password", default=os.environ.get("SCL_PASSWORD", ""), help="Cognito password")
    parser.add_argument("--region", default=region, help="AWS region")
    args = parser.parse_args()

    rgn = args.region
    payload = json.loads(args.payload)
    ssm = boto3.client("ssm", region_name=rgn)

    # Resolve runtime ARN
    try:
        runtime_arn = ssm.get_parameter(Name=f"/{prefix}/serve/runtime-arn")["Parameter"]["Value"]
    except Exception as e:
        print(json.dumps({"error": f"Cannot resolve runtime ARN: {e}"}))
        sys.exit(1)

    # Resolve credentials
    username = args.user
    password = args.password
    if not username or not password:
        try:
            sm = boto3.client("secretsmanager", region_name=rgn)
            secret = json.loads(sm.get_secret_value(SecretId=f"{prefix}-dev-integ-test-user")["SecretString"])
            username = username or secret["username"]
            password = password or secret["password"]
        except Exception as e:
            print(json.dumps({"error": f"Cannot resolve credentials: {e}"}))
            sys.exit(1)

    # Authenticate
    try:
        client_id = ssm.get_parameter(Name=f"/{prefix}/userpool-client-id")["Parameter"]["Value"]
        cognito = boto3.client("cognito-idp", region_name=rgn)
        auth = cognito.initiate_auth(
            AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": username, "PASSWORD": password},
            ClientId=client_id,
        )
        token = auth["AuthenticationResult"]["IdToken"]
    except Exception as e:
        print(json.dumps({"error": f"Authentication failed: {e}"}))
        sys.exit(1)

    # Invoke
    encoded_arn = urllib.parse.quote(runtime_arn, safe="")
    suffix = "amazonaws.com.cn" if rgn.startswith("cn-") else "amazonaws.com"
    url = f"https://bedrock-agentcore.{rgn}.{suffix}/runtimes/{encoded_arn}/invocations?qualifier=DEFAULT"
    payload.setdefault("requestId", str(uuid.uuid4()))
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": f"cli-{uuid.uuid4().hex}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = _consume_agentcore(resp)
            print(json.dumps(result, indent=2))
    except urllib.error.HTTPError as e:
        print(json.dumps({"error": f"HTTP {e.code}", "detail": e.read().decode()[:500]}))
        sys.exit(1)
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
