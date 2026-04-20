"""Store registry — dependency injection for store backends."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from trellis.stores.base import (
    BlobStore,
    DocumentStore,
    EventLog,
    GraphStore,
    TraceStore,
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

# Cache of merged (built-in + plugin) backend maps.  Populated lazily on
# first access so that installing a plugin wheel into an already-running
# process doesn't require a restart.  Keyed by store_type ("trace", ...).
_MERGED_BACKENDS_CACHE: dict[str, dict[str, tuple[str, str]]] = {}

# Backend name -> (module_path, class_name)
_BUILTIN_BACKENDS: dict[str, dict[str, tuple[str, str]]] = {
    "trace": {
        "sqlite": ("trellis.stores.sqlite.trace", "SQLiteTraceStore"),
        "postgres": ("trellis.stores.postgres.trace", "PostgresTraceStore"),
    },
    "document": {
        "sqlite": ("trellis.stores.sqlite.document", "SQLiteDocumentStore"),
        "postgres": ("trellis.stores.postgres.document", "PostgresDocumentStore"),
    },
    "graph": {
        "sqlite": ("trellis.stores.sqlite.graph", "SQLiteGraphStore"),
        "postgres": ("trellis.stores.postgres.graph", "PostgresGraphStore"),
    },
    "vector": {
        "sqlite": ("trellis.stores.sqlite.vector", "SQLiteVectorStore"),
        "pgvector": ("trellis.stores.pgvector.store", "PgVectorStore"),
        "lancedb": ("trellis.stores.lancedb.store", "LanceVectorStore"),
    },
    "event_log": {
        "sqlite": ("trellis.stores.sqlite.event_log", "SQLiteEventLog"),
        "postgres": ("trellis.stores.postgres.event_log", "PostgresEventLog"),
    },
    "blob": {
        "local": ("trellis.stores.local.blob", "LocalBlobStore"),
        "s3": ("trellis.stores.s3.blob", "S3BlobStore"),
    },
}


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

    builtins = _BUILTIN_BACKENDS.get(store_type, {})
    try:
        from trellis.plugins import GROUP_STORES, merge_with_builtins  # noqa: PLC0415
    except Exception:
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


def _import_callable(dotted_path: str) -> Callable[[str], list[float]] | None:
    """Import a callable from a dotted module path (e.g. ``pkg.mod.func``)."""
    import importlib  # noqa: PLC0415

    try:
        module_path, _, attr_name = dotted_path.rpartition(".")
        module = importlib.import_module(module_path)
        fn = getattr(module, attr_name)
        if callable(fn):
            return fn  # type: ignore[no-any-return]
        logger.warning("embedding_fn_not_callable", path=dotted_path)
    except Exception:
        logger.warning("embedding_fn_import_failed", path=dotted_path, exc_info=True)
    return None


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
) -> Callable[[str], list[float]] | None:
    """Build an embedding callable using the OpenAI SDK."""
    try:
        import openai  # noqa: PLC0415
    except ModuleNotFoundError:
        logger.warning("embedding_fn_openai_not_installed")
        return None

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
        self._config = config or {}
        self._stores_dir = stores_dir
        self._embedding_config = embedding_config or {}
        self._retrieval_config = retrieval_config or {}
        self._llm_config = llm_config or {}
        self._cache: dict[str, Any] = {}
        self._embedding_fn_cache: Callable[[str], list[float]] | None = _UNSET
        self._budget_config_cache: Any = _UNSET

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
        """Create a registry from XPG config directory."""
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
            try:
                import yaml  # noqa: PLC0415

                data = yaml.safe_load(config_path.read_text()) or {}
                store_config = data.get("stores", {})
                embedding_config = data.get("embeddings", {})
                retrieval_config = data.get("retrieval", {})
                llm_config = data.get("llm", {})
                if data.get("data_dir"):
                    data_dir = Path(data["data_dir"])
            except Exception:
                logger.warning("registry_config_load_failed", path=str(config_path))

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

        registry = _get_merged_backends(store_type)
        if backend not in registry:
            msg = f"Unknown backend '{backend}' for store type '{store_type}'"
            raise ValueError(msg)

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
                raise ValueError(msg)
            self._stores_dir.mkdir(parents=True, exist_ok=True)
            db_names = {
                "trace": "traces.db",
                "document": "documents.db",
                "graph": "graph.db",
                "vector": "vectors.db",
                "event_log": "events.db",
            }
            params["db_path"] = self._stores_dir / db_names[store_type]

        # For lancedb backend, default to stores_dir/lancedb/
        if backend == "lancedb" and "uri" not in params:
            if self._stores_dir is None:
                msg = "stores_dir must be set for lancedb backend without explicit uri"
                raise ValueError(msg)
            self._stores_dir.mkdir(parents=True, exist_ok=True)
            params["uri"] = str(self._stores_dir / "lancedb")

        # For local blob backend, default to stores_dir/blobs/
        if backend == "local" and "root_dir" not in params:
            if self._stores_dir is None:
                msg = (
                    "stores_dir must be set for local blob backend"
                    " without explicit root_dir"
                )
                raise ValueError(msg)
            params["root_dir"] = self._stores_dir / "blobs"

        # For postgres backends, default DSN from env
        if backend == "postgres" and "dsn" not in params:
            import os  # noqa: PLC0415

            dsn = os.environ.get("TRELLIS_PG_DSN")
            if not dsn:
                msg = (
                    "dsn must be set for postgres backends"
                    " (config or TRELLIS_PG_DSN env var)"
                )
                raise ValueError(msg)
            params["dsn"] = dsn

        # For pgvector backend, default DSN from env
        if backend == "pgvector" and "dsn" not in params:
            import os  # noqa: PLC0415

            dsn = os.environ.get("TRELLIS_PG_DSN")
            if not dsn:
                msg = (
                    "dsn must be set for pgvector backend"
                    " (config or TRELLIS_PG_DSN env var)"
                )
                raise ValueError(msg)
            params["dsn"] = dsn

        # For s3 backend, default bucket from env
        if backend == "s3" and "bucket" not in params:
            import os  # noqa: PLC0415

            bucket = os.environ.get("TRELLIS_S3_BUCKET")
            if not bucket:
                msg = (
                    "bucket must be set for s3 backend"
                    " (config or TRELLIS_S3_BUCKET env var)"
                )
                raise ValueError(msg)
            params["bucket"] = bucket

        logger.info("store_instantiated", store_type=store_type, backend=backend)
        return cls(**params)

    def _get(self, store_type: str) -> Any:
        if store_type not in self._cache:
            self._cache[store_type] = self._instantiate(store_type)
        return self._cache[store_type]

    @property
    def trace_store(self) -> TraceStore:
        return self._get("trace")  # type: ignore[no-any-return]

    @property
    def document_store(self) -> DocumentStore:
        return self._get("document")  # type: ignore[no-any-return]

    @property
    def graph_store(self) -> GraphStore:
        return self._get("graph")  # type: ignore[no-any-return]

    @property
    def vector_store(self) -> VectorStore:
        return self._get("vector")  # type: ignore[no-any-return]

    @property
    def event_log(self) -> EventLog:
        return self._get("event_log")  # type: ignore[no-any-return]

    @property
    def blob_store(self) -> BlobStore:
        return self._get("blob")  # type: ignore[no-any-return]

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
        the config file. Returns ``None`` — never raises — when the config
        is absent, incomplete, references an unknown provider, cannot
        resolve an API key, or when the provider SDK is not installed.
        Provider SDK imports are deferred to inside this method so core
        stays dependency-free.
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
            except ModuleNotFoundError:
                logger.debug("llm_client_sdk_not_installed", provider=provider)
                return None

            chosen_model = model or DEFAULT_CHAT_MODEL
            try:
                built = OpenAIClient(
                    api_key=api_key,
                    base_url=base_url,
                    default_model=chosen_model,
                )
            except ModuleNotFoundError:
                logger.debug("llm_client_sdk_not_installed", provider=provider)
                return None

        elif provider == "anthropic":
            try:
                from trellis.llm.providers.anthropic import (  # noqa: PLC0415
                    DEFAULT_MODEL,
                    AnthropicClient,
                )
            except ModuleNotFoundError:
                logger.debug("llm_client_sdk_not_installed", provider=provider)
                return None

            chosen_model = model or DEFAULT_MODEL
            try:
                built = AnthropicClient(
                    api_key=api_key,
                    base_url=base_url,
                    default_model=chosen_model,
                )
            except ModuleNotFoundError:
                logger.debug("llm_client_sdk_not_installed", provider=provider)
                return None

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
        repetition. Returns ``None`` — never raises — under the same
        conditions as :meth:`build_llm_client`. Currently only the OpenAI
        provider ships a first-party embedder implementation; Anthropic
        and other providers return ``None``.
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
            except ModuleNotFoundError:
                logger.debug("embedder_client_sdk_not_installed", provider=provider)
                return None

            chosen_model = model or DEFAULT_EMBEDDING_MODEL
            try:
                embedder = OpenAIEmbedder(
                    api_key=api_key,
                    base_url=base_url,
                    default_model=chosen_model,
                )
            except ModuleNotFoundError:
                logger.debug("embedder_client_sdk_not_installed", provider=provider)
                return None
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
        """Close all cached stores."""
        for store in self._cache.values():
            try:
                store.close()
            except Exception:
                logger.warning("store_close_failed", store=type(store).__name__)
        self._cache.clear()
