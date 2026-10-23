# Pin to digest to prevent supply-chain attacks via tag mutation.
# The AWS Lambda base freezes its dnf repos to the base's build-time snapshot, so an
# in-image `dnf upgrade` is a no-op — patch currency comes ONLY from re-resolving this
# digest to a newer base that AWS has rebuilt. Digest re-resolved from the
# public.ecr.aws/lambda/python:3.12 tag on 2026-09-17 to clear the AL2023 expat /
# openssl / libtiff HIGH CVEs Trivy/Inspector flagged against the prior 2026-05-13 base.
FROM --platform=linux/amd64 public.ecr.aws/lambda/python:3.12@sha256:a32fd0af1505dad402a3a8c2bc1766a81b292f6869f13f1239d6c28b57b434c7

# Optional package-mirror overrides for builds on hosts where the public
# indexes are slow or unreachable (e.g. AWS China), passed by CDK as build args
# from the build_* context keys. The defaults keep today's sources: pip already
# defaults to https://pypi.org/simple, and CPU torch comes from the PyTorch CPU
# index. A non-empty TORCH_FIND_LINKS replaces that index with a flat
# find-links page of torch wheels (pip --find-links). Build args are visible to
# RUN as environment variables but are not persisted in the image.
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
ARG TORCH_FIND_LINKS=
# `spacy download` fetches from raw.githubusercontent.com, which is unreachable
# from some hosts (AWS China). A model wheel placed next to this Dockerfile
# (en_core_web_sm-*.whl, git-ignored) is installed instead, after checking it
# against the official spacy-models release hash below.
ARG SPACY_MODEL_SHA256=1932429db727d4bff3deed6b34cfc05df17794f4a52eeb26cf8928f7c1a0fb85

# System deps: poppler for PDF text extraction, file-libs for libmagic (file type detection)
RUN dnf install -y poppler-utils file-libs && dnf clean all

# Install shared common library (changes less often -> better layer caching)
COPY libs/common/ /tmp/common/
RUN pip install --no-cache-dir --root-user-action=ignore /tmp/common/ && rm -rf /tmp/common/

# CPU-only PyTorch — unstructured[pdf] pulls in torch via unstructured-inference
# (requires torch>=2.10.0). Without constraints, pip installs the full CUDA
# variant (~4 GB nvidia-* packages). Pre-install CPU torch, then constrain the
# requirements install so pip can't upgrade it.
COPY packages/sources/documents/preprocessing/constraints.txt /tmp/constraints.txt
RUN if [ -n "$TORCH_FIND_LINKS" ]; then set -- --find-links "$TORCH_FIND_LINKS"; \
    else set -- --extra-index-url "$TORCH_INDEX_URL"; fi && \
    pip install --no-cache-dir --root-user-action=ignore \
    torch==2.11.0+cpu \
    "$@"

# Install remaining deps (constrained to keep CPU torch), then:
#  - Swap opencv-python → headless (unstructured pulls in opencv which needs libGL, absent in Lambda)
#  - Ensure spaCy is present (newer unstructured releases no longer pin it as a hard
#    dependency, so install it explicitly before downloading the model — otherwise
#    `spacy download` fails with "No module named spacy" under a constrained install)
#  - Pre-install spaCy model (unstructured loads it at runtime but Lambda fs is read-only)
COPY packages/sources/documents/preprocessing/requirements.txt packages/sources/documents/preprocessing/en_core_web_sm-*.whl ${LAMBDA_TASK_ROOT}/
RUN if [ -n "$TORCH_FIND_LINKS" ]; then set -- --find-links "$TORCH_FIND_LINKS"; \
    else set -- --extra-index-url "$TORCH_INDEX_URL"; fi && \
    pip install --no-cache-dir --root-user-action=ignore \
    -c /tmp/constraints.txt \
    "$@" \
    -r ${LAMBDA_TASK_ROOT}/requirements.txt && \
    pip uninstall -y opencv-python 2>/dev/null; \
    pip install --no-cache-dir --root-user-action=ignore opencv-python-headless && \
    pip install --no-cache-dir --root-user-action=ignore spacy && \
    if ls ${LAMBDA_TASK_ROOT}/en_core_web_sm-*.whl >/dev/null 2>&1; then \
      whl=$(ls ${LAMBDA_TASK_ROOT}/en_core_web_sm-*.whl) && \
      echo "${SPACY_MODEL_SHA256}  ${whl}" | sha256sum -c - && \
      pip install --no-cache-dir --root-user-action=ignore "${whl}" && rm -f "${whl}"; \
    else python -m spacy download en_core_web_sm; fi

# Install the documents package so handler can be imported as a module
COPY smithy-generated/control-plane-python-server/ /tmp/server/
RUN pip install --no-cache-dir --root-user-action=ignore /tmp/server/ && rm -rf /tmp/server/
COPY packages/sources/ /tmp/sources/
# Install the documents package, then upgrade pip to clear its own advisories.
# Unlike the Debian service images, setuptools/msgpack are NOT bumped here: this
# Lambda base already ships setuptools 81.x (past the CVE-2025-47273 fix), msgpack is
# not installed, and a later-layer setuptools upgrade only adds a lower-layer scanner
# artifact without improving the deployable image.
RUN pip install --no-cache-dir --root-user-action=ignore /tmp/sources/ && rm -rf /tmp/sources/ && \
    pip install --no-cache-dir --root-user-action=ignore --upgrade pip

# numba's @njit(cache=True) needs a writable cache dir (/tmp is writable on Lambda)
ENV NUMBA_CACHE_DIR=/tmp/numba_cache
# Suppress matplotlib/fontconfig warnings about read-only home dir
ENV MPLCONFIGDIR=/tmp/matplotlib FONTCONFIG_PATH=/tmp/fonts

USER 1000
CMD ["coa_sources.documents.preprocessing.handler.handler"]
