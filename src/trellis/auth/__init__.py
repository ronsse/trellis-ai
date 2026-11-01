"""Scoped API-key credentials for the REST surface.

See :mod:`trellis.auth.api_keys` for token format and verification
semantics, and ``trellis_api.auth`` for the FastAPI dependency layer.
"""

from trellis.auth.api_keys import (
    ALL_SCOPES,
    KEY_PRESETS,
    KEY_SCOPES,
    SCOPE_ADMIN,
    SCOPE_CONFIDENTIAL,
    SCOPE_EXTERNAL,
    SCOPE_INGEST,
    SCOPE_MUTATE,
    SCOPE_READ,
    SCOPE_RESTRICTED,
    SHOWN_ONCE_WARNING,
    TOKEN_PREFIX,
    api_key_public_row,
    clearance_of,
    external_scope_refusal,
    generate_api_key,
    hash_secret,
    parse_scope_list,
    parse_token,
    scopes_satisfy,
    verify_token,
)

__all__ = [
    "ALL_SCOPES",
    "KEY_PRESETS",
    "KEY_SCOPES",
    "SCOPE_ADMIN",
    "SCOPE_CONFIDENTIAL",
    "SCOPE_EXTERNAL",
    "SCOPE_INGEST",
    "SCOPE_MUTATE",
    "SCOPE_READ",
    "SCOPE_RESTRICTED",
    "TOKEN_PREFIX",
    "SHOWN_ONCE_WARNING",
    "api_key_public_row",
    "clearance_of",
    "external_scope_refusal",
    "generate_api_key",
    "hash_secret",
    "parse_scope_list",
    "parse_token",
    "scopes_satisfy",
    "verify_token",
]
