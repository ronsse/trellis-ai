"""ArcadeDBGraphStore — thin adapter over :class:`BoltOpenCypherGraphStore`.

The shared base class' Cypher payload runs against ArcadeDB unchanged.
This class swaps in ArcadeDB-specific driver construction (basic auth
over Bolt) and idempotent HTTP-based database creation, then defers
everything else to the parent. See
:mod:`trellis.stores.bolt_opencypher.graph` for the SCD-2 + Cypher
contract and :class:`ArcadeDBVectorStore` for the paired vector path
(SQL over HTTP, not Cypher).

Self-hosting note: this store connects over Bolt (default port 7687),
which a stock self-hosted ArcadeDB does **not** expose by default — only
the HTTP endpoint (2480) is on. Start the server (or build the image)
with the Bolt plugin enabled::

    -Darcadedb.server.plugins=Bolt:com.arcadedb.bolt.BoltProtocolPlugin

A connection-refused on 7687 while 2480 answers means the plugin flag is
missing. See ``docs/design/adr-arcadedb-blessed-substrate.md``
("Self-hosting requirement") for the full deployment note.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from trellis.errors import ConfigError
from trellis.stores.arcadedb.base import (
    build_arcadedb_driver,
    derive_http_url_from_bolt,
    ensure_database,
    execute_sql,
)
from trellis.stores.base.registry import RegistryContext
from trellis.stores.bolt_opencypher.base import (
    BoltDriverConfig,
    check_driver_installed,
    registry_driver_cache,
)
from trellis.stores.bolt_opencypher.graph import BoltOpenCypherGraphStore

if TYPE_CHECKING:
    from neo4j import Driver

logger = structlog.get_logger(__name__)
_REGISTRY_MIGRATIONS_KEY = f"{__name__}:provenance_migrations"


#: ArcadeDB schema-typed properties for the edge provenance columns
#: (Phase 3 of ``adr-graph-ontology.md`` §6.4 / item 2 of the
#: self-improvement program). ArcadeDB stores relationship properties
#: untyped by default; declaring them with ``CREATE PROPERTY`` opts the
#: column into ArcadeDB's type-coercion + constraint surface so
#: ``confidence`` carries a server-enforced ``MIN/MAX`` range matching
#: the Python-boundary validator. ``extractor_tier`` is left untyped
#: beyond STRING — ArcadeDB has no enum constraint, so the allowlist is
#: enforced by :func:`trellis.stores.base.edge_provenance.validate_edge_provenance`.
#:
#: ``CREATE PROPERTY ... IF NOT EXISTS`` is idempotent — re-runs are
#: cheap no-ops against an already-migrated database. The trailing
#: ``CREATE EDGE TYPE EDGE IF NOT EXISTS`` mirrors the vector store's
#: ``CREATE VERTEX TYPE Node IF NOT EXISTS`` pattern: the Cypher write
#: path auto-creates the edge type on first use, but we declare it
#: explicitly so ``CREATE PROPERTY`` has a target schema to attach to
#: even on a fresh database where no edges have been written yet.
_ARCADEDB_EDGE_PROVENANCE_SCHEMA: tuple[str, ...] = (
    "CREATE EDGE TYPE EDGE IF NOT EXISTS",
    "CREATE PROPERTY EDGE.source_trace_id IF NOT EXISTS STRING",
    "CREATE PROPERTY EDGE.agent_id IF NOT EXISTS STRING",
    # ArcadeDB FLOAT is 32-bit; FLOAT supports MIN/MAX constraints.
    # The Python validator enforces the same [0.0, 1.0] range with a
    # message that points at the offending value.
    "CREATE PROPERTY EDGE.confidence IF NOT EXISTS FLOAT (MIN 0.0, MAX 1.0)",
    "CREATE PROPERTY EDGE.evidence_ref IF NOT EXISTS STRING",
    # ArcadeDB has no enum constraint — the allowlist
    # ({DETERMINISTIC, HYBRID, LLM}) is enforced at the Python
    # boundary in ``validate_edge_provenance``. Declaring the property
    # as STRING still pulls it into ArcadeDB's schema so queries can
    # filter on it without JSON-extracting the properties bag.
    "CREATE PROPERTY EDGE.extractor_tier IF NOT EXISTS STRING",
)


class ArcadeDBGraphStore(BoltOpenCypherGraphStore):
    """ArcadeDB-backed graph store.

    The Cypher payload + SCD-2 logic live in the parent class. This
    subclass builds a Bolt driver with basic auth and (optionally)
    creates the target ArcadeDB database via the HTTP admin endpoint
    before any Bolt session opens.
    """

    @classmethod
    def prepare_registry_params(  # noqa: PLR0912, PLR0915
        cls,
        ctx: RegistryContext,
        store_type: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Resolve registry-owned ArcadeDB connection and migration state."""
        if "driver" in params:
            return params

        uri = params.get("uri") or ctx.env.get("TRELLIS_ARCADEDB_URI")
        if not uri:
            msg = (
                "arcadedb backend requires 'uri' in config or "
                "TRELLIS_ARCADEDB_URI env var (e.g. bolt://host:7687)"
            )
            raise ConfigError(msg, setting=f"stores.{store_type}.uri")
        user = params.get("user") or ctx.env.get("TRELLIS_ARCADEDB_USER") or "root"
        password = params.get("password") or ctx.env.get("TRELLIS_ARCADEDB_PASSWORD")
        database = (
            params.get("database")
            or ctx.env.get("TRELLIS_ARCADEDB_DATABASE")
            or "trellis"
        )
        http_url = params.get("http_url") or ctx.env.get("TRELLIS_ARCADEDB_HTTP_URL")
        admin_user = (
            params.get("admin_user")
            or ctx.env.get("TRELLIS_ARCADEDB_ADMIN_USER")
            or user
        )
        admin_password = (
            params.get("admin_password")
            or ctx.env.get("TRELLIS_ARCADEDB_ADMIN_PASSWORD")
            or password
        )
        key = (uri, user)
        drivers = registry_driver_cache(ctx)
        migrated: set[tuple[str, str]] = ctx.shared.setdefault(
            _REGISTRY_MIGRATIONS_KEY,
            set(),
        )
        prepared = {
            key: value
            for key, value in params.items()
            if key
            not in {
                "driver_config",
                "ensure_database_exists",
                "admin_user",
                "admin_password",
            }
        }
        prepared.update(uri=uri, user=user, database=database)

        if key in drivers:
            cls._prepare_cached_registry_driver(
                ctx,
                migrated=migrated,
                key=key,
                http_url=http_url,
                uri=uri,
                user=admin_user,
                password=admin_password,
                database=database,
                params=prepared,
            )
            prepared.pop("password", None)
            prepared["driver"] = drivers[key]
            return prepared

        if not password:
            msg = (
                "arcadedb backend requires 'password' in config or "
                "TRELLIS_ARCADEDB_PASSWORD env var"
            )
            raise ConfigError(msg, setting=f"stores.{store_type}.password")
        admin_password = admin_password or password

        raw_config = params.get("driver_config")
        if raw_config is None:
            driver_config: BoltDriverConfig | None = None
        elif isinstance(raw_config, BoltDriverConfig):
            driver_config = raw_config
        elif isinstance(raw_config, dict):
            driver_config = BoltDriverConfig(**raw_config)
        else:
            msg = (
                "driver_config must be a BoltDriverConfig, a dict, or omitted; "
                f"got {type(raw_config).__name__}"
            )
            raise TypeError(msg)

        if params.get("ensure_database_exists", True):
            http_url = http_url or derive_http_url_from_bolt(uri)
            if not http_url:
                msg = (
                    "arcadedb backend with ensure_database_exists=True "
                    "needs an http_url (or a parseable host in the Bolt uri)."
                )
                raise ConfigError(msg, setting=f"stores.{store_type}.http_url")
            ensure_database(http_url, admin_user, admin_password, database)

        resolved_http_url = http_url or derive_http_url_from_bolt(uri)
        if resolved_http_url:
            cls._init_arcadedb_edge_provenance_schema(
                http_url=resolved_http_url,
                user=admin_user,
                password=admin_password,
                database=database,
            )
        else:
            ctx.emit_warning(
                "ArcadeDB provenance migration skipped: no HTTP URL could be resolved"
            )
        migrated.add(key)

        driver = build_arcadedb_driver(uri, user, password, config=driver_config)
        drivers[key] = driver
        ctx.register_closer(driver.close)
        prepared.pop("password", None)
        prepared["driver"] = driver
        if resolved_http_url and not prepared.get("http_url"):
            prepared["http_url"] = resolved_http_url
        return prepared

    @classmethod
    def _prepare_cached_registry_driver(
        cls,
        ctx: RegistryContext,
        *,
        migrated: set[tuple[str, str]],
        key: tuple[str, str],
        http_url: str | None,
        uri: str,
        user: str,
        password: str | None,
        database: str,
        params: dict[str, Any],
    ) -> None:
        if key in migrated:
            return
        resolved_http_url = http_url or derive_http_url_from_bolt(uri)
        if resolved_http_url and password:
            cls._init_arcadedb_edge_provenance_schema(
                http_url=resolved_http_url,
                user=user,
                password=password,
                database=database,
            )
            migrated.add(key)
            if not params.get("http_url"):
                params["http_url"] = resolved_http_url
            return
        ctx.emit_warning(
            "ArcadeDB provenance migration skipped for a cached driver: "
            "HTTP URL or password unavailable"
        )

    def __init__(
        self,
        uri: str,
        *,
        user: str = "root",
        password: str | None = None,
        database: str = "trellis",
        driver: Driver | None = None,
        driver_config: BoltDriverConfig | None = None,
        http_url: str | None = None,
        ensure_database_exists: bool = True,
        admin_user: str | None = None,
        admin_password: str | None = None,
    ) -> None:
        """Initialize an ArcadeDB graph store.

        Parameters
        ----------
        uri
            Bolt URI for the ArcadeDB server, e.g.
            ``bolt://arcadedb.internal:7687``.
        user, password
            Basic-auth credentials for **runtime** operations (the Bolt
            connection that serves reads/writes). ``root`` is the
            conventional admin user for ArcadeDB; production deployments
            should create a dedicated least-privilege user and reserve
            admin credentials for the ``admin_user`` / ``admin_password``
            pair below (issue #193).
        admin_user, admin_password
            Optional privileged credentials used **only** for the
            init/migration phase — database creation
            (``ensure_database``) and the typed-property schema DDL.
            When omitted, ``user`` / ``password`` are used for those
            phases too (single-credential deployments keep working).
            The Bolt runtime driver is never built with these.
        database
            Target ArcadeDB database name. Created on first boot if
            ``ensure_database_exists=True``.
        driver
            Optional pre-built driver, typically injected by
            :class:`StoreRegistry` so the graph + vector pair share one
            connection pool. When set, ``password`` and
            ``driver_config`` must be ``None`` (mutually exclusive
            configuration paths).
        driver_config
            Optional :class:`BoltDriverConfig` overriding the default
            production-safe driver kwargs. Ignored if ``driver`` is set.
        http_url
            Base URL for ArcadeDB's HTTP REST endpoint, e.g.
            ``http://arcadedb.internal:2480``. Required when
            ``ensure_database_exists=True`` (the HTTP endpoint is the
            documented path for database creation). When omitted, the
            store assumes the database is already present and the HTTP
            URL isn't needed.
        ensure_database_exists
            When True (the default), creates the named database via the
            HTTP REST endpoint at startup if it doesn't already exist.
            Set False to skip the check — appropriate for deployments
            where the database is provisioned out-of-band (e.g. by
            Terraform) and the HTTP endpoint isn't exposed to the
            store's network.
        """
        check_driver_installed()

        # Effective credentials for the privileged init/migration phase.
        # Falls back to the runtime pair so single-credential deployments
        # are unchanged; when the admin pair is set, runtime Bolt traffic
        # never carries it (issue #193).
        migration_user = admin_user or user
        migration_password = admin_password if admin_password is not None else password

        # Injected-driver path: registry shared a driver across the
        # graph + future Bolt siblings. ``close()`` is a no-op.
        if driver is not None:
            if password is not None or driver_config is not None:
                msg = (
                    "Pass either ``driver`` (caller-owned) or "
                    "``password`` + ``driver_config`` (store-owned), not both."
                )
                raise ValueError(msg)
            super().__init__(driver=driver, database=database, owns_driver=False)
            # Property-schema migration requires HTTP credentials. The
            # registry path forwards ``http_url`` (so we know it
            # resolved one) but strips ``password`` to preserve the
            # mutex with ``driver``. In that case the registry already
            # ran the migration before injecting the driver — log at
            # debug, no missing-constraint risk.
            #
            # If ``http_url`` plus a usable migration credential are
            # present, run the migration here. The runtime ``password``
            # can't appear alongside ``driver`` (mutex above), but the
            # admin pair legitimately can — it never builds a driver, so
            # a direct caller with an injected driver may still hand us
            # DDL credentials. Otherwise — no http_url at all — fall
            # back to the warning: a direct caller missed credentials
            # and the FLOAT MIN/MAX constraint will not be installed.
            if http_url is not None and migration_password is not None:
                self._init_arcadedb_edge_provenance_schema(
                    http_url=http_url,
                    user=migration_user,
                    password=migration_password,
                    database=database,
                )
            elif http_url is not None:
                logger.debug(
                    "arcadedb_provenance_schema_migration_handled_by_registry",
                    reason=(
                        "http_url forwarded alongside injected driver but "
                        "password stripped (registry mutex). Registry runs "
                        "the typed-property migration itself before "
                        "injecting the driver."
                    ),
                )
            else:
                logger.warning(
                    "arcadedb_provenance_schema_migration_skipped_injected_driver",
                    reason=(
                        "http_url not supplied alongside injected driver; "
                        "the FLOAT MIN/MAX constraint on edge.confidence "
                        "will not be installed. Pass http_url (and run "
                        "the migration externally) to enable the schema-"
                        "typed property constraint, or construct via "
                        "StoreRegistry which handles this automatically."
                    ),
                )
            logger.info(
                "arcadedb_graph_store_initialized",
                uri=uri,
                database=database,
            )
            return

        # Store-owned-driver path: build our own driver + (optionally)
        # idempotently create the target database via HTTP first.
        if password is None:
            msg = "password is required when ``driver`` is not provided"
            raise ValueError(msg)
        # Recompute now that ``password`` is narrowed to ``str`` — the
        # early binding above is typed Optional for the injected-driver
        # branch, but every privileged call below needs a real secret.
        migration_password = admin_password if admin_password is not None else password
        if ensure_database_exists:
            if http_url is None:
                msg = (
                    "http_url is required when ensure_database_exists=True "
                    "(database creation goes through the HTTP REST endpoint, "
                    "not Bolt). Either provide http_url or set "
                    "ensure_database_exists=False if the database is "
                    "pre-provisioned."
                )
                raise ValueError(msg)
            ensure_database(http_url, migration_user, migration_password, database)
        driver = build_arcadedb_driver(uri, user, password, config=driver_config)
        super().__init__(driver=driver, database=database, owns_driver=True)
        # Idempotently declare schema-typed properties for the
        # provenance columns + the FLOAT MIN/MAX constraint on
        # ``confidence``. Runs over HTTP SQL because openCypher does
        # not expose ArcadeDB's typed-property DDL. Safe to call on
        # every boot — every statement is ``IF NOT EXISTS``. Uses the
        # migration credential pair — the only DDL in the store's life.
        if http_url is not None:
            self._init_arcadedb_edge_provenance_schema(
                http_url=http_url,
                user=migration_user,
                password=migration_password,
                database=database,
            )
        else:
            logger.warning(
                "arcadedb_provenance_schema_migration_skipped_no_http_url",
                reason=(
                    "http_url not supplied; provenance properties will be "
                    "created lazily on first write without typed-property "
                    "constraints. Pass http_url to enable the FLOAT "
                    "MIN/MAX constraint on confidence."
                ),
            )
        logger.info(
            "arcadedb_graph_store_initialized",
            uri=uri,
            database=database,
        )

    @staticmethod
    def _init_arcadedb_edge_provenance_schema(
        *,
        http_url: str,
        user: str,
        password: str,
        database: str,
    ) -> None:
        """Run the idempotent ``CREATE PROPERTY`` migration via HTTP SQL.

        Each statement is ``IF NOT EXISTS`` so calling this against an
        already-migrated database is a no-op. Failures bubble up as
        ``RuntimeError`` from :func:`execute_sql` — the registry will
        surface them at boot rather than as opaque errors on first
        write.
        """
        for stmt in _ARCADEDB_EDGE_PROVENANCE_SCHEMA:
            execute_sql(http_url, user, password, database, stmt)
        logger.info(
            "arcadedb_edge_provenance_schema_migrated",
            database=database,
            statements=len(_ARCADEDB_EDGE_PROVENANCE_SCHEMA),
        )

    def close(self) -> None:
        owns = self._owns_driver
        super().close()
        if owns:
            logger.info("arcadedb_graph_store_closed")
        else:
            logger.debug("arcadedb_graph_store_close_noop_injected_driver")
