"""Per-consumer LLM routing: named tiers and the routes that select them.

One ``llm:`` block used to feed every LLM call site, so the reconcile
judge, memory extraction, enrichment, precedent mining, the shadow
classifier and the session-capture judge all ran on one model. This module
lets a deployment give each of them its own model, endpoint or credential::

    llm:
      provider: openai                  # the parent block, unchanged
      base_url: http://localhost:11434/v1
      api_key_env: OLLAMA_API_KEY
      model: hermes3:8b
      tiers:
        deep:
          base_url: http://localhost:4000/v1
          api_key_env: LITELLM_API_KEY  # a moved endpoint names its own key
          model: deep
      routes:
        reconcile: deep
        precedent_mining: deep

Three rules carry the design.

* **An unrouted consumer gets the parent block exactly**, so a config with
  no ``routes:`` builds the clients it always built.
* **A tier inherits field by field, credentials excepted.** ``provider``,
  ``base_url`` and ``model`` fall back to the parent when the tier omits the
  key; an explicit ``null`` is an override, not an omission. ``api_key`` and
  ``api_key_env`` are one unit: a tier that sets either inherits neither, and
  a tier that moves the endpoint (a different ``provider`` or ``base_url``)
  must name its own, because inheriting would send one service's key to
  another. The same hazard runs the other way, so a tier that changes
  ``provider`` under a parent with a ``base_url`` must set its own
  ``base_url`` (``null`` for the provider's default).
* **A defect in the block raises** :class:`LLMRoutingError` at build time,
  for every consumer, rather than degrading any of them to the parent block.
  Falling back is how a consumer the operator pinned to one model silently
  runs on another. Validation is lazy (never at registry load), so a typo
  here cannot take down a surface that makes no LLM call.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

from trellis.errors import ConfigError

__all__ = [
    "CREDENTIAL_KEYS",
    "ENDPOINT_KEYS",
    "LLM_BLOCK_KEYS",
    "LLMConsumer",
    "LLMRoute",
    "LLMRoutingError",
    "resolve_llm_route",
    "validate_llm_routing",
]


class LLMConsumer(StrEnum):
    """Every in-repo call site that builds an LLM client from config.

    A member's value is its key under ``llm.routes``. The set is closed on
    purpose: ``tests/unit/test_llm_consumer_rule.py`` requires every
    ``build_llm_client(...)`` call in ``src/`` to name one, so a new call
    site cannot quietly share another consumer's tier.
    """

    MEMORY_EXTRACTION = "memory_extraction"
    RECONCILE = "reconcile"
    CLASSIFY_SHADOW = "classify_shadow"
    ENRICHMENT = "enrichment"
    PRECEDENT_MINING = "precedent_mining"
    SESSION_CAPTURE = "session_capture"


#: Keys a tier may set; the same fields the parent block's builder reads.
LLM_BLOCK_KEYS: frozenset[str] = frozenset(
    {"provider", "api_key", "api_key_env", "base_url", "model"}
)
#: Inherited as one unit, never field by field.
CREDENTIAL_KEYS: tuple[str, str] = ("api_key", "api_key_env")
#: A tier that changes either of these talks to a different service.
ENDPOINT_KEYS: tuple[str, str] = ("provider", "base_url")

_TIERS_KEY = "tiers"
_ROUTES_KEY = "routes"


class LLMRoutingError(ConfigError):
    """The ``llm.tiers`` / ``llm.routes`` block cannot be resolved.

    A :class:`~trellis.errors.ConfigError`, so the CLI boundary renders it
    with the deployment-state exit code instead of a traceback. ``setting``
    names the YAML path to edit, e.g. ``llm.routes.reconcile``.
    """


@dataclass(frozen=True)
class LLMRoute:
    """The effective configuration one consumer builds its client from.

    ``tier`` is ``None`` when the consumer is unrouted and ``block`` is the
    parent ``llm:`` block itself. ``block`` can hold a literal API key, so it
    is kept out of ``repr``; use :meth:`describe` for anything printed.
    """

    consumer: LLMConsumer | None
    tier: str | None
    block: Mapping[str, Any] = field(repr=False)

    @property
    def routed(self) -> bool:
        """``True`` when ``llm.routes`` pins this consumer to a tier."""
        return self.tier is not None

    @property
    def provider(self) -> str | None:
        return _optional_str(self.block.get("provider"))

    @property
    def model(self) -> str | None:
        return _optional_str(self.block.get("model"))

    @property
    def base_url(self) -> str | None:
        return _optional_str(self.block.get("base_url"))

    def credential_source(
        self, environ: Mapping[str, str] | None = None
    ) -> Literal["env", "literal"] | None:
        """Which credential a build would use right now, without reading it out.

        Mirrors the registry's precedence: a set ``api_key_env`` wins, a
        literal ``api_key`` is the fallback, and ``None`` means the build
        has no key and will return no client.
        """
        env = os.environ if environ is None else environ
        env_name = self.block.get("api_key_env")
        if env_name and env.get(str(env_name)):
            return "env"
        if self.block.get("api_key"):
            return "literal"
        return None

    def describe(self, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
        """A JSON-safe summary that names the credential's source, never its value.

        ``base_url`` has any ``user:password@`` part masked, since a URL is
        the one other field a credential can hide in.
        """
        env_name = self.block.get("api_key_env")
        return {
            "consumer": None if self.consumer is None else self.consumer.value,
            "tier": self.tier,
            "provider": self.provider,
            "model": self.model,
            "base_url": _display_url(self.base_url),
            "api_key_env": None if not env_name else str(env_name),
            "credential_source": self.credential_source(environ),
        }


def validate_llm_routing(llm_config: Mapping[str, Any] | None) -> None:
    """Raise :class:`LLMRoutingError` if ``llm.tiers`` or ``llm.routes`` is malformed.

    Checks the whole block, not just the route being resolved, so a typo in
    a route nobody has exercised yet still surfaces on the first build.
    A ``tiers:`` or ``routes:`` key left with every entry commented out
    parses as ``null`` and means "none"; a single tier left empty that way
    is an error, because routing to it would silently build the parent.
    """
    parent = llm_config or {}
    tiers = _tiers(parent)
    routes = _routes(parent)
    parent_endpoint = {key: parent.get(key) for key in ENDPOINT_KEYS}

    for name, body in tiers.items():
        _validate_tier(str(name), body, parent_endpoint)
    known = {member.value for member in LLMConsumer}
    for consumer, tier in routes.items():
        _validate_route(str(consumer), tier, tiers, known)


def _validate_tier(name: str, body: Any, parent_endpoint: Mapping[str, Any]) -> None:
    setting = f"llm.tiers.{name}"
    if not isinstance(body, Mapping):
        shape = "empty (null)" if body is None else type(body).__name__
        msg = (
            f"LLM tier {name!r} must be a mapping of {sorted(LLM_BLOCK_KEYS)},"
            f" got {shape}. Give it at least the keys that differ from the"
            f" parent 'llm:' block, or use '{{}}' to alias the parent."
        )
        raise LLMRoutingError(msg, setting=setting)
    unknown = sorted(str(key) for key in body if key not in LLM_BLOCK_KEYS)
    if unknown:
        msg = (
            f"LLM tier {name!r} has unknown key(s) {unknown}; a tier may set"
            f" only {sorted(LLM_BLOCK_KEYS)}."
        )
        raise LLMRoutingError(msg, setting=setting)
    for key, value in body.items():
        if value is not None and not isinstance(value, str):
            msg = (
                f"LLM tier {name!r} key {key!r} must be a string or null,"
                f" got {type(value).__name__}."
            )
            raise LLMRoutingError(msg, setting=f"{setting}.{key}")
    if "provider" in body and not body["provider"]:
        msg = (
            f"LLM tier {name!r} sets an empty provider; omit the key to"
            " inherit the parent's."
        )
        raise LLMRoutingError(msg, setting=f"{setting}.provider")

    owns_credential = any(key in body for key in CREDENTIAL_KEYS)
    names_credential = any(body.get(key) for key in CREDENTIAL_KEYS)
    if owns_credential and not names_credential:
        msg = (
            f"LLM tier {name!r} sets api_key/api_key_env without a value,"
            " so it can never build a client. Name the credential, or omit"
            " both keys to inherit the parent's."
        )
        raise LLMRoutingError(msg, setting=setting)
    moved = [
        key
        for key in ENDPOINT_KEYS
        if key in body and body[key] != parent_endpoint[key]
    ]
    if moved and not owns_credential:
        msg = (
            f"LLM tier {name!r} changes {' and '.join(moved)} but names no"
            " credential, so it would send the parent block's key to a"
            " different service. Set api_key_env (preferred) or api_key on"
            " the tier."
        )
        raise LLMRoutingError(msg, setting=setting)
    if (
        "provider" in body
        and body["provider"] != parent_endpoint["provider"]
        and "base_url" not in body
        and parent_endpoint["base_url"]
    ):
        msg = (
            f"LLM tier {name!r} changes provider but would inherit the parent"
            " block's base_url, which points at the parent's service, so the"
            " tier's own key would be sent there. Set base_url on the tier"
            " (null for the provider's default)."
        )
        raise LLMRoutingError(msg, setting=f"{setting}.base_url")


def _validate_route(
    consumer: str, tier: Any, tiers: Mapping[Any, Any], known: set[str]
) -> None:
    setting = f"llm.routes.{consumer}"
    if consumer not in known:
        msg = (
            f"llm.routes names unknown consumer {consumer!r}; the consumers"
            f" are {sorted(known)}."
        )
        raise LLMRoutingError(msg, setting=setting)
    if not isinstance(tier, str) or not tier:
        msg = (
            f"llm.routes.{consumer} must name a tier, got"
            f" {'null' if tier is None else repr(tier)}. Delete the route to"
            " use the parent 'llm:' block."
        )
        raise LLMRoutingError(msg, setting=setting)
    if tier not in tiers:
        defined = sorted(tiers) or "none"
        msg = (
            f"llm.routes.{consumer} names tier {tier!r}, which llm.tiers does"
            f" not define (defined: {defined})."
        )
        raise LLMRoutingError(msg, setting=setting)


def resolve_llm_route(
    llm_config: Mapping[str, Any] | None,
    consumer: LLMConsumer | str | None,
) -> LLMRoute:
    """Resolve the configuration ``consumer`` builds its LLM client from.

    ``consumer=None`` (an out-of-repo caller) and any unrouted consumer get
    the parent block unchanged. Raises :class:`LLMRoutingError` when the
    routing block is malformed, and :class:`ValueError` for a consumer name
    that is not an :class:`LLMConsumer` — that one is a code defect, not a
    config one.
    """
    member = None if consumer is None else LLMConsumer(consumer)
    parent = llm_config or {}
    validate_llm_routing(parent)
    tier = None if member is None else _routes(parent).get(member.value)
    if tier is None:
        return LLMRoute(consumer=member, tier=None, block=MappingProxyType(parent))

    body = _tiers(parent)[tier]
    effective: dict[str, Any] = {
        key: body[key] if key in body else parent.get(key)
        for key in ("provider", "base_url", "model")
    }
    credential_owner = body if any(key in body for key in CREDENTIAL_KEYS) else parent
    for key in CREDENTIAL_KEYS:
        effective[key] = credential_owner.get(key)
    return LLMRoute(consumer=member, tier=tier, block=MappingProxyType(effective))


def _tiers(parent: Mapping[str, Any]) -> Mapping[Any, Any]:
    return _section(parent, _TIERS_KEY)


def _routes(parent: Mapping[str, Any]) -> Mapping[Any, Any]:
    return _section(parent, _ROUTES_KEY)


def _section(parent: Mapping[str, Any], key: str) -> Mapping[Any, Any]:
    value = parent.get(key)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        msg = f"llm.{key} must be a mapping, got {type(value).__name__}."
        raise LLMRoutingError(msg, setting=f"llm.{key}")
    for name in value:
        if not isinstance(name, str) or not name:
            msg = f"llm.{key} keys must be non-empty strings, got {name!r}."
            raise LLMRoutingError(msg, setting=f"llm.{key}")
    return value


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _display_url(url: str | None) -> str | None:
    """``url`` with its userinfo masked; a placeholder if it cannot be parsed."""
    if url is None:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<unparseable>"
    if "@" not in parts.netloc:
        return url
    host = parts.netloc.rpartition("@")[2]
    return urlunsplit(parts._replace(netloc=f"***@{host}"))
