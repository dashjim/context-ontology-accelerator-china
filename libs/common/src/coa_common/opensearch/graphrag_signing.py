# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Make graphrag-toolkit sign for a managed OpenSearch domain.

graphrag-toolkit's ``create_os_client`` / ``create_os_async_client`` hard-code
the SigV4 service ``aoss``, so against a managed OpenSearch Service domain
(``vector_store=opensearch``, AWS China) every request is rejected. The
toolkit's AOSS-safe request shapes (no client ``_id``, no explicit refresh)
work unchanged on a domain, so only the signer needs to change.
"""

from __future__ import annotations

import logging

from coa_common.config import opensearch_signing_service

log = logging.getLogger(__name__)


def patch_graphrag_signing_service(endpoint: str) -> bool:
    """Sign graphrag-toolkit's OpenSearch clients for ``endpoint``'s service.

    No-op (returns False) for an AOSS endpoint or when graphrag-toolkit is not
    installed; otherwise rebinds the toolkit's signer classes so every client
    it creates afterwards signs for ``es``, and returns True. Idempotent.
    Applies process-wide — one vector store per process.
    """
    service = opensearch_signing_service(endpoint)
    if service == "aoss":
        return False
    try:
        import graphrag_toolkit.lexical_graph.storage.vector.opensearch_vector_indexes as ovi
        from opensearchpy import AWSV4SignerAsyncAuth, Urllib3AWSV4SignerAuth
    except ImportError:
        log.debug("graphrag_toolkit not installed; signing patch skipped")
        return False

    def _sync_auth(credentials, region, _service="aoss"):
        return Urllib3AWSV4SignerAuth(credentials, region, service)

    def _async_auth(credentials, region, _service="aoss"):
        return AWSV4SignerAsyncAuth(credentials, region, service)

    ovi.Urllib3AWSV4SignerAuth = _sync_auth
    ovi.AWSV4SignerAsyncAuth = _async_auth
    log.info("graphrag_toolkit OpenSearch clients now sign for service %s", service)
    return True
