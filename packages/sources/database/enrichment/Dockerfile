# Build context must be the repo root:
#   docker build -f packages/sources/database/enrichment/Dockerfile .
# Pin to digest to prevent supply-chain attacks via tag mutation. The pin is for
# supply-chain integrity only, NOT patch currency — see the apt-get upgrade below.
# python:3.12-slim, digest resolved from the tag on 2026-09-01.
FROM --platform=linux/amd64 public.ecr.aws/docker/library/python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f

# Optional package-mirror overrides for builds on hosts where the public
# indexes are slow or unreachable (e.g. AWS China), passed by CDK as build args
# from the build_* context keys. The defaults keep the stock sources: an empty
# APT_MIRROR leaves the Debian sources untouched, and pip already defaults to
# https://pypi.org/simple. Build args are visible to RUN as environment
# variables but are not persisted in the image.
ARG APT_MIRROR=
ARG PIP_INDEX_URL=https://pypi.org/simple

WORKDIR /app

# Apply Debian security patches on top of the pinned base. This image installs no
# apt packages of its own, so without this layer its OS patch level was frozen at
# whatever the pinned base shipped — which is how it ended up on openssl
# 3.5.6-1~deb13u2 with 10 open Inspector findings. `upgrade` (not `dist-upgrade`)
# keeps this to security and point-release updates from the trixie suites only.
RUN if [ -n "$APT_MIRROR" ]; then \
      sed -i "s|http://deb.debian.org/|https://${APT_MIRROR}/|g" /etc/apt/sources.list.d/debian.sources && \
      grep -q "https://${APT_MIRROR}/" /etc/apt/sources.list.d/debian.sources; \
    fi && \
    apt-get update && \
    apt-get upgrade -y && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Install shared common library
COPY libs/common/ /tmp/common/
RUN pip install --no-cache-dir /tmp/common/ && rm -rf /tmp/common/

# Install smithy-generated models
COPY smithy-generated/control-plane-python-server/ /tmp/server/
RUN pip install --no-cache-dir /tmp/server/ && rm -rf /tmp/server/

# Install the database package, then remediate scanner findings in the SAME final layer:
# upgrade pip + setuptools (CVE-2025-47273; build toolchain, not a declared dep), and
# REMOVE msgpack — an orphan that nothing imports or declares (verified via grep +
# pipdeptree --reverse), pulled in transiently and left behind. `pip uninstall -y` is a
# no-op exit 0 if it is absent, so this stays safe.
COPY packages/sources/ /tmp/sources/
RUN pip install --no-cache-dir /tmp/sources/ && rm -rf /tmp/sources/ && \
    pip install --no-cache-dir --upgrade pip "setuptools>=78.1.1" && \
    pip uninstall -y msgpack

USER 1000
ENTRYPOINT ["python", "-m", "coa_sources.database.pipeline.enrichment_handler"]
