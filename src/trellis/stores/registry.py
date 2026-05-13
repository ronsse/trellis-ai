"""Store registry — dependency injection for store backends."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import structlog

from trellis.errors import BackendNotInstalledError, ConfigError, ValidationError
from trellis.stores.base import (
    BlobStore,
    DocumentStore,
    EventLog,
    GraphStore,
    OutcomeStore,
    ParameterStore,
    TraceStore,
    TunerStateStore,
    VectorStore,
)

if TYPE_CHECKING:
    from trellis.llm.protocol import EmbedderClient, LLMClient

logger = structlog.get_logger(__name__)

_UNSET: Any = object()  # sentinel for lazy embedding_fn init

# Minimum length below which an API key is fully masked rather than showing
# trailing characters. Very short strings are either placeholders or
# malformed, so we treat them as opaque.
_MIN_SAFE_KEY_LEN = 4


class RegistryValidationError(Exception):
    """Aggregate failure raised by :meth:`StoreRegistry.validate`.

    Carries the per-store ``(store_type, exception)`` pairs so an
    operator looking at a startup crash can see every problem at once
    rather than fixing them serially across deploy attempts. The
    rendered message is multi-line and intentionally verbose — startup
    output is the right place for that.
    """

    def __init__(self, errors: list[tuple[str, Exception]]) -> None:
        self.errors = errors
        formatted = "\n".join(
            f"  - {store_type}: {type(exc).__name__}: {exc}"
            for store_type, exc in errors
        )
        super().__init__(
            f"StoreRegistry validation failed for {len(errors)} store(s):\n{formatted}"
        )


# Cache of merged (built-in + plugin) backend maps. Populated lazily on
# first access so that installing a plugin wheel into an already-running
# process doesn't require a restart. Keyed by store_type ("trace", ...).
_MERGED_BACKENDS_CACHE: dict[str, dict[str, tuple[str, str]]] = {}

# Backend registry keyed as plane -> store_type -> backend_name ->
# (module_path, class_name). The plane-outer shape makes it structurally
# visible which stores belong to which plane; code paths that should
# only touch one plane can constrain themselves to its sub-dict. The
# plugin loader (see adr-plugin-contract.md) merges entry-point
# backends on top of this table per store_type at lookup time — see
# _get_merged_backends.
#
# See docs/design/adr-planes-and-substrates.md for the plane taxonomy:
# Knowledge Plane = shared agent-readable state populated by client
# systems through the governed mutation pipeline; Operational Plane =
# Trellis-internal state (audit, execution traces) never populated by
# client code. Cross-plane data only flows through the two sanctioned
# bridges documented in the ADR: MutationExecutor (Knowledge writes
# emit to EventLog) and the effectiveness feedback loop (EventLog
# informs DocumentStore tags).
_BUILTIN_BACKENDS: dict[str, dict[str, dict[str, tuple[str, str]]]] = {
    "knowledge": {
        "graph": {
            "sqlite": ("trellis.stores.sqlite.graph", "SQLiteGraphStore"),
            "postgres": ("trellis.stores.postgres.graph", "PostgresGraphStore"),
            "neo4j": ("trellis.stores.neo4j.graph", "Neo4jGraphStore"),
            "arcadedb": ("trellis.stores.arcadedb.graph", "ArcadeDBGraphStore"),
        },
        "vector": {
            "sqlite": ("trellis.stores.sqlite.vector", "SQLiteVectorStore"),
            "pgvector": ("trellis.stores.pgvector.store", "PgVectorStore"),
            "neo4j": ("trellis.stores.neo4j.vector", "Neo4jVectorStore"),
            "arcadedb": ("trellis.stores.arcadedb.vector", "ArcadeDBVectorStore"),
        },
        "document": {
            "sqlite": ("trellis.stores.sqlite.document", "SQLiteDocumentStore"),
            "postgres": (
                "trellis.stores.postgres.document",
                "PostgresDocumentStore",
            ),
        },
        "blob": {
            "local": ("trellis.stores.local.blob", "LocalBlobStore"),
            "s3": ("trellis.stores.s3.blob", "S3BlobStore"),
        },
    },
    "operational": {
        "trace": {
            "sqlite": ("trellis.stores.sqlite.trace", "SQLiteTraceStore"),
            "postgres": ("trellis.stores.postgres.trace", "PostgresTraceStore"),
        },
        "event_log": {
            "sqlite": ("trellis.stores.sqlite.event_log", "SQLiteEventLog"),
            "postgres": (
                "trellis.stores.postgres.event_log",
                "PostgresEventLog",
            ),
        },
        # Feedback-driven parameter-tuning stores (operational plane by
        # the planes-and-substrates ADR cutoff: "consumed by Trellis to
        # self-improve").  SQLite-only for now; Postgres implementations
        # follow when the loop lands in production.
        "outcome": {
            "sqlite": ("trellis.stores.sqlite.outcome", "SQLiteOutcomeStore"),
        },
        "parameter": {
            "sqlite": ("trellis.stores.sqlite.parameter", "SQLiteParameterStore"),
        },
        "tuner_state": {
            "sqlite": (
                "trellis.stores.sqlite.tuner_state",
                "SQLiteTunerStateStore",
            ),
        },
    },
}

# Derived from _BUILTIN_BACKENDS — single source of truth for plane
# membership. Do not hand-edit; add a store_type under the appropriate
# plane above.
_PLANE_OF: dict[str, str] = {
    store_type: plane
    for plane, store_types in _BUILTIN_BACKENDS.items()
    for store_type in store_types
}

# Environment variable names per plane for Postgres DSN resolution.
_PLANE_PG_DSN_ENV: dict[str, str] = {
    "knowledge": "TRELLIS_KNOWLEDGE_PG_DSN",
    "operational": "TRELLIS_OPERATIONAL_PG_DSN",
}

# Operator-controlled toggle for the connectivity-ping branch in
# :meth:`StoreRegistry.validate`. Read at validate-time, not import-time,
# so a process can flip the env var between runs without reload.
_VALIDATE_CONNECTIVITY_ENV = "TRELLIS_VALIDATE_CONNECTIVITY"
_TRUTHY = frozenset({"1", "true", "yes", "on"})

# Fingerprint-mismatch check (Logic Gap 4.5). On by default; the env var
# bypasses for migration windows where a substrate is being swapped or
# upgraded and the operator has accepted the staleness risk for the
# transition. Read at validate-time so the same registry instance can
# flip behaviour between runs.
_FINGERPRINT_SKIP_ENV = "TRELLIS_SKIP_FINGERPRINT_CHECK"
_FINGERPRINT_META_FILENAME = "_trellis_meta.json"
# Default schema version when a substrate class doesn't override
# ``SCHEMA_VERSION``. Bumping this is a fleet-wide migration event;
# bumping the per-class constant on a single substrate is the normal
# path when only that substrate's schema changes.
_DEFAULT_SCHEMA_VERSION = "1"

# Mapping from backend name to the ``pyproject.toml`` optional-extra that
# pulls in its Python dependencies. Used by
# :class:`BackendNotInstalledError` to render an install command in the
# error message. Backends that ship in core (``sqlite``, ``local``) are
# absent from this map — their import never fails.
_EXTRA_FOR_BACKEND: dict[str, str] = {
    "postgres": "cloud",
    "pgvector": "cloud",
    "s3": "cloud",
    "neo4j": "neo4j",
    "arcadedb": "arcadedb",
}


def _resolve_connectivity_check(explicit: bool | None) -> bool:
    """Return the effective connectivity-check flag for a validate() call."""
    if explicit is not None:
        return explicit
    import os  # noqa: PLC0415

    raw = os.environ.get(_VALIDATE_CONNECTIVITY_ENV, "").strip().lower()
    return raw in _TRUTHY


def _extract_store_config(data: dict[str, Any], config_source: str) -> dict[str, Any]:
    """Flatten the YAML store config into the internal ``{store_type: cfg}`` shape.

    Accepts the plane-split shape (``knowledge:`` / ``operational:``
    blocks). The internal representation stays flat
    (``{"graph": {...}, ...}``) because ``_instantiate`` resolves the
    plane from ``_PLANE_OF`` at lookup time.
    """
    knowledge_cfg = data.get("knowledge")
    operational_cfg = data.get("operational")

    if knowledge_cfg is None and operational_cfg is None:
        return {}

    merged: dict[str, Any] = {}
    for plane_name, plane_cfg in (
        ("knowledge", knowledge_cfg),
        ("operational", operational_cfg),
    ):
        if not plane_cfg:
            continue
        if not isinstance(plane_cfg, dict):
            logger.warning(
                "registry_config_plane_not_mapping",
                plane=plane_name,
                source=config_source,
            )
            continue
        for store_type, store_cfg in plane_cfg.items():
            expected_plane = _PLANE_OF.get(store_type)
            if expected_plane is None:
                logger.warning(
                    "registry_config_unknown_store_type",
                    store_type=store_type,
                    plane=plane_name,
                    source=config_source,
                )
                continue
            if expected_plane != plane_name:
                logger.warning(
                    "registry_config_store_in_wrong_plane",
                    store_type=store_type,
                    declared_plane=plane_name,
                    expected_plane=expected_plane,
                    source=config_source,
                )
                continue
            merged[store_type] = store_cfg
    return merged


def _resolve_plane_pg_dsn(store_type: str) -> str | None:
    """Resolve a Postgres DSN for ``store_type`` via its plane's env var.

    Precedence:

    1. ``TRELLIS_{PLANE}_PG_DSN`` (per ADR planes-and-substrates).
    2. ``None`` — caller raises with a helpful message.
    """
    import os  # noqa: PLC0415

    plane = _PLANE_OF.get(store_type)
    if plane is None:
        return None

    plane_env = _PLANE_PG_DSN_ENV[plane]
    return os.environ.get(plane_env)


def _get_merged_backends(store_type: str) -> dict[str, tuple[str, str]]:
    """Return built-in backends for ``store_type`` merged with plugins.

    Imports :mod:`trellis.plugins` lazily to avoid pulling the
    plugin loader into the import graph for callers that don't need
    it (e.g. tests that stub ``_BUILTIN_BACKENDS`` directly).  The
    merged map is cached per store_type — re-running discovery on
    every ``_instantiate`` call would be wasteful.
    """
    if store_type in _MERGED_BACKENDS_CACHE:
        return _MERGED_BACKENDS_CACHE[store_type]

    plane = _PLANE_OF.get(store_type)
    if plane is None:
        # Unknown store_type — return empty; caller surfaces the error.
        return {}
    builtins = _BUILTIN_BACKENDS.get(plane, {}).get(store_type, {})
    try:
        from trellis.plugins import GROUP_STORES, merge_with_builtins  # noqa: PLC0415
    except ImportError:
        # GRACEFUL-DEGRADATION: ``trellis.plugins`` is shipped in core,
        # but a deliberately stripped install (e.g. embedded zipapp without
        # the plugin loader) is a supported deployment shape. Built-in
        # backends keep working; only entry-point plugins are unavailable.
        # Narrow to ImportError so genuine bugs in plugin discovery still
        # surface as unhandled exceptions.
        logger.debug("plugin_loader_unavailable", store_type=store_type)
        _MERGED_BACKENDS_CACHE[store_type] = dict(builtins)
        return _MERGED_BACKENDS_CACHE[store_type]

    merged, _ = merge_with_builtins(
        f"{GROUP_STORES}.{store_type}",
        builtins,
    )
    _MERGED_BACKENDS_CACHE[store_type] = merged
    return merged


def _reset_backend_cache() -> None:
    """Test helper — clear the merged-backends cache.

    Exposed for tests that install mock entry points mid-run; real
    callers rely on first-access caching and don't need to reset.
    """
    _MERGED_BACKENDS_CACHE.clear()


def _try_llm_plugin(
    *,
    group: str,
    provider: str,
    api_key: str,
    base_url: str | None,
    model: str | None,
    failure_event: str,
) -> Any | None:
    """Resolve an LLM client / embedder from a plugin entry-point group.

    Plugins advertise a factory that accepts the same keyword args
    the built-in providers do: ``api_key``, ``base_url``,
    ``default_model``. Anything else is the plugin's concern.

    Returns ``None`` — never raises — when the plugin is missing,
    malformed, or can't be instantiated. The caller is responsible
    for logging the unknown-provider path; this helper only logs
    when a plugin *was* found but failed to instantiate
    (``failure_event``).
    """
    try:
        from trellis.plugins import discover, load_class  # noqa: PLC0415
    except Exception:
        return None

    for spec in discover(group):
        if spec.name != provider:
            continue
        factory = load_class(spec)
        if factory is None:
            return None
        try:
            return factory(
                api_key=api_key,
                base_url=base_url,
                default_model=model,
            )
        except Exception:
            logger.exception(
                failure_event,
                provider=provider,
                plugin=spec.value,
            )
            return None
    return None


def _try_llm_provider_plugin(
    *,
    provider: str,
    api_key: str,
    base_url: str | None,
    model: str | None,
) -> Any | None:
    """Plugin path for :class:`LLMClient` via the ``trellis.llm.providers`` group."""
    from trellis.plugins import GROUP_LLM_PROVIDERS  # noqa: PLC0415

    return _try_llm_plugin(
        group=GROUP_LLM_PROVIDERS,
        provider=provider,
        api_key=api_key,
        base_url=base_url,
        model=model,
        failure_event="llm_provider_plugin_init_failed",
    )


def _try_llm_embedder_plugin(
    *,
    provider: str,
    api_key: str,
    base_url: str | None,
    model: str | None,
) -> Any | None:
    """Plugin path for :class:`EmbedderClient` via ``trellis.llm.embedders``."""
    from trellis.plugins import GROUP_LLM_EMBEDDERS  # noqa: PLC0415

    return _try_llm_plugin(
        group=GROUP_LLM_EMBEDDERS,
        provider=provider,
        api_key=api_key,
        base_url=base_url,
        model=model,
        failure_event="llm_embedder_plugin_init_failed",
    )


def _import_callable(dotted_path: str) -> Callable[[str], list[float]]:
    """Import a callable from a dotted module path (e.g. ``pkg.mod.func``).

    Raises :class:`ConfigError` when the path is malformed, the module
    cannot be imported, or the named attribute is not callable. Returning
    ``None`` silently here would let a misconfigured ``TRELLIS_EMBEDDING_FN``
    propagate as ``embedding_fn is None`` downstream, which masks the typo
    behind a "no embeddings configured" branch.
    """
    import importlib  # noqa: PLC0415

    module_path, _, attr_name = dotted_path.rpartition(".")
    if not module_path or not attr_name:
        msg = (
            f"Invalid embedding callable path {dotted_path!r} —"
            " expected a dotted path like 'pkg.module.func'."
        )
        raise ConfigError(msg, setting="embeddings.provider")
    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        msg = (
            f"Could not import embedding callable {dotted_path!r}:"
            f" module {module_path!r} is not importable ({exc})."
        )
        raise ConfigError(msg, setting="embeddings.provider") from exc
    fn = getattr(module, attr_name, None)
    if fn is None:
        msg = (
            f"Embedding callable path {dotted_path!r} resolved, but"
            f" attribute {attr_name!r} is missing from {module_path!r}."
        )
        raise ConfigError(msg, setting="embeddings.provider")
    if not callable(fn):
        msg = (
            f"Embedding callable path {dotted_path!r} resolved, but"
            f" {attr_name!r} is not callable."
        )
        raise ConfigError(msg, setting="embeddings.provider")
    return fn  # type: ignore[no-any-return]


def _mask_api_key(api_key: str | None) -> str:
    """Mask an API key for safe logging — keep last 4 chars only.

    Returns ``"<none>"`` when the key is empty / ``None``.
    """
    if not api_key:
        return "<none>"
    if len(api_key) <= _MIN_SAFE_KEY_LEN:
        return "***"
    return f"***{api_key[-4:]}"


def _resolve_api_key(cfg: dict[str, Any]) -> str | None:
    """Resolve an API key from a config block.

    Prefers ``api_key_env`` (name of an environment variable to read) over
    ``api_key`` (literal value). Returns ``None`` when neither is present
    or the referenced env var is unset / empty. Never raises.
    """
    import os  # noqa: PLC0415

    env_name = cfg.get("api_key_env")
    literal = cfg.get("api_key")

    if env_name:
        value = os.environ.get(str(env_name))
        if value:
            return value
        logger.debug(
            "llm_api_key_env_unset",
            api_key_env=str(env_name),
        )
        # Fall through to literal only if env var is truly absent; we prefer
        # api_key_env but tolerate a literal fallback when both are specified.
        if literal:
            return str(literal)
        return None

    if literal:
        return str(literal)

    logger.debug("llm_api_key_missing")
    return None


def _build_openai_embedding_fn(
    config: dict[str, Any],
) -> Callable[[str], list[float]]:
    """Build an embedding callable using the OpenAI SDK.

    Raises :class:`BackendNotInstalledError` when the ``openai`` SDK
    is not installed — returning ``None`` here silently demoted
    ``embeddings: provider: openai`` configs to no-embedding mode
    instead of telling the operator the extra is missing.
    """
    try:
        import openai  # noqa: PLC0415
    except ModuleNotFoundError as exc:
        raise BackendNotInstalledError(
            backend_name="openai-embeddings",
            extra="llm-openai",
        ) from exc

    model = config.get("model", "text-embedding-3-small")
    # Prefer api_key_env over literal api_key; either falls back to the
    # OpenAI SDK's default OPENAI_API_KEY env var lookup when neither is set.
    api_key = _resolve_api_key(config)
    kwargs: dict[str, Any] = {}
    if api_key:
        kwargs["api_key"] = api_key
    if config.get("base_url"):
        kwargs["base_url"] = config["base_url"]

    client = openai.OpenAI(**kwargs)

    def _embed(text: str) -> list[float]:
        resp = client.embeddings.create(input=[text], model=model)
        return list(resp.data[0].embedding)

    return _embed


class _KnowledgePlane:
    """Namespaced accessor for the four Knowledge-Plane stores.

    Knowledge stores hold shared, agent-readable state populated by
    client systems. See docs/design/adr-planes-and-substrates.md §2.1.
    """

    __slots__ = ("_registry",)

    def __init__(self, registry: StoreRegistry) -> None:
        self._registry = registry

    @property
    def graph_store(self) -> GraphStore:
        return self._registry._get("graph")  # type: ignore[no-any-return]

    @property
    def vector_store(self) -> VectorStore:
        return self._registry._get("vector")  # type: ignore[no-any-return]

    @property
    def document_store(self) -> DocumentStore:
        return self._registry._get("document")  # type: ignore[no-any-return]

    @property
    def blob_store(self) -> BlobStore:
        return self._registry._get("blob")  # type: ignore[no-any-return]


class _OperationalPlane:
    """Namespaced accessor for the two Operational-Plane stores.

    Operational stores hold Trellis-internal state (execution traces,
    the mutation audit log). Client code never populates these directly;
    they are written by the governed mutation pipeline and read by
    admin/debug surfaces. See docs/design/adr-planes-and-substrates.md §2.1.
    """

    __slots__ = ("_registry",)

    def __init__(self, registry: StoreRegistry) -> None:
        self._registry = registry

    @property
    def trace_store(self) -> TraceStore:
        return self._registry._get("trace")  # type: ignore[no-any-return]

    @property
    def event_log(self) -> EventLog:
        return self._registry._get("event_log")  # type: ignore[no-any-return]

    @property
    def outcome_store(self) -> OutcomeStore:
        """High-volume per-call signal log for feedback-driven parameter tuning."""
        return self._registry._get("outcome")  # type: ignore[no-any-return]

    @property
    def parameter_store(self) -> ParameterStore:
        """Versioned parameter snapshots keyed by learning-axis scope."""
        return self._registry._get("parameter")  # type: ignore[no-any-return]

    @property
    def tuner_state_store(self) -> TunerStateStore:
        """Working state (proposals, cursors) for tuner components."""
        return self._registry._get("tuner_state")  # type: ignore[no-any-return]


# Per-backend URI validation rules. Each entry maps the backend name
# to the param key that carries the URI/DSN and the set of schemes
# that are valid for it. Postgres accepts both ``postgres://`` and
# ``postgresql://`` (psycopg parses both). Neo4j accepts the bolt and
# neo4j scheme families (the ``+s`` / ``+ssc`` variants enable TLS).
# S3 isn't listed here — its config carries a bucket name, not a URI,
# so there's no scheme-level invariant the registry can enforce.
_BACKEND_URI_RULES: dict[str, tuple[str, frozenset[str]]] = {
    "postgres": ("dsn", frozenset({"postgres", "postgresql"})),
    "pgvector": ("dsn", frozenset({"postgres", "postgresql"})),
    "neo4j": (
        "uri",
        frozenset({"bolt", "neo4j", "bolt+s", "bolt+ssc", "neo4j+s", "neo4j+ssc"}),
    ),
}


def _validate_uri(
    backend: str, uri: str, allowed_schemes: frozenset[str]
) -> str | None:
    """Return an error message when ``uri`` is malformed; ``None`` on success.

    Parses with :func:`urlparse`, demands a non-empty scheme matching
    one of ``allowed_schemes``, and demands a non-empty netloc (the
    last check catches ``postgres:///dbname`` and ``://localhost``
    typos that the scheme check alone would miss).
    """
    parsed = urlparse(uri)
    expected = sorted(allowed_schemes)
    if not parsed.scheme:
        return f"empty URL scheme (expected one of {expected})"
    if parsed.scheme not in allowed_schemes:
        return (
            f"unexpected URL scheme '{parsed.scheme}' for {backend} backend"
            f" (expected one of {expected})"
        )
    if not parsed.netloc:
        return f"empty network location in URI '{uri}' (host:port required)"
    return None


class StoreRegistry:
    """Lazily instantiates and caches store backends based on configuration."""

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        stores_dir: Path | None = None,
        *,
        embedding_config: dict[str, Any] | None = None,
        retrieval_config: dict[str, Any] | None = None,
        llm_config: dict[str, Any] | None = None,
    ) -> None:
        # Accept two input shapes equivalently:
        #   * plane-split  ``{"knowledge": {...}, "operational": {...}}``
        #   * already-flat  ``{"graph": {...}, ...}``
        # Plane-split gets normalised to flat before storing. Without
        # this, plane-split callers silently fell back to SQLite because
        # ``_resolve_backend`` does ``self._config.get(store_type)`` at
        # lookup time and never sees through the plane wrapper.
        raw_config = config or {}
        if any(k in raw_config for k in ("knowledge", "operational")):
            self._config = _extract_store_config(raw_config, "constructor")
        else:
            self._config = raw_config
        self._stores_dir = stores_dir
        self._embedding_config = embedding_config or {}
        self._retrieval_config = retrieval_config or {}
        self._llm_config = llm_config or {}
        self._cache: dict[str, Any] = {}
        self._embedding_fn_cache: Callable[[str], list[float]] | None = _UNSET
        self._budget_config_cache: Any = _UNSET
        # Shared Bolt drivers: one ``Driver`` per ``(uri, user)`` so a
        # graph + vector pair pointing at the same instance reuses one
        # connection pool. Used by Neo4j today; future Bolt-speaking
        # backends share the same cache. Closed by :meth:`close` after
        # individual stores have closed (a no-op for stores with
        # injected drivers).
        self._bolt_drivers: dict[tuple[str, str], Any] = {}
        self._knowledge = _KnowledgePlane(self)
        self._operational = _OperationalPlane(self)

    @property
    def stores_dir(self) -> Path | None:
        """Return the base directory for store files, or ``None``."""
        return self._stores_dir

    @classmethod
    def from_config_dir(
        cls,
        config_dir: Path | None = None,
        data_dir: Path | None = None,
    ) -> StoreRegistry:
        """Create a registry from a Trellis config directory.

        Reads the plane-split store config per ADR
        planes-and-substrates::

            knowledge:
              graph: { backend: sqlite }
              vector: { backend: sqlite }
              document: { backend: sqlite }
              blob: { backend: local }
            operational:
              trace: { backend: sqlite }
              event_log: { backend: sqlite }
        """
        import os  # noqa: PLC0415

        if config_dir is None:
            config_dir = Path(
                os.environ.get("TRELLIS_CONFIG_DIR", str(Path.home() / ".trellis"))
            )
        if data_dir is None:
            data_dir = Path(
                os.environ.get("TRELLIS_DATA_DIR", str(config_dir / "data"))
            )

        # Try to load store config from config.yaml
        store_config: dict[str, Any] = {}
        embedding_config: dict[str, Any] = {}
        retrieval_config: dict[str, Any] = {}
        llm_config: dict[str, Any] = {}
        config_path = config_dir / "config.yaml"
        if config_path.exists():
            import yaml  # noqa: PLC0415

            try:
                raw_text = config_path.read_text()
            except OSError as exc:
                msg = (
                    f"Could not read Trellis config at {config_path}: {exc}."
                    " Check file permissions or pass an explicit config_dir."
                )
                raise ConfigError(msg, setting="config_dir") from exc
            try:
                data = yaml.safe_load(raw_text) or {}
            except yaml.YAMLError as exc:
                msg = (
                    f"Could not parse Trellis config at {config_path}: {exc}."
                    " Fix the YAML syntax and retry."
                )
                raise ConfigError(msg, setting="config.yaml") from exc
            store_config = _extract_store_config(data, str(config_path))
            embedding_config = data.get("embeddings", {})
            retrieval_config = data.get("retrieval", {})
            llm_config = data.get("llm", {})
            if data.get("data_dir"):
                data_dir = Path(data["data_dir"])

        stores_dir = data_dir / "stores"
        return cls(
            config=store_config,
            stores_dir=stores_dir,
            embedding_config=embedding_config,
            retrieval_config=retrieval_config,
            llm_config=llm_config,
        )

    def _resolve_backend(self, store_type: str) -> tuple[str, dict[str, Any]]:
        """Resolve backend name and params for a store type."""
        store_cfg = self._config.get(store_type, {})
        if isinstance(store_cfg, str):
            return store_cfg, {}
        backend = store_cfg.get("backend", self._default_backend(store_type))
        params = {k: v for k, v in store_cfg.items() if k != "backend"}
        return backend, params

    @staticmethod
    def _default_backend(store_type: str) -> str:
        """Return the default backend for a store type."""
        if store_type == "blob":
            return "local"
        return "sqlite"

    def _instantiate(self, store_type: str) -> Any:
        """Create a store instance from config."""
        backend, params = self._resolve_backend(store_type)

        plane = _PLANE_OF.get(store_type)
        if plane is None:
            msg = f"Unknown store type '{store_type}'"
            raise ValidationError(msg)

        registry = _get_merged_backends(store_type)
        if backend not in registry:
            msg = f"Unknown backend '{backend}' for store type '{store_type}'"
            raise ConfigError(msg, setting=f"stores.{store_type}.backend")

        module_path, class_name = registry[backend]

        import importlib  # noqa: PLC0415

        module = importlib.import_module(module_path)
        cls = getattr(module, class_name)

        # For sqlite backends, default to stores_dir/<type>.db
        if backend == "sqlite" and "db_path" not in params:
            if self._stores_dir is None:
                msg = (
                    "stores_dir must be set for sqlite backends"
                    " without explicit db_path"
                )
                raise ConfigError(msg, setting="stores_dir")
            self._stores_dir.mkdir(parents=True, exist_ok=True)
            db_names = {
                "trace": "traces.db",
                "document": "documents.db",
                "graph": "graph.db",
                "vector": "vectors.db",
                "event_log": "events.db",
                "outcome": "outcomes.db",
                "parameter": "parameters.db",
                "tuner_state": "tuner_state.db",
            }
            params["db_path"] = self._stores_dir / db_names[store_type]

        # For local blob backend, default to stores_dir/blobs/
        if backend == "local" and "root_dir" not in params:
            if self._stores_dir is None:
                msg = (
                    "stores_dir must be set for local blob backend"
                    " without explicit root_dir"
                )
                raise ConfigError(msg, setting="stores_dir")
            params["root_dir"] = self._stores_dir / "blobs"

        # For postgres / pgvector backends, default DSN from env.
        # Plane-aware resolution: TRELLIS_KNOWLEDGE_PG_DSN for the
        # knowledge plane, TRELLIS_OPERATIONAL_PG_DSN for the
        # operational plane.
        if backend in {"postgres", "pgvector"} and "dsn" not in params:
            dsn = _resolve_plane_pg_dsn(store_type)
            if not dsn:
                plane_env = _PLANE_PG_DSN_ENV[plane]
                msg = (
                    f"dsn must be set for {backend} backend"
                    f" (config or {plane_env} env var)"
                )
                raise ConfigError(msg, setting=plane_env)
            params["dsn"] = dsn

        # For neo4j backend, share one driver per (uri, user) across the
        # graph + vector store pair. Params can carry a ``driver_config``
        # mapping (or DriverConfig instance) plus the connection trio;
        # the resolved driver is injected so individual stores skip their
        # own build_driver() call. The registry keeps the driver and
        # closes it in :meth:`close` after stores have finished.
        if backend == "neo4j" and "driver" not in params:
            params = self._inject_neo4j_driver(params)

        # ArcadeDB: graph shares the Bolt driver cache with Neo4j;
        # vector takes HTTP-only params (no Bolt driver) via a
        # separate resolver.
        if backend == "arcadedb" and store_type == "graph" and "driver" not in params:
            params = self._inject_arcadedb_driver(params)
        elif backend == "arcadedb" and store_type == "vector":
            params = self._resolve_arcadedb_vector_params(params)

        # For s3 backend, default bucket from env
        if backend == "s3" and "bucket" not in params:
            import os  # noqa: PLC0415

            bucket = os.environ.get("TRELLIS_S3_BUCKET")
            if not bucket:
                msg = (
                    "bucket must be set for s3 backend"
                    " (config or TRELLIS_S3_BUCKET env var)"
                )
                raise ConfigError(msg, setting="TRELLIS_S3_BUCKET")
            params["bucket"] = bucket

        logger.info("store_instantiated", store_type=store_type, backend=backend)
        return cls(**params)

    def _inject_neo4j_driver(self, params: dict[str, Any]) -> dict[str, Any]:
        """Resolve a shared Neo4j driver for ``params`` and inject it.

        Returns a new ``params`` dict with ``password`` + ``driver_config``
        stripped (those go into the driver) and ``driver`` added. The
        returned driver is cached on the registry under ``(uri, user)``
        and closed by :meth:`close`.

        Idempotent for the same ``(uri, user)`` — second + later calls
        return the same driver instance even if the second store passes
        a different ``driver_config`` (the first config wins; this is
        expected since both stores share one connection pool).
        """
        from trellis.stores.neo4j.base import (  # noqa: PLC0415
            DriverConfig,
            build_driver,
        )

        if "uri" not in params:
            msg = "neo4j backend requires 'uri' in config or env"
            raise ConfigError(msg, setting="stores.graph.uri")
        uri = params["uri"]
        user = params.get("user", "neo4j")
        key = (uri, user)

        new_params = {k: v for k, v in params.items() if k != "driver_config"}
        if key in self._bolt_drivers:
            new_params.pop("password", None)
            new_params["driver"] = self._bolt_drivers[key]
            return new_params

        if "password" not in params:
            msg = "neo4j backend requires 'password' in config"
            raise ConfigError(msg, setting="stores.graph.password")

        raw_cfg = params.get("driver_config")
        if raw_cfg is None:
            cfg: DriverConfig | None = None
        elif isinstance(raw_cfg, DriverConfig):
            cfg = raw_cfg
        elif isinstance(raw_cfg, dict):
            cfg = DriverConfig(**raw_cfg)
        else:
            msg = (
                "driver_config must be a DriverConfig, a dict, or omitted; "
                f"got {type(raw_cfg).__name__}"
            )
            raise TypeError(msg)

        driver = build_driver(uri, user, params["password"], config=cfg)
        self._bolt_drivers[key] = driver
        new_params.pop("password")
        new_params["driver"] = driver
        return new_params

    def _inject_arcadedb_driver(self, params: dict[str, Any]) -> dict[str, Any]:
        """Resolve a shared ArcadeDB Bolt driver for ``params`` and inject it.

        Mirrors :meth:`_inject_neo4j_driver` — same driver-cache key
        shape (``(uri, user)``), same shared ``_bolt_drivers`` dict,
        same closing semantics. Differences from the Neo4j path:

        - Default user is ``"root"`` (ArcadeDB's conventional admin
          user) rather than ``"neo4j"``.
        - Honors ``TRELLIS_ARCADEDB_URI`` / ``_USER`` / ``_PASSWORD`` /
          ``_DATABASE`` env vars as fallbacks before raising.
        - Triggers the HTTP-based ``ensure_database`` call exactly
          once per driver (only when the driver is newly built), so the
          target database is created idempotently at first use.
        """
        import os  # noqa: PLC0415

        from trellis.stores.arcadedb.base import (  # noqa: PLC0415
            build_arcadedb_driver,
            derive_http_url_from_bolt,
            ensure_database,
        )
        from trellis.stores.bolt_opencypher.base import (  # noqa: PLC0415
            BoltDriverConfig,
        )

        uri = params.get("uri") or os.environ.get("TRELLIS_ARCADEDB_URI")
        if not uri:
            msg = (
                "arcadedb backend requires 'uri' in config or "
                "TRELLIS_ARCADEDB_URI env var (e.g. bolt://host:7687)"
            )
            raise ConfigError(msg, setting="stores.graph.uri")
        user = params.get("user") or os.environ.get("TRELLIS_ARCADEDB_USER") or "root"
        password = params.get("password") or os.environ.get("TRELLIS_ARCADEDB_PASSWORD")
        database = (
            params.get("database")
            or os.environ.get("TRELLIS_ARCADEDB_DATABASE")
            or "trellis"
        )
        http_url = params.get("http_url") or os.environ.get("TRELLIS_ARCADEDB_HTTP_URL")
        ensure_db = params.get("ensure_database_exists", True)

        key = (uri, user)
        # Strip driver_config from params we'll forward to the store —
        # it's consumed at driver build time, not by the store itself.
        new_params = {
            k: v
            for k, v in params.items()
            if k not in {"driver_config", "http_url", "ensure_database_exists"}
        }
        new_params["uri"] = uri
        new_params["user"] = user
        new_params["database"] = database

        if key in self._bolt_drivers:
            # Driver already built (e.g. by the graph store; vector
            # store now joining the pool). Strip ``password`` so the
            # store skips its own build path and uses the injected
            # driver.
            new_params.pop("password", None)
            new_params["driver"] = self._bolt_drivers[key]
            return new_params

        if not password:
            msg = (
                "arcadedb backend requires 'password' in config or "
                "TRELLIS_ARCADEDB_PASSWORD env var"
            )
            raise ConfigError(msg, setting="stores.graph.password")

        raw_cfg = params.get("driver_config")
        if raw_cfg is None:
            cfg: BoltDriverConfig | None = None
        elif isinstance(raw_cfg, BoltDriverConfig):
            cfg = raw_cfg
        elif isinstance(raw_cfg, dict):
            cfg = BoltDriverConfig(**raw_cfg)
        else:
            msg = (
                "driver_config must be a BoltDriverConfig, a dict, or omitted; "
                f"got {type(raw_cfg).__name__}"
            )
            raise TypeError(msg)

        # Ensure the target database exists before the Bolt driver
        # binds to it. ``ensure_database`` is idempotent — a no-op when
        # the database already exists.
        if ensure_db:
            if not http_url:
                http_url = derive_http_url_from_bolt(uri)
            if not http_url:
                msg = (
                    "arcadedb backend with ensure_database_exists=True "
                    "needs an http_url (or a parseable host in the Bolt "
                    "uri). Set http_url in config, set "
                    "TRELLIS_ARCADEDB_HTTP_URL, or disable "
                    "ensure_database_exists."
                )
                raise ConfigError(msg, setting="stores.graph.http_url")
            ensure_database(http_url, user, password, database)

        driver = build_arcadedb_driver(uri, user, password, config=cfg)
        self._bolt_drivers[key] = driver
        new_params.pop("password")
        new_params["driver"] = driver
        return new_params

    def _resolve_arcadedb_vector_params(
        self, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Resolve HTTP-side config for an ArcadeDB vector store.

        The vector store talks SQL over HTTP, not Cypher over Bolt, so
        it doesn't share the Bolt driver cache. It needs:
        ``http_url`` / ``user`` / ``password`` / ``database`` — derived
        from explicit params first, then env vars
        (``TRELLIS_ARCADEDB_HTTP_URL`` / ``_USER`` / ``_PASSWORD`` /
        ``_DATABASE``), with reasonable defaults where possible.

        If ``http_url`` is missing AND a sibling Bolt URI is present
        (``uri`` param or ``TRELLIS_ARCADEDB_URI``), derive
        ``http_url`` by swapping the Bolt port for the HTTP port on the
        same host. Lets a single ``arcadedb:`` config block cover both
        graph (Bolt) + vector (HTTP) backends without duplicating the
        host.
        """
        import os  # noqa: PLC0415

        from trellis.stores.arcadedb.base import (  # noqa: PLC0415
            derive_http_url_from_bolt,
        )

        new_params = dict(params)
        # http_url priority: explicit param > env var > derived from
        # sibling Bolt URI.
        http_url = new_params.get("http_url") or os.environ.get(
            "TRELLIS_ARCADEDB_HTTP_URL"
        )
        if not http_url:
            bolt_uri = new_params.get("uri") or os.environ.get(
                "TRELLIS_ARCADEDB_URI"
            )
            if bolt_uri:
                http_url = derive_http_url_from_bolt(bolt_uri)
        if not http_url:
            msg = (
                "arcadedb vector backend requires 'http_url' in config or "
                "TRELLIS_ARCADEDB_HTTP_URL env var (or a sibling Bolt 'uri' "
                "to derive it from)"
            )
            raise ConfigError(msg, setting="stores.vector.http_url")

        user = (
            new_params.get("user")
            or os.environ.get("TRELLIS_ARCADEDB_USER")
            or "root"
        )
        password = (
            new_params.get("password")
            or os.environ.get("TRELLIS_ARCADEDB_PASSWORD")
        )
        if not password:
            msg = (
                "arcadedb vector backend requires 'password' in config or "
                "TRELLIS_ARCADEDB_PASSWORD env var"
            )
            raise ConfigError(msg, setting="stores.vector.password")
        database = (
            new_params.get("database")
            or os.environ.get("TRELLIS_ARCADEDB_DATABASE")
            or "trellis"
        )

        # The vector store doesn't take a Bolt ``uri`` or ``driver`` —
        # strip them so the constructor doesn't complain.
        for key in ("uri", "driver", "driver_config", "ensure_database_exists"):
            new_params.pop(key, None)
        new_params["http_url"] = http_url
        new_params["user"] = user
        new_params["password"] = password
        new_params["database"] = database
        return new_params

    def _get(self, store_type: str) -> Any:
        if store_type not in self._cache:
            self._cache[store_type] = self._instantiate(store_type)
        return self._cache[store_type]

    def _check_uri_formats(
        self, store_types: Iterable[str]
    ) -> list[tuple[str, Exception]]:
        """Validate URI/DSN format for every relevant store in ``store_types``.

        Pre-flight check that catches typos (``://localhost``,
        ``postgres:///db``) before ``_instantiate`` hands them to a
        client SDK that may report them with a less actionable
        error. Configs without an explicit URI in the dict (e.g.
        Postgres relying on ``TRELLIS_KNOWLEDGE_PG_DSN``) are skipped
        here — the env-var fallback path is exercised in
        ``_instantiate`` and raises its own ``ConfigError`` if unset.
        """
        failures: list[tuple[str, Exception]] = []
        for store_type in store_types:
            store_cfg = self._config.get(store_type, {})
            if not isinstance(store_cfg, dict):
                continue
            backend = store_cfg.get("backend", self._default_backend(store_type))
            rule = _BACKEND_URI_RULES.get(backend)
            if rule is None:
                continue
            param, allowed = rule
            uri = store_cfg.get(param)
            if not uri:
                continue
            err_msg = _validate_uri(backend, uri, allowed)
            if err_msg is not None:
                failures.append((store_type, ConfigError(err_msg, setting=param)))
        return failures

    def _resolve_substrate_class(self, store_type: str) -> type | None:
        """Import and return the substrate class for ``store_type``, or None.

        Used by :meth:`_check_schema_fingerprints` to read the
        ``SCHEMA_VERSION`` class attribute without paying the cost of
        a full ``_instantiate`` (which opens connections, creates files,
        etc.). Returns ``None`` when no backend entry is registered for
        the configured name; raises :class:`BackendNotInstalledError`
        when the backend is known but its optional extra is missing,
        so the fingerprint check fails loudly instead of silently
        skipping schema-drift detection for backends whose SDK is gone.
        """
        backend, _ = self._resolve_backend(store_type)
        registry = _get_merged_backends(store_type)
        spec = registry.get(backend)
        if spec is None:
            return None
        module_path, class_name = spec
        import importlib  # noqa: PLC0415

        try:
            module = importlib.import_module(module_path)
        except ImportError as exc:
            raise BackendNotInstalledError(
                backend_name=backend,
                extra=_EXTRA_FOR_BACKEND.get(backend),
            ) from exc
        cls = getattr(module, class_name, None)
        return cls if isinstance(cls, type) else None

    def _compute_fingerprints(self, store_types: Iterable[str]) -> dict[str, str]:
        """Compute the configured fingerprint per store_type.

        Format: ``"{store_kind}/{backend}/v{SCHEMA_VERSION}"``. The
        backend name distinguishes substrate swaps; the version
        distinguishes within-substrate schema changes. Together they
        give us a single string that flips whenever the on-disk shape
        is no longer compatible with what we wrote last boot.

        Catches :class:`BackendNotInstalledError` here and falls back
        to the default schema version: the same error will surface
        with a better contextual message during instantiation, and
        we don't want the fingerprint check to short-circuit the
        aggregate validation report.
        """
        out: dict[str, str] = {}
        for store_type in store_types:
            backend, _ = self._resolve_backend(store_type)
            try:
                cls = self._resolve_substrate_class(store_type)
            except BackendNotInstalledError:
                # AGGREGATE-DEFER: instantiation path will re-raise the
                # same error with the per-store context, which the
                # validate() loop captures into the
                # ``RegistryValidationError`` aggregate.
                cls = None
            version = (
                str(getattr(cls, "SCHEMA_VERSION", _DEFAULT_SCHEMA_VERSION))
                if cls is not None
                else _DEFAULT_SCHEMA_VERSION
            )
            out[store_type] = f"{store_type}/{backend}/v{version}"
        return out

    def _fingerprint_meta_path(self) -> Path | None:
        """Return the on-disk path for the fingerprint meta file, or None.

        Returns ``None`` when ``stores_dir`` isn't configured — an
        all-remote deployment (postgres + s3 + neo4j with no local
        sqlite) has nowhere natural to write the file. In that case
        the check is silently skipped; the alternative would require
        every substrate to grow its own metadata table, which is out
        of scope for this unit (Logic Gap 4.5).
        """
        if self._stores_dir is None:
            return None
        return self._stores_dir / _FINGERPRINT_META_FILENAME

    def _load_fingerprint_meta(self) -> dict[str, str]:
        """Read the persisted fingerprint map; empty dict on first boot.

        Raises :class:`ConfigError` when the meta file exists but cannot
        be read or parsed — a corrupt fingerprint file would silently
        disable schema-drift detection on the affected stores, which is
        the exact regression Logic Gap 4.5 was meant to prevent.
        """
        path = self._fingerprint_meta_path()
        if path is None or not path.exists():
            return {}
        import json  # noqa: PLC0415

        try:
            raw = path.read_text()
        except OSError as exc:
            msg = (
                f"Could not read fingerprint meta at {path}: {exc}."
                " Fix the underlying I/O error or delete the file to"
                " reset the fingerprint store (first-boot semantics)."
            )
            raise ConfigError(msg, setting="stores_dir") from exc
        try:
            data = json.loads(raw)
        except ValueError as exc:
            msg = (
                f"Could not parse fingerprint meta at {path}: {exc}."
                " The file is corrupt. Delete it to reset the fingerprint"
                " store (first-boot semantics) and re-run validate."
            )
            raise ConfigError(msg, setting="stores_dir") from exc
        if not isinstance(data, dict):
            return {}
        return {str(k): str(v) for k, v in data.items()}

    def _write_fingerprint_meta(self, meta: dict[str, str]) -> None:
        """Persist the fingerprint map; best-effort (logs and continues on error)."""
        path = self._fingerprint_meta_path()
        if path is None:
            return
        try:
            import json  # noqa: PLC0415

            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(meta, indent=2, sort_keys=True))
        except OSError:
            logger.warning("fingerprint_meta_write_failed", path=str(path))

    def _check_schema_fingerprints(
        self, store_types: Iterable[str]
    ) -> tuple[list[tuple[str, Exception]], dict[str, str]]:
        """Compare configured vs stored fingerprints; return (errors, to_write).

        First-boot semantics: a missing entry is not an error — the
        configured fingerprint is queued for write after instantiation
        succeeds, so we don't lock in a fingerprint for a store that
        couldn't actually start. Mismatched entries become
        :class:`ConfigError`. ``TRELLIS_SKIP_FINGERPRINT_CHECK=1``
        bypasses both branches.
        """
        import os  # noqa: PLC0415

        if os.environ.get(_FINGERPRINT_SKIP_ENV, "").strip().lower() in _TRUTHY:
            logger.debug("schema_fingerprint_check_skipped_env")
            return [], {}

        # No on-disk place to put the meta file → no-op. All-remote
        # deployments (no sqlite/local-blob ⇒ no stores_dir) pay no
        # tax for the check; the threat model degrades gracefully to
        # ``no protection`` rather than ``boot crash``.
        if self._fingerprint_meta_path() is None:
            logger.debug("schema_fingerprint_check_skipped_no_stores_dir")
            return [], {}

        targets = list(store_types)
        configured = self._compute_fingerprints(targets)
        stored = self._load_fingerprint_meta()

        errors: list[tuple[str, Exception]] = []
        to_write: dict[str, str] = {}
        for store_type in targets:
            want = configured[store_type]
            have = stored.get(store_type)
            if have is None:
                # First boot for this store — queue the write.
                to_write[store_type] = want
                continue
            if have == want:
                continue
            msg = (
                f"SchemaFingerprintMismatch: store '{store_type}' "
                f"(plane={_PLANE_OF.get(store_type, '?')}): "
                f"configured fingerprint '{want}' != stored fingerprint '{have}'. "
                "Substrate schema may be out of date. Run migrations or switch "
                f"substrate, or set {_FINGERPRINT_SKIP_ENV}=1 to bypass during a "
                "migration window."
            )
            errors.append(
                (store_type, ConfigError(msg, setting=f"stores.{store_type}.backend"))
            )
        return errors, to_write

    def _check_embedding_dim_consistency(self) -> list[tuple[str, Exception]]:
        """Ensure ``vector`` and ``graph`` configs agree on ``embedding_dim``.

        The Neo4j shape #2 vector store stores embeddings as a property
        on graph-store nodes, so the two stores must share a dimension
        when both opt in. Either store may omit ``embedding_dim`` (the
        graph store opts out of vector storage; the vector store falls
        back to its backend default), in which case there is nothing
        to compare and no error is raised.
        """
        graph_cfg = self._config.get("graph") or {}
        vector_cfg = self._config.get("vector") or {}
        if not isinstance(graph_cfg, dict) or not isinstance(vector_cfg, dict):
            return []
        graph_dim = graph_cfg.get("embedding_dim")
        vector_dim = vector_cfg.get("embedding_dim")
        if graph_dim is None or vector_dim is None:
            return []
        if graph_dim == vector_dim:
            return []
        msg = (
            f"embedding_dim mismatch: graph={graph_dim} vs vector={vector_dim};"
            " the Neo4j shape #2 path stores vectors on graph nodes, so the"
            " two configs must agree."
        )
        return [
            (
                "embedding_dim",
                ConfigError(msg, setting="stores.{graph,vector}.embedding_dim"),
            )
        ]

    def validate(
        self,
        *,
        store_types: Iterable[str] | None = None,
        check_connectivity: bool | None = None,
    ) -> None:
        """Eagerly instantiate every store so misconfigurations fail at startup.

        Walks each entry in *store_types* (default: every store_type the
        registry knows about) and forces a side-effecting
        :meth:`_instantiate` call so latent config errors — missing
        Postgres DSN, unset ``TRELLIS_S3_BUCKET``, missing
        ``stores_dir`` for sqlite, plugin import failures — surface here
        rather than on first request. Successful stores stay warm in
        the cache, so post-validation access is free.

        Errors are accumulated across all stores rather than raised on
        the first failure: an operator deploying a fresh stack benefits
        more from seeing every misconfiguration at once than from
        playing whack-a-mole one store at a time. The aggregate is
        raised as :class:`RegistryValidationError`.

        Connectivity checks
        -------------------

        When ``check_connectivity=True``, additionally performs a Bolt
        round-trip per cached Bolt driver via
        :func:`trellis.stores.bolt_opencypher.base.verify_connectivity`.
        Failures (``ServiceUnavailable``, ``AuthError``, etc.) are
        added to the same aggregate so the operator sees both config
        errors and unreachable-backend errors in one shot.
        ``store_type`` for each connectivity error is reported as
        ``"bolt-driver:<uri>"``.

        Default (``check_connectivity=None``): respect the
        ``TRELLIS_VALIDATE_CONNECTIVITY`` env var (truthy values
        ``1`` / ``true`` / ``yes`` enable). Off otherwise. The env-var
        path lets dev keep fast restarts while production turns it on
        without code changes. Pass ``True`` / ``False`` explicitly to
        override the env var.

        Connectivity checks for *other* lazily-connecting backends (S3
        boto client, psycopg pools that defer connect) are not
        implemented here — Neo4j is the blessed graph backend so it
        gets the explicit check; the others connect on first use and
        surface errors there. Add a similar wrapper in this method
        when a deployment incident motivates it.
        """
        targets: list[str] = (
            list(store_types) if store_types is not None else list(_PLANE_OF.keys())
        )
        errors: list[tuple[str, Exception]] = []

        # Pre-flight: cheap, declarative checks that surface bad config
        # before we try to instantiate (which can be slow and may swallow
        # the underlying typo behind a driver-level error). Aggregated
        # alongside instantiation failures so the operator sees one
        # consolidated report.
        errors.extend(self._check_uri_formats(targets))
        errors.extend(self._check_embedding_dim_consistency())

        # Schema-fingerprint check (Logic Gap 4.5) — detects substrate
        # swaps and schema-version bumps that would otherwise corrupt
        # data at write time. ``to_write_fp`` is held until after
        # instantiation succeeds so we never lock in a fingerprint for
        # a store that failed to start.
        fp_errors, to_write_fp = self._check_schema_fingerprints(targets)
        errors.extend(fp_errors)

        instantiated_ok: set[str] = set()
        for store_type in targets:
            try:
                self._get(store_type)
                instantiated_ok.add(store_type)
            except Exception as exc:
                # AGGREGATE: not a silent fallback. The exception is held
                # for re-raise via :class:`RegistryValidationError` at the
                # end of this method so an operator deploying a fresh
                # stack sees every misconfigured store in one report
                # instead of fixing them serially. Catch every exception
                # type so a misbehaving plugin backend (e.g. raising a
                # custom Error subclass on missing config) doesn't bypass
                # the aggregate.
                errors.append((store_type, exc))
                logger.warning(
                    "store_registry_validation_failed",
                    store_type=store_type,
                    error=str(exc),
                )

        if _resolve_connectivity_check(check_connectivity):
            errors.extend(self._check_bolt_connectivity())

        # Persist first-boot fingerprints only when nothing else failed.
        # If any store crashed during instantiation, leave the meta file
        # alone — operator fixes the breakage, re-runs validate, and
        # bootstrap completes once everything is green.
        if not errors and to_write_fp:
            stored = self._load_fingerprint_meta()
            for store_type, fp in to_write_fp.items():
                if store_type in instantiated_ok:
                    stored[store_type] = fp
            self._write_fingerprint_meta(stored)

        if errors:
            raise RegistryValidationError(errors)
        logger.info(
            "store_registry_validated",
            store_count=len(targets),
        )

    def _check_bolt_connectivity(self) -> list[tuple[str, Exception]]:
        """Ping every cached Bolt driver. Returns ``(label, exc)`` per failure.

        Covers all Bolt-speaking backends — the underlying
        ``verify_connectivity`` call is the same Bolt-level round-trip
        regardless of which server is at the other end.
        """
        if not self._bolt_drivers:
            return []
        from trellis.stores.bolt_opencypher.base import (  # noqa: PLC0415
            verify_connectivity,
        )

        failures: list[tuple[str, Exception]] = []
        for (uri, user), driver in self._bolt_drivers.items():
            label = f"bolt-driver:{uri}"
            try:
                verify_connectivity(driver)
                logger.debug("bolt_connectivity_ok", uri=uri, user=user)
            except Exception as exc:
                # AGGREGATE: not a silent fallback. The exception is held
                # for re-raise via :class:`RegistryValidationError` by the
                # caller so the operator sees every unreachable driver in
                # one shot. The Bolt driver can raise ``ServiceUnavailable``,
                # ``AuthError``, or arbitrary user-defined subclasses; the
                # broad catch keeps all of them in the aggregate.
                failures.append((label, exc))
                logger.warning(
                    "bolt_connectivity_check_failed",
                    uri=uri,
                    user=user,
                    error=str(exc),
                )
        return failures

    @property
    def knowledge(self) -> _KnowledgePlane:
        """Knowledge-Plane stores (graph, vector, document, blob).

        Populated by client systems via the governed mutation pipeline;
        read by agent-facing surfaces. See
        docs/design/adr-planes-and-substrates.md §2.1.
        """
        return self._knowledge

    @property
    def operational(self) -> _OperationalPlane:
        """Operational-Plane stores (trace, event_log).

        Internal to Trellis — never populated by client systems
        directly. Written by the mutation pipeline as a side effect of
        Knowledge writes; read by admin/debug surfaces and the
        effectiveness feedback loop. See
        docs/design/adr-planes-and-substrates.md §2.1.
        """
        return self._operational

    @property
    def embedding_fn(self) -> Callable[[str], list[float]] | None:
        """Return the configured embedding function, or None.

        Reads the ``embeddings`` section of config.yaml::

            embeddings:
              provider: openai          # openai | custom
              model: text-embedding-3-small
              # api_key: ...            # or set OPENAI_API_KEY env var

        When *provider* is ``"openai"``, returns a callable that uses the
        ``openai`` SDK.  Deployments can also set ``TRELLIS_EMBEDDING_FN`` to a
        dotted import path (e.g. ``mypackage.embeddings.embed``) for fully
        custom providers.

        Returns ``None`` only when no embedding is configured at all
        (no env var, no ``embeddings.provider``). Raises
        :class:`BackendNotInstalledError` when ``provider: openai`` is
        configured but the ``llm-openai`` extra is missing, and
        :class:`ConfigError` when the dotted-path provider can't be
        imported or doesn't resolve to a callable.
        """
        if self._embedding_fn_cache is not _UNSET:
            return self._embedding_fn_cache  # type: ignore[return-value]

        self._embedding_fn_cache = None  # default: not configured

        # 1. Check env var for custom dotted-path callable
        import os  # noqa: PLC0415

        custom_path = os.environ.get("TRELLIS_EMBEDDING_FN")
        if custom_path:
            self._embedding_fn_cache = _import_callable(custom_path)
            if self._embedding_fn_cache is not None:
                logger.info("embedding_fn_loaded", source="env", path=custom_path)
                return self._embedding_fn_cache

        # 2. Check config
        provider = self._embedding_config.get("provider")
        if not provider:
            return None

        if provider == "openai":
            self._embedding_fn_cache = _build_openai_embedding_fn(
                self._embedding_config
            )
        else:
            # Treat provider as a dotted import path
            self._embedding_fn_cache = _import_callable(provider)

        if self._embedding_fn_cache is not None:
            logger.info("embedding_fn_loaded", source="config", provider=provider)

        return self._embedding_fn_cache

    @property
    def budget_config(self) -> Any:
        """Return the :class:`BudgetConfig` resolved from ``retrieval.budgets``.

        Cached across calls.  Returns a default (empty) :class:`BudgetConfig`
        when no ``retrieval.budgets`` section is configured, so callers can
        always call ``.resolve(...)`` on the result.
        """
        if self._budget_config_cache is not _UNSET:
            return self._budget_config_cache

        from trellis.retrieve.budget_config import BudgetConfig  # noqa: PLC0415

        budgets_data = self._retrieval_config.get("budgets")
        self._budget_config_cache = BudgetConfig.from_dict(budgets_data)
        return self._budget_config_cache

    def build_llm_client(self) -> LLMClient | None:
        """Construct an ``LLMClient`` from the ``llm:`` config block, if present.

        Reads the ``llm:`` section of ``config.yaml``::

            llm:
              provider: openai           # or "anthropic"
              api_key_env: OPENAI_API_KEY   # env var name (preferred)
              # api_key: sk-...             # OR literal (discouraged)
              model: gpt-4o-mini
              base_url: https://...       # optional

        ``api_key_env`` is preferred over ``api_key`` so secrets stay out of
        the config file. Returns ``None`` when the config is absent,
        incomplete, references an unknown provider, or cannot resolve an
        API key — these are valid "not configured" states. Raises
        :class:`BackendNotInstalledError` when a provider is configured
        but its optional SDK extra (``llm-openai`` / ``llm-anthropic``)
        is not installed, so an operator who set ``provider: openai``
        without ``pip install trellis-ai[llm-openai]`` sees an explicit
        error instead of silently getting no LLM client. Provider SDK
        imports are deferred to inside this method so core stays
        dependency-free.
        """
        cfg = self._llm_config
        if not cfg:
            logger.debug("llm_client_not_configured")
            return None

        provider = cfg.get("provider")
        if not provider:
            logger.debug("llm_client_provider_missing")
            return None

        api_key = _resolve_api_key(cfg)
        if not api_key:
            logger.debug("llm_client_api_key_unresolved", provider=provider)
            return None

        base_url = cfg.get("base_url")
        model = cfg.get("model")
        masked = _mask_api_key(api_key)

        built: LLMClient | None = None
        chosen_model: str | None = None

        if provider == "openai":
            try:
                from trellis.llm.providers.openai import (  # noqa: PLC0415
                    DEFAULT_CHAT_MODEL,
                    OpenAIClient,
                )
            except ModuleNotFoundError as exc:
                raise BackendNotInstalledError(
                    backend_name="openai",
                    extra="llm-openai",
                ) from exc

            chosen_model = model or DEFAULT_CHAT_MODEL
            try:
                built = OpenAIClient(
                    api_key=api_key,
                    base_url=base_url,
                    default_model=chosen_model,
                )
            except ModuleNotFoundError as exc:
                # Constructor itself imports the openai SDK lazily.
                raise BackendNotInstalledError(
                    backend_name="openai",
                    extra="llm-openai",
                ) from exc

        elif provider == "anthropic":
            try:
                from trellis.llm.providers.anthropic import (  # noqa: PLC0415
                    DEFAULT_MODEL,
                    AnthropicClient,
                )
            except ModuleNotFoundError as exc:
                raise BackendNotInstalledError(
                    backend_name="anthropic",
                    extra="llm-anthropic",
                ) from exc

            chosen_model = model or DEFAULT_MODEL
            try:
                built = AnthropicClient(
                    api_key=api_key,
                    base_url=base_url,
                    default_model=chosen_model,
                )
            except ModuleNotFoundError as exc:
                # Constructor itself imports the anthropic SDK lazily.
                raise BackendNotInstalledError(
                    backend_name="anthropic",
                    extra="llm-anthropic",
                ) from exc

        else:
            # Unknown built-in — try the plugin path.  Entry points
            # under ``trellis.llm.providers`` let third-party packages
            # contribute custom providers (Bedrock, Vertex, vLLM-native,
            # etc.) without touching core.  See
            # ``docs/design/adr-plugin-contract.md``.
            built = _try_llm_provider_plugin(
                provider=provider,
                api_key=api_key,
                base_url=base_url,
                model=model,
            )
            if built is None:
                logger.debug("llm_client_unknown_provider", provider=provider)
                return None
            chosen_model = model  # plugin owns its default

        logger.info(
            "llm_client_built",
            provider=provider,
            model=chosen_model,
            masked_key=masked,
        )
        return built

    def build_embedder_client(self) -> EmbedderClient | None:
        """Construct an ``EmbedderClient`` from the ``llm.embedding:`` sub-block.

        The ``embedding`` sub-block falls back to the parent ``llm:`` block
        for ``provider``, ``api_key`` / ``api_key_env``, and ``base_url``
        when those fields are omitted. This lets a single OpenAI
        ``llm:`` block produce both a chat client and an embedder without
        repetition. Returns ``None`` when the embedder provider is
        configured but no first-party implementation ships for it
        (currently only OpenAI has one — Anthropic returns ``None``).
        Raises :class:`BackendNotInstalledError` when the OpenAI
        embedder is configured but the ``llm-openai`` extra is not
        installed, mirroring :meth:`build_llm_client` semantics.
        """
        parent = self._llm_config
        if not parent:
            logger.debug("embedder_client_not_configured")
            return None

        sub = parent.get("embedding") or {}
        # Merge: sub-block values win; otherwise inherit from parent.
        merged: dict[str, Any] = {
            "provider": sub.get("provider", parent.get("provider")),
            "api_key": sub.get("api_key", parent.get("api_key")),
            "api_key_env": sub.get("api_key_env", parent.get("api_key_env")),
            "base_url": sub.get("base_url", parent.get("base_url")),
            "model": sub.get("model"),
        }

        provider = merged.get("provider")
        if not provider:
            logger.debug("embedder_client_provider_missing")
            return None

        api_key = _resolve_api_key(merged)
        if not api_key:
            logger.debug("embedder_client_api_key_unresolved", provider=provider)
            return None

        base_url = merged.get("base_url")
        model = merged.get("model")
        masked = _mask_api_key(api_key)

        if provider == "openai":
            try:
                from trellis.llm.providers.openai import (  # noqa: PLC0415
                    DEFAULT_EMBEDDING_MODEL,
                    OpenAIEmbedder,
                )
            except ModuleNotFoundError as exc:
                raise BackendNotInstalledError(
                    backend_name="openai",
                    extra="llm-openai",
                ) from exc

            chosen_model = model or DEFAULT_EMBEDDING_MODEL
            try:
                embedder = OpenAIEmbedder(
                    api_key=api_key,
                    base_url=base_url,
                    default_model=chosen_model,
                )
            except ModuleNotFoundError as exc:
                # Constructor itself imports the openai SDK lazily.
                raise BackendNotInstalledError(
                    backend_name="openai",
                    extra="llm-openai",
                ) from exc
            logger.info(
                "embedder_client_built",
                provider=provider,
                model=chosen_model,
                masked_key=masked,
            )
            return embedder

        # Unknown built-in — try plugin path.
        embedder_plugin = _try_llm_embedder_plugin(
            provider=provider,
            api_key=api_key,
            base_url=base_url,
            model=model,
        )
        if embedder_plugin is not None:
            logger.info(
                "embedder_client_built",
                provider=provider,
                model=model,
                masked_key=masked,
                source="plugin",
            )
            return embedder_plugin  # type: ignore[no-any-return]
        logger.debug("embedder_client_unknown_provider", provider=provider)
        return None

    def close(self) -> None:
        """Close all cached stores and shared resources.

        Idempotent — second + later calls find an empty cache and no-op.
        Safe to call from a shutdown handler that may fire twice.
        Failures in any single ``close()`` are logged and skipped so a
        misbehaving backend cannot block cleanup of the rest.

        Stores are closed first; for Neo4j stores with an injected
        driver, ``close()`` is a no-op and the driver is closed by the
        registry afterwards. This avoids racing the registry's shutdown
        sweep with a store's individual ``close()`` call.

        Lifecycle:

        * The FastAPI lifespan in :mod:`trellis_api.app` calls
          ``close()`` automatically on uvicorn shutdown.
        * Direct callers (CLI subcommands, tests, SDK local mode) should
          use the context-manager form to get the same guarantee::

              with StoreRegistry.from_config_dir() as registry:
                  ...

          Without the ``with`` block, file descriptors live until
          process exit. Fine for short-lived CLI invocations; leaks
          across long-running processes that hold a registry.
        """
        for store in self._cache.values():
            try:
                store.close()
            except Exception:
                # GRACEFUL-DEGRADATION: shutdown sweep. One misbehaving
                # store must not block cleanup of the others, or shutdown
                # leaks every resource after the first failure. The warning
                # is the observability signal; raising here would defeat
                # the purpose of the sweep.
                logger.warning(
                    "store_close_failed",
                    store=type(store).__name__,
                    exc_info=True,
                )
        self._cache.clear()
        for key, driver in self._bolt_drivers.items():
            try:
                driver.close()
            except Exception:
                # GRACEFUL-DEGRADATION: same shutdown-sweep rationale as
                # the store loop above. We log with traceback so the
                # operator can diagnose flaky driver shutdowns post-hoc.
                logger.warning(
                    "bolt_driver_close_failed",
                    uri=key[0],
                    user=key[1],
                    exc_info=True,
                )
        self._bolt_drivers.clear()

    def __enter__(self) -> StoreRegistry:
        """Enable use as a context manager — see :meth:`close`."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        """Close on context-manager exit, even when an exception escapes the body."""
        self.close()
