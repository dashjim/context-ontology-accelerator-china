#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

# Pre-deploy validation — catches common silent failures before CDK runs.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ERRORS=0

err() { echo "ERROR: $1" >&2; ERRORS=$((ERRORS + 1)); }
warn() { echo "WARN:  $1" >&2; }
ok() { echo "  OK:  $1"; }

echo "=== Pre-deploy preflight checks ==="
echo ""

# ── 1. Required toolchain versions (Node 22+, Java 17+, pnpm) ────────────
# Mirrors the versions pinned in .mise.toml. We only check presence/version
# here — installing mise itself is a one-time local setup step (see
# scripts/setup-dev.sh) and is intentionally not automated in this script,
# since CI provisions its own toolchain and never calls deploy.sh.
if command -v node >/dev/null 2>&1; then
  NODE_MAJOR=$(node --version | sed 's/^v//' | cut -d. -f1)
  if [ "$NODE_MAJOR" -ge 22 ]; then
    ok "Node $(node --version) found"
  else
    err "Node 22+ required (found $(node --version)). Run: mise install"
  fi
else
  err "Node not found. Install via 'mise install' (see .mise.toml) or https://mise.run"
fi

if JAVA_CHECK="$("$REPO_ROOT/scripts/check-java-version.sh")"; then
  ok "$JAVA_CHECK"
else
  err "$JAVA_CHECK"
fi

if command -v pnpm >/dev/null 2>&1; then
  ok "pnpm $(pnpm --version) found"
else
  err "pnpm not found. Run: mise install (or npm install -g pnpm)"
fi

# ── 2. Python pip availability ────────────────────────────────────────────
if command -v pip >/dev/null 2>&1 && pip --version >/dev/null 2>&1; then
  ok "pip functional: $(pip --version 2>&1)"
elif command -v pip3 >/dev/null 2>&1 && pip3 --version >/dev/null 2>&1; then
  ok "pip3 functional (bundler resolves this automatically)"
else
  warn "Neither pip nor pip3 is functional. Docker bundling required."
fi

# ── 3. Container engine (Docker or Finch) ─────────────────────────────────
# CDK shells out to $CDK_DOCKER (default: docker) for asset bundling.
CONTAINER_ENGINE=""
if [ -n "${CDK_DOCKER:-}" ]; then
  if command -v "$CDK_DOCKER" >/dev/null 2>&1 && "$CDK_DOCKER" info >/dev/null 2>&1; then
    CONTAINER_ENGINE="$CDK_DOCKER"
    ok "CDK_DOCKER=$CDK_DOCKER (explicit, daemon running)"
  else
    err "CDK_DOCKER=$CDK_DOCKER set but daemon is not reachable"
  fi
elif command -v finch >/dev/null 2>&1 && finch info >/dev/null 2>&1; then
  export CDK_DOCKER=finch
  CONTAINER_ENGINE=finch
  ok "Finch daemon running (exported CDK_DOCKER=finch)"
elif command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  CONTAINER_ENGINE=docker
  ok "Docker daemon running"
else
  warn "No container engine running (docker/finch) — local pip bundling must succeed"
fi

# ── 3b. ARM64 image builds (vkg, serve, mcp) ──────────────────────────────
# Those stacks pin Platform.LINUX_ARM64 for their image assets. On an x86_64
# host without a qemu-aarch64 binfmt handler the engine pulls the arm64 base
# image and the first RUN step dies with `exec /bin/sh: exec format error` —
# ~20 minutes into the deploy, surfaced only as "Failed to build asset ...".
# Checked offline (no test image pull). On Linux the kernel's binfmt registry
# is authoritative for Docker and Finch alike. Elsewhere (Docker Desktop's VM)
# fall back to buildx, counting only platforms a *running* node detected: a
# trailing `*` (e.g. `linux/arm64*` on a stopped docker-container builder)
# marks a platform someone configured, not one the host can execute.
_arm64_binfmt_registered() {
  for _bf in "${1:-/proc/sys/fs/binfmt_misc}"/qemu-aarch64*; do
    [ -f "$_bf" ] && head -n 1 "$_bf" 2>/dev/null | grep -q '^enabled' && return 0
  done
  return 1
}
# Reads `buildx ls` output on stdin.
_buildx_lists_runnable_arm64() {
  grep -E '[[:space:]]running[[:space:]]' | grep -Eq 'linux/arm64(/v8)?([,[:space:]]|$)'
}
if [ -n "$CONTAINER_ENGINE" ]; then
  HOST_ARCH="$(uname -m)"
  HOST_OS="$(uname -s)"
  if [ "$HOST_ARCH" = "aarch64" ] || [ "$HOST_ARCH" = "arm64" ]; then
    ok "Host is $HOST_ARCH — arm64 image assets build natively"
  elif [ "$HOST_OS" = "Linux" ] && _arm64_binfmt_registered; then
    ok "arm64 emulation available on $HOST_ARCH host (qemu-aarch64 binfmt registered)"
  elif "$CONTAINER_ENGINE" buildx ls 2>/dev/null | _buildx_lists_runnable_arm64; then
    ok "arm64 emulation available on $HOST_ARCH host (buildx lists linux/arm64)"
  elif [ "$HOST_OS" = "Linux" ]; then
    err "Host is $HOST_ARCH and $CONTAINER_ENGINE cannot build linux/arm64 images (needed by the vkg, serve and mcp stacks). Register emulation: docker run --privileged --rm tonistiigi/binfmt --install arm64 — see external-docs/content/deploying.md, 'Cross-architecture container builds'"
  else
    warn "Could not confirm $CONTAINER_ENGINE can build linux/arm64 images on this $HOST_ARCH host. Verify with: $CONTAINER_ENGINE run --rm --platform linux/arm64 alpine uname -m (should print aarch64)"
  fi
fi

# ── 3c. AMD64 image builds on an arm64 host (ontology, sources tasks) ─────
# The ontology, db-enrichment, kg-build and preprocessing images pin
# linux/amd64. Without buildx, Docker's legacy builder ignores --platform and
# installs arm64 packages, failing with "does not provide the specified
# platform (linux/amd64)". Ubuntu's qemu-user-static 8.x also crashes with
# "QEMU internal SIGSEGV" running Debian trixie amd64 binaries; register a
# newer QEMU (tonistiigi/binfmt ships 10.x).
if [ "$CONTAINER_ENGINE" = "docker" ] && ! docker buildx version >/dev/null 2>&1; then
  err "docker buildx is not installed — the legacy builder cannot build linux/amd64 images on this host (Ubuntu: apt install docker-buildx)"
fi
if [ -n "$CONTAINER_ENGINE" ] && [ "$(uname -s)" = "Linux" ] \
  && { [ "$(uname -m)" = "aarch64" ] || [ "$(uname -m)" = "arm64" ]; }; then
  _x86_bf=/proc/sys/fs/binfmt_misc/qemu-x86_64
  if ! { [ -f "$_x86_bf" ] && head -n 1 "$_x86_bf" | grep -q '^enabled'; }; then
    err "No qemu-x86_64 binfmt handler — linux/amd64 images cannot build on this arm64 host. Register emulation: docker run --privileged --rm tonistiigi/binfmt --install amd64"
  else
    _x86_interp="$(sed -n 's/^interpreter //p' "$_x86_bf")"
    _x86_qemu_major="$("$_x86_interp" --version 2>/dev/null | sed -n 's/.*version \([0-9][0-9]*\)\..*/\1/p' | head -n 1)"
    if [ -n "$_x86_qemu_major" ] && [ "$_x86_qemu_major" -lt 9 ]; then
      warn "qemu-x86_64 $_x86_qemu_major.x is registered; QEMU 8.x is known to SIGSEGV building trixie amd64 images. Replace it: docker run --privileged --rm tonistiigi/binfmt --uninstall qemu-x86_64 && docker run --privileged --rm tonistiigi/binfmt --install amd64"
    else
      ok "amd64 emulation available (qemu-x86_64 binfmt registered${_x86_qemu_major:+, QEMU $_x86_qemu_major.x})"
    fi
  fi
fi

# ── 4. Smithy-generated artifacts present AND current ────────────────────
# smithy-generated/ is gitignored, so `git pull` leaves it at the previous
# commit's output. Checking only that specs exist would ship handlers against a
# stale generated model — e.g. a renamed enum member fails the Lambda at import.
# Regenerate whenever any codegen input changed since the last generation.
FINGERPRINT="$REPO_ROOT/scripts/smithy-fingerprint.sh"
SMITHY_STATE="$("$FINGERPRINT" state "$REPO_ROOT")"
case "$SMITHY_STATE" in
  current) ok "Smithy generated artifacts are current with the models" ;;
  missing) warn "smithy-generated/ is missing or empty — running 'make generate'..." ;;
  unstamped) warn "smithy-generated/ predates input fingerprinting — regenerating once with 'make generate'..." ;;
  stale) warn "smithy-generated/ is stale (Smithy models or codegen changed since it was generated) — running 'make generate'..." ;;
  *) err "Could not determine the state of smithy-generated/ ($SMITHY_STATE)" ;;
esac

if [ "$SMITHY_STATE" = missing ] || [ "$SMITHY_STATE" = unstamped ] || [ "$SMITHY_STATE" = stale ]; then
  if (cd "$REPO_ROOT" && make generate); then
    if [ "$("$FINGERPRINT" state "$REPO_ROOT")" = current ]; then
      ok "Smithy code generation complete"
    else
      err "'make generate' ran but smithy-generated/ is still not current with the models."
    fi
  else
    err "'make generate' failed. Fix Smithy/Gradle errors above before deploying."
  fi
fi

# Resolution order matches CDK's own precedence (infra/bin/app.ts uses
# CDK_DEFAULT_REGION as the source of truth) so preflight validates the
# same region CDK will actually deploy to.
REGION="${CDK_DEFAULT_REGION:-${AWS_DEFAULT_REGION:-${AWS_REGION:-us-east-1}}}"

# Value of CDK context KEY passed through SCL_EXTRA_CONTEXT (see deploy.sh) as
# `-c KEY=VALUE` / `--context KEY=VALUE`; returns 1 when KEY is not passed.
_extra_context() {
  _ec_kv="$(printf '%s\n' ${SCL_EXTRA_CONTEXT:-} | grep "^$1=" | tail -n 1)" || return 1
  printf '%s\n' "${_ec_kv#*=}"
}

# ── 5. ECR Public authentication ─────────────────────────────────────────
# The build pulls base images from ECR Public (hosted only in us-east-1,
# regardless of deploy region). We do NOT force `docker login`: a user who
# configures a credential helper for ECR Public (credHelpers "public.ecr.aws"
# -> ecr-login, or a credsStore) authenticates automatically on pull and CANNOT
# `docker login` at all — the helper has no writable credential store, so login
# exits non-zero. Forcing it turned a valid (AWS-recommended) setup into a hard
# preflight failure (issue #89). Instead: skip the explicit login when a helper
# is configured, and treat an inability to log in as a WARNING — ECR Public also
# serves anonymous (rate-limited) pulls, so a failed login must not block deploy.

# Path to the active engine's config.json (Docker honors $DOCKER_CONFIG).
_engine_config_file() {
  case "${CONTAINER_ENGINE:-}" in
    *finch*)
      for _ecr_f in "$HOME/.finch/config.json" "$HOME/.finch/.docker/config.json"; do
        [ -f "$_ecr_f" ] && { printf '%s\n' "$_ecr_f"; return 0; }
      done
      return 1
      ;;
    *)
      printf '%s\n' "${DOCKER_CONFIG:-$HOME/.docker}/config.json"
      ;;
  esac
}

# Echo the credential helper bound to public.ecr.aws, or return 1. Scope is
# deliberately the per-registry credHelper only: a plain credsStore (osxkeychain,
# desktop, …) supports `docker login` normally, so it must NOT be skipped. The
# exotic "credsStore": "ecr-login" case is still covered — its login fails and is
# handled by the non-fatal warning below, not a hard error.
_ecr_cred_helper() {
  _ecr_cfg="$(_engine_config_file)" || return 1
  [ -f "$_ecr_cfg" ] || return 1
  if command -v jq >/dev/null 2>&1; then
    _ecr_h="$(jq -r '.credHelpers["public.ecr.aws"] // empty' "$_ecr_cfg" 2>/dev/null)"
    [ -n "$_ecr_h" ] && { printf '%s\n' "$_ecr_h"; return 0; }
    return 1
  fi
  # jq-less fallback: is public.ecr.aws present as a credHelpers key?
  grep -Eq '"public\.ecr\.aws"[[:space:]]*:' "$_ecr_cfg" 2>/dev/null && { printf 'credHelper\n'; return 0; }
  return 1
}

# AWS China credentials cannot get an ECR Public token (us-east-1 is another
# partition); anonymous pulls work from China, so there is nothing to log in to.
if [[ "$REGION" == cn-* ]]; then
  ok "ECR Public: anonymous base image pulls from $REGION — skipping explicit login"
elif [ -n "${CONTAINER_ENGINE:-}" ]; then
  if command -v aws >/dev/null 2>&1 && aws sts get-caller-identity >/dev/null 2>&1; then
    if ECR_HELPER="$(_ecr_cred_helper)"; then
      ok "ECR Public auth handled by credential helper ($ECR_HELPER) — skipping explicit login"
    else
      # Split the token fetch from the login so the RIGHT error surfaces (a pipe
      # would either discard the AWS error via 2>/dev/null, or feed it to docker
      # as the password via 2>&1). Failure is a warning, not a fatal error.
      _ecr_awserr="$(mktemp)"
      if _ecr_pw="$(aws ecr-public get-login-password --region us-east-1 2>"$_ecr_awserr")"; then
        ECR_LOGIN_ERR="$(printf '%s' "$_ecr_pw" \
          | "$CONTAINER_ENGINE" login --username AWS --password-stdin public.ecr.aws 2>&1)" \
          && ok "Authenticated to ECR Public (us-east-1)" \
          || warn "Could not authenticate to ECR Public; base image pulls fall back to anonymous (rate-limited). Detail: ${ECR_LOGIN_ERR:-unknown error}"
      else
        warn "Could not obtain an ECR Public token from AWS; base image pulls fall back to anonymous (rate-limited). Detail: $(cat "$_ecr_awserr" 2>/dev/null || echo 'unknown error')"
      fi
      rm -f "$_ecr_awserr"
    fi
  else
    warn "AWS credentials not available — skipping ECR Public authentication"
  fi
else
  warn "No container engine — skipping ECR Public authentication"
fi

# ── 6. VPC limit check (requires AWS credentials) ────────────────────────
# A redeploy reuses the VPC the network stack already created (and SCL_VPC_ID
# imports one), so only a first deploy needs a free VPC slot.
_COA_VPC_NAME="${SCL_PREFIX:-coa}-${ENV:-dev}-vpc"
if [ -n "${SCL_VPC_ID:-}" ]; then
  ok "Importing VPC ${SCL_VPC_ID} — no new VPC needed"
elif command -v aws >/dev/null 2>&1 && aws sts get-caller-identity >/dev/null 2>&1 \
  && [ "$(aws ec2 describe-vpcs --region "$REGION" --filters "Name=tag:Name,Values=$_COA_VPC_NAME" \
          --query 'length(Vpcs)' --output text 2>/dev/null || echo 0)" -gt 0 ]; then
  ok "VPC $_COA_VPC_NAME already exists in $REGION — no new VPC needed"
elif command -v aws >/dev/null 2>&1 && aws sts get-caller-identity >/dev/null 2>&1; then
  VPC_COUNT=$(aws ec2 describe-vpcs --region "$REGION" --query 'length(Vpcs)' --output text 2>/dev/null || echo "?")
  VPC_LIMIT_RAW=$(aws service-quotas get-service-quota --service-code vpc --quota-code L-F678F1CE --region "$REGION" --query 'Quota.Value' --output text 2>/dev/null || echo "5")
  VPC_LIMIT="${VPC_LIMIT_RAW%%.*}"
  [[ "$VPC_LIMIT" =~ ^[0-9]+$ ]] || VPC_LIMIT=5
  if [ "$VPC_COUNT" != "?" ]; then
    if [ "$VPC_COUNT" -ge "$VPC_LIMIT" ]; then
      err "VPC limit reached: $VPC_COUNT/$VPC_LIMIT VPCs in $REGION."
      err "Delete unused VPCs or request a quota increase before deploying."
    else
      ok "VPC headroom: $VPC_COUNT/$VPC_LIMIT used in $REGION"
    fi
  fi
else
  warn "AWS credentials not available — skipping VPC limit check"
fi

# ── 7. Lambda reserved-concurrency headroom (requires AWS credentials) ────
# The VKG-reload and doc-preprocessing Lambdas each reserve
# `lambda_reserved_concurrency` (default 5) executions. Lambda refuses ANY
# reservation that would drop account-wide unreserved concurrency below its
# floor of 10 — the reduced default AWS applies to some new accounts. We check
# ACTUAL unreserved headroom (not the raw L-B99A9384 quota) because other
# functions' existing reservations already reduce it. Without this, deploy runs
# ~30 min then fails and rolls back on coa-dev-vkg / coa-dev-sources.
RESERVED_PER_FN="${SCL_LAMBDA_RESERVED_CONCURRENCY:-5}"
if [[ "$RESERVED_PER_FN" =~ ^[0-9]+$ ]] && [ "$RESERVED_PER_FN" -gt 0 ]; then
  if command -v aws >/dev/null 2>&1 && aws sts get-caller-identity >/dev/null 2>&1; then
    NUM_RESERVED_FNS=2 # VkgReloadFn + SourcesPreProcessingFn
    NEEDED=$((RESERVED_PER_FN * NUM_RESERVED_FNS))
    MIN_UNRESERVED=10 # Lambda's hard floor; binding on reduced-quota accounts
    UNRESERVED=$(aws lambda get-account-settings --region "$REGION" \
      --query 'AccountLimit.UnreservedConcurrentExecutions' --output text 2>/dev/null || echo "?")
    if [[ "$UNRESERVED" =~ ^[0-9]+$ ]]; then
      if [ "$((UNRESERVED - NEEDED))" -lt "$MIN_UNRESERVED" ]; then
        err "Lambda concurrency headroom too low in $REGION: $UNRESERVED unreserved, deployment reserves $NEEDED."
        err "Lambda rejects reservations that leave fewer than $MIN_UNRESERVED unreserved executions account-wide."
        err "Fix: request an increase for quota L-B99A9384, or deploy without reservations:"
        err "  SCL_LAMBDA_RESERVED_CONCURRENCY=0 make deploy-dev"
      else
        ok "Lambda concurrency headroom: $UNRESERVED unreserved, reserving $NEEDED in $REGION"
      fi
    else
      warn "Could not read Lambda account settings — skipping concurrency headroom check"
    fi
  else
    warn "AWS credentials not available — skipping Lambda concurrency check"
  fi
else
  ok "Lambda reserved concurrency disabled (lambda_reserved_concurrency=0) — skipping headroom check"
fi

# ── 8. CDK bootstrap in every region the deploy touches ───────────────────
# The *-edge-waf stack ALWAYS deploys to us-east-1 (CloudFront-scope WAF WebACLs
# and CloudFront ACM certs exist only there), so a non-us-east-1 deploy needs
# BOTH regions bootstrapped. Without this the deploy runs for a long while and
# then fails on that one stack with a bootstrap error — the docs showed a single
# `cdk bootstrap <REGION>` and never mentioned the second one.
# cn- regions deploy no *-edge-waf stack (CDK context edge_waf defaults off
# there: us-east-1 is outside the aws-cn partition), so they need only REGION.
if command -v aws >/dev/null 2>&1 && aws sts get-caller-identity >/dev/null 2>&1; then
  _bootstrap_regions="$REGION"
  _edge_waf="$(_extra_context edge_waf)" || _edge_waf=false
  _edge_waf="$(printf '%s' "$_edge_waf" | tr '[:upper:]' '[:lower:]')"
  if [[ "$REGION" == cn-* ]] && [[ "$_edge_waf" =~ ^(false|0|off|no)$ ]]; then
    ok "No *-edge-waf stack in $REGION (edge_waf off) — us-east-1 bootstrap not needed"
  elif [ "$REGION" != "us-east-1" ]; then
    _bootstrap_regions="$REGION us-east-1"
  fi
  for _bsr in $_bootstrap_regions; do
    if aws cloudformation describe-stacks --stack-name CDKToolkit --region "$_bsr" \
        >/dev/null 2>&1; then
      ok "CDK bootstrapped in $_bsr"
    else
      err "CDK is not bootstrapped in $_bsr."
      if [ "$_bsr" = "us-east-1" ] && [ "$REGION" != "us-east-1" ]; then
        err "us-east-1 is always required — the *-edge-waf stack deploys there regardless of your deploy region."
      fi
      err "Run: npx cdk bootstrap aws://\$(aws sts get-caller-identity --query Account --output text)/$_bsr"
    fi
  done
else
  warn "AWS credentials not available — skipping CDK bootstrap check"
fi

# ── 9. Effective Bedrock model IDs are invocable from the deploy region ───
# Two checks, with different strengths, on the model IDs the stacks will
# actually deploy — the value in /{prefix}/config when set, else the built-in
# default (#1020: with no config every default is a `us.` profile, so a
# non-US deploy reaches CREATE_COMPLETE and fails at the first Bedrock call).
#
#   ERROR  a geographic inference profile the deploy region's geography does
#          not publish (`us.` in ap-northeast-1). Deterministic; bin/app.ts
#          fails synth on the same condition, this is the earlier, friendlier
#          report.
#   WARN   the model is not listed in the region. Availability is
#          account-scoped and a bare in-region ID lives in a different API
#          than a geographic profile, so a hard failure here could block a
#          valid deploy.
#
# The defaults MUST match libs/ts-shared/src/constants.ts — pinned by
# infra/test/model-id-defaults.test.ts. Node is already a required deploy
# prerequisite, so use it here instead of making model validation depend on jq.
_DEFAULT_LLM_MODEL_ID="us.anthropic.claude-sonnet-5"
_DEFAULT_EMBED_MODEL_ID="us.cohere.embed-v4:0"
_DEFAULT_INDUCTION_MODEL_ID="us.anthropic.claude-sonnet-5"
_DEFAULT_CHAT_MODEL_ID="us.anthropic.claude-haiku-4-5-20251001-v1:0"
_CONFIG_PARAM="/${SCL_PREFIX:-coa}/config"

# LLM backend — llm_provider in bin/app.ts: the CDK context value when passed,
# else openai_compatible in cn- regions (AWS China has no Bedrock), else bedrock.
if _LLM_PROVIDER="$(_extra_context llm_provider)"; then
  _LLM_PROVIDER="$(printf '%s' "$_LLM_PROVIDER" | tr '[:upper:]' '[:lower:]')"
elif [[ "$REGION" == cn-* ]]; then
  _LLM_PROVIDER="openai_compatible"
else
  _LLM_PROVIDER="bedrock"
fi

# Region the stacks invoke Bedrock in — bedrock_region in bin/app.ts: the CDK
# context value when passed (empty or `none` = the deploy region), else the
# deploy region (no cn- default: a China deployment must not call Bedrock in a
# commercial region).
if _BEDROCK_REGION="$(_extra_context bedrock_region)"; then
  case "$(printf '%s' "$_BEDROCK_REGION" | tr '[:upper:]' '[:lower:]')" in
    ""|none) _BEDROCK_REGION="$REGION" ;;
  esac
else
  _BEDROCK_REGION="$REGION"
fi

# Geographies (as inference-profile prefixes) whose profiles are published in
# a region. Mirrors regionGeographies() in infra/lib/utils/model-region.ts —
# infra/test/model-id-defaults.test.ts runs this function and compares.
_coa_region_geographies() {
  case "$1" in
    us-gov-*) echo "us-gov" ;;
    cn-*) echo "" ;;
    us-*) echo "us global" ;;
    eu-*) echo "eu global" ;;
    ca-*) echo "ca global" ;;
    ap-northeast-1|ap-northeast-3) echo "jp apac global" ;;
    ap-southeast-2|ap-southeast-4) echo "au apac global" ;;
    me-central-1) echo "apac global" ;;
    ap-*) echo "apac global" ;;
    *) echo "global" ;;
  esac
}

# The leading geography of an inference-profile ID (3+ dot segments, known
# prefix), else nothing — bare IDs, ARNs and unknown geographies are not judged.
_coa_model_geo_prefix() {
  case "$1" in
    *.*.*) ;;
    *) return ;;
  esac
  _p="$(printf '%s' "$1" | cut -d. -f1 | tr '[:upper:]' '[:lower:]')"
  case "$_p" in
    us|eu|apac|jp|au|ca|us-gov|global) echo "$_p" ;;
  esac
}

_coa_resolve_model_ids() {
  _COA_DEFAULT_LLM_MODEL_ID="$_DEFAULT_LLM_MODEL_ID" \
  _COA_DEFAULT_EMBED_MODEL_ID="$_DEFAULT_EMBED_MODEL_ID" \
  _COA_DEFAULT_INDUCTION_MODEL_ID="$_DEFAULT_INDUCTION_MODEL_ID" \
  _COA_DEFAULT_CHAT_MODEL_ID="$_DEFAULT_CHAT_MODEL_ID" \
  node -e '
    const fail = (message) => {
      console.error(message);
      process.exit(1);
    };

    let config;
    try {
      config = JSON.parse(process.argv[1]);
    } catch (error) {
      fail(error instanceof Error ? error.message : String(error));
    }
    if (config === null || Array.isArray(config) || typeof config !== "object") {
      fail("config root must be an object");
    }

    const defaults = [
      ["bedrockLlmModelId", process.env._COA_DEFAULT_LLM_MODEL_ID],
      ["bedrockEmbedModelId", process.env._COA_DEFAULT_EMBED_MODEL_ID],
      ["bedrockInductionLlmModelId", process.env._COA_DEFAULT_INDUCTION_MODEL_ID],
      ["bedrockChatModelId", process.env._COA_DEFAULT_CHAT_MODEL_ID],
    ];
    const rows = defaults.map(([key, fallback]) => {
      const value = Object.prototype.hasOwnProperty.call(config, key)
        ? config[key]
        : null;
      if (value === null) {
        return [key, fallback, "default"];
      }
      if (typeof value !== "string") {
        fail(`${key} must be a string`);
      }
      if (/^\s*$/.test(value)) {
        return [key, fallback, "default"];
      }
      if (/\s/.test(value)) {
        fail(`${key} must not contain whitespace`);
      }
      return [key, value, "config"];
    });
    process.stdout.write(`${rows.map((row) => row.join("\t")).join("\n")}\n`);
  ' "$1"
}

if [ "$_LLM_PROVIDER" = "openai_compatible" ]; then
  # bin/app.ts skips the same check: OPENAI_COMPAT_* map the Bedrock-style IDs
  # to provider model names.
  ok "LLM calls use the OpenAI-compatible provider (llm_provider=openai_compatible) — skipping Bedrock model-ID validation"
elif command -v aws >/dev/null 2>&1 \
    && aws sts get-caller-identity >/dev/null 2>&1; then
  if _cfg_json="$(aws ssm get-parameter --name "$_CONFIG_PARAM" --region "$REGION" \
      --query 'Parameter.Value' --output text 2>&1)"; then
    :
  elif printf '%s' "$_cfg_json" | grep -q 'ParameterNotFound'; then
    warn "$_CONFIG_PARAM not found in $REGION — every Bedrock model ID falls back to its built-in default."
    _cfg_json="{}"
  else
    err "Could not read $_CONFIG_PARAM in $REGION — model-ID validation cannot proceed."
    echo "       detail: $_cfg_json" >&2
    _cfg_json=""
  fi
  if [ -n "$_cfg_json" ]; then
    # One line per key: key<TAB>effective id<TAB>origin
    if _effective="$(_coa_resolve_model_ids "$_cfg_json" 2>&1)"; then
      :
    else
      err "$_CONFIG_PARAM is not valid deployment JSON — model-ID validation cannot proceed."
      echo "       detail: $_effective" >&2
      _effective=""
    fi
  fi
  if [ -n "${_effective:-}" ]; then
    _geos="$(_coa_region_geographies "$_BEDROCK_REGION")"
    _profiles=""
    _models=""
    if [ "$_BEDROCK_REGION" = "$REGION" ]; then
      _profiles="$(aws bedrock list-inference-profiles --region "$REGION" \
        --query 'inferenceProfileSummaries[].inferenceProfileId' --output text 2>/dev/null || echo "")"
      _models="$(aws bedrock list-foundation-models --region "$REGION" \
        --query 'modelSummaries[].modelId' --output text 2>/dev/null || echo "")"
      if [ -z "$_profiles" ] && [ -z "$_models" ]; then
        warn "Could not list Bedrock models in $REGION — skipping the availability check"
      fi
    else
      # Cross-region Bedrock authenticates with a Bedrock API key, not the
      # deploy credentials, so those cannot be relied on to list the models.
      ok "Bedrock is invoked in $_BEDROCK_REGION (bedrock_region) — validating model IDs against it, skipping the model listing"
    fi
    while IFS="$(printf '\t')" read -r _key _mid _origin; do
      [ -n "$_key" ] || continue
      case "$_origin" in
        config) _from="from $_CONFIG_PARAM" ;;
        *) _from="built-in default; $_CONFIG_PARAM does not set $_key" ;;
      esac
      _geo="$(_coa_model_geo_prefix "$_mid")"
      if [ -n "$_geo" ] && ! printf ' %s ' "$_geos" | grep -qF " $_geo "; then
        err "$_key '$_mid' is a '$_geo.' inference profile, which Bedrock does not publish in $_BEDROCK_REGION ($_from)."
        echo "       fix: set $_key in $_CONFIG_PARAM to a profile published in $_BEDROCK_REGION (${_geos:-no geographic profiles here}) or the bare in-region model ID." >&2
        continue
      fi
      if [ -n "$_profiles" ] || [ -n "$_models" ]; then
        if printf '%s %s' "$_profiles" "$_models" | grep -qF "$_mid"; then
          ok "Bedrock model available in $REGION: $_key=$_mid ($_origin)"
        else
          warn "Bedrock model '$_mid' ($_key, $_from) not found in $REGION."
          warn "  It may be account-scoped or not yet enabled; if it is a cross-geography profile it fails at"
          warn "  first invocation with ValidationException. See 'Where the model IDs live' in external-docs/content/deploying.md."
        fi
      fi
    done <<EOF_EFFECTIVE
$_effective
EOF_EFFECTIVE
  fi
fi

# ── 10. Stale cdk.context.json ───────────────────────────────────────────
if [ -f "$REPO_ROOT/infra/cdk.context.json" ]; then
  warn "infra/cdk.context.json exists — cached lookups may be stale."
  warn "If deploy fails with 'resource not found', delete it: rm infra/cdk.context.json"
fi

# ── Summary ──────────────────────────────────────────────────────────────
echo ""
if [ $ERRORS -gt 0 ]; then
  echo "PREFLIGHT FAILED: $ERRORS error(s) found. Fix before deploying."
  exit 1
else
  echo "Preflight passed."
fi
