#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# Validate the implicit SMUS Admin-role fallback without collapsing AWS
# credential, authorization, and service failures into "role does not exist".
set -euo pipefail

if [ -n "${SCL_SMUS_ADMIN_ARNS:-}" ]; then
  exit 0
fi

# The Admin role is only used by the SMUS metadata store. CDK context
# metadata_store_backend (passed via SCL_EXTRA_CONTEXT, see deploy.sh) defaults
# to dynamodb in cn- regions, where SMUS is not offered, and to smus elsewhere.
REGION="${CDK_DEFAULT_REGION:-${AWS_DEFAULT_REGION:-${AWS_REGION:-us-east-1}}}"
BACKEND="$(printf '%s\n' ${SCL_EXTRA_CONTEXT:-} | sed -n 's/^metadata_store_backend=//p' | tail -n 1)"
if [ -z "$BACKEND" ]; then
  [[ "$REGION" == cn-* ]] && BACKEND=dynamodb || BACKEND=smus
fi
if [ "$(printf '%s' "$BACKEND" | tr '[:upper:]' '[:lower:]')" = "dynamodb" ]; then
  echo "metadata_store_backend=dynamodb — no SMUS admin principal needed, skipping the 'Admin' role check."
  exit 0
fi

AWS_ERROR_FILE="$(mktemp)"
trap 'rm -f "$AWS_ERROR_FILE"' EXIT

aws_error_reason() {
  local error_code
  error_code="$(sed -nE 's/.*error occurred \(([^)]*)\).*/\1/p' "$AWS_ERROR_FILE")"
  error_code="${error_code%%$'\n'*}"
  if [ -n "$error_code" ]; then
    printf '%s\n' "$error_code"
  elif grep -q "Unable to locate credentials" "$AWS_ERROR_FILE"; then
    printf '%s\n' "UnableToLocateCredentials"
  elif grep -Eq "config profile .* could not be found" "$AWS_ERROR_FILE"; then
    printf '%s\n' "ProfileNotFound"
  elif grep -Eq "Could not connect to the endpoint URL|Connect timeout on endpoint URL" "$AWS_ERROR_FILE"; then
    printf '%s\n' "EndpointConnectionError"
  elif grep -q "SSL validation failed" "$AWS_ERROR_FILE"; then
    printf '%s\n' "SSLValidationError"
  else
    # AWS CLI stderr should never be echoed wholesale here: third-party wrappers
    # can add environment diagnostics, including credential material.
    printf '%s\n' "AWS CLI command failed (details suppressed)"
  fi
}

if ! CALLER_IDENTITY="$(aws sts get-caller-identity --query '[Account, Arn]' --output text 2>"$AWS_ERROR_FILE")"; then
  echo "ERROR: Could not determine the AWS account with the active credentials." >&2
  echo "AWS reason: $(aws_error_reason)" >&2
  echo "Verify AWS_PROFILE, credential expiration, network access, and sts:GetCallerIdentity, then retry." >&2
  exit 1
fi

read -r ACCOUNT_ID CALLER_ARN <<<"$CALLER_IDENTITY"
# Partition of the caller's own ARN (aws, aws-cn, aws-us-gov), for the hint below.
PARTITION="$(cut -d: -f2 <<<"${CALLER_ARN:-}")"
PARTITION="${PARTITION:-aws}"

if ! [[ "$ACCOUNT_ID" =~ ^[0-9]{12}$ ]]; then
  echo "ERROR: STS returned an invalid AWS account ID; refusing to infer an Admin-role ARN." >&2
  exit 1
fi

: >"$AWS_ERROR_FILE"
if ADMIN_ROLE_NAME="$(aws iam get-role --role-name Admin --query "Role.RoleName" --output text 2>"$AWS_ERROR_FILE")"; then
  if [ "$ADMIN_ROLE_NAME" != "Admin" ]; then
    echo "ERROR: IAM returned an unexpected result while verifying the fallback role 'Admin'." >&2
    exit 1
  fi
  echo "No SCL_SMUS_ADMIN_ARNS set — falling back to this account's existing 'Admin' role."
  exit 0
fi

AWS_REASON="$(aws_error_reason)"
if [ "$AWS_REASON" = "NoSuchEntity" ]; then
  echo "ERROR: No SMUS admin principal configured, and IAM confirmed that account $ACCOUNT_ID has no role named 'Admin'." >&2
  echo "Set SCL_SMUS_ADMIN_ARNS to the IAM role/user ARN(s) that human admins federate into," >&2
  echo "e.g. for an IAM Identity Center account, your permission set's federated role:" >&2
  echo "  SCL_SMUS_ADMIN_ARNS=arn:$PARTITION:iam::$ACCOUNT_ID:role/aws-reserved/sso.amazonaws.com/<region>/AWSReservedSSO_AdministratorAccess_<suffix> make deploy-dev" >&2
else
  echo "ERROR: No SMUS admin principal configured, and the fallback role 'Admin' could not be verified in account $ACCOUNT_ID." >&2
  echo "AWS reason: $AWS_REASON" >&2
  echo "Grant iam:GetRole for the fallback role, fix the AWS connection, or set SCL_SMUS_ADMIN_ARNS explicitly." >&2
fi
exit 1
