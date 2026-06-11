"""Scoped API-key credentials for the REST surface.

See :mod:`trellis.auth.api_keys` for token format and verification
semantics, and ``trellis_api.auth`` for the FastAPI dependency layer.
"""

from trellis.auth.api_keys import (
    ALL_SCOPES,
    SCOPE_ADMIN,
    SCOPE_INGEST,
    SCOPE_MUTATE,
    SCOPE_READ,
    TOKEN_PREFIX,
    generate_api_key,
    hash_secret,
    parse_token,
    scopes_satisfy,
    verify_token,
)

__all__ = [
    "ALL_SCOPES",
    "SCOPE_ADMIN",
    "SCOPE_INGEST",
    "SCOPE_MUTATE",
    "SCOPE_READ",
    "TOKEN_PREFIX",
    "generate_api_key",
    "hash_secret",
    "parse_token",
    "scopes_satisfy",
    "verify_token",
]
