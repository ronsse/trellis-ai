"""Command and operation schemas for the governed mutation pipeline."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import Field

from trellis.core.base import VersionedModel, utc_now
from trellis.core.ids import generate_ulid
from trellis.core.text_safety import contains_nul_or_lone_surrogate


class Operation(StrEnum):
    """All mutation verbs supported by the pipeline."""

    # Ingest
    TRACE_INGEST = "trace.ingest"
    TRACE_APPEND_STEP = "trace.append_step"
    TRACE_RECORD_OUTCOME = "trace.record_outcome"
    EVIDENCE_INGEST = "evidence.ingest"
    EVIDENCE_ATTACH = "evidence.attach"

    # Curate
    PRECEDENT_PROMOTE = "precedent.promote"
    PRECEDENT_DEMOTE = "precedent.demote"
    PRECEDENT_REINSTATE = "precedent.reinstate"
    ENTITY_CREATE = "entity.create"
    ENTITY_UPDATE = "entity.update"
    ENTITY_MERGE = "entity.merge"
    ALIAS_UPSERT = "alias.upsert"
    LINK_CREATE = "link.create"
    LINK_REMOVE = "link.remove"
    LABEL_ADD = "label.add"
    LABEL_REMOVE = "label.remove"
    FEEDBACK_RECORD = "feedback.record"
    OBSERVATION_RECORD = "observation.record"
    MEASUREMENT_RECORD = "measurement.record"
    CLASSIFICATION_SET = "classification.set"

    # Maintain
    REDACTION_APPLY = "redaction.apply"
    RETENTION_PRUNE = "retention.prune"
    RETENTION_RESTORE = "retention.restore"

    # Admin — each routed only through its own admin-scope executor, never
    # through ``create_curate_handlers``. SCHEDULE_UPDATE and
    # JOB_RUN_REQUEST go through ``build_schedule_executor`` (see
    # ``mutate/schedule_handlers.py``); SETTINGS_UPDATE is a *separate*
    # executor, ``build_settings_executor`` (see
    # ``mutate/handlers.py::create_settings_handlers``) — the two
    # executors are disjoint, not one shared admin executor (gate #860b,
    # follow-up 9).
    SCHEDULE_UPDATE = "schedule.update"
    JOB_RUN_REQUEST = "job.run_request"
    SETTINGS_UPDATE = "settings.update"
    CLASSIFY_RULE_APPLY = "classify_rule.apply"
    CLASSIFY_RULE_REVOKE = "classify_rule.revoke"
    # Credentials: routed only through ``build_api_keys_executor`` (admin
    # REST, ``trellis admin api-keys``, MCP revoke). Never in
    # ``create_curate_handlers``, so ``execute_mutation`` cannot mint a key.
    API_KEY_MINT = "api_key.mint"
    API_KEY_REVOKE = "api_key.revoke"


class CommandStatus(StrEnum):
    """Outcome status of a command execution."""

    SUCCESS = "success"
    REJECTED = "rejected"
    FAILED = "failed"
    DUPLICATE = "duplicate"


class BatchStrategy(StrEnum):
    """Strategy for executing a batch of commands."""

    SEQUENTIAL = "sequential"
    STOP_ON_ERROR = "stop_on_error"
    CONTINUE_ON_ERROR = "continue_on_error"


class Command(VersionedModel):
    """A mutation command submitted to the pipeline.

    ``requested_by`` is the audit-trail identifier for whoever submitted
    the command. Convention is ``<surface>:<verb>`` where surface is one
    of ``cli`` / ``api`` / ``mcp`` / ``worker`` and verb is the entry
    point (e.g. ``cli:link``, ``api:bulk-ingest``, ``mcp:save_memory``,
    ``worker:dbt-extract``). The ``"unknown"`` default is a safety
    fallback that should never appear in production events — every call
    site is expected to stamp its own identity.
    """

    command_id: str = Field(default_factory=generate_ulid)
    operation: Operation
    target_id: str | None = None
    target_type: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    requested_by: str = "unknown"
    idempotency_key: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


#: The sole ``metadata["rejection_reason"]`` value a policy-gate refusal
#: carries. A frozenset of one so a reader comparing it against
#: :data:`STORE_REFUSAL_REASONS` sees the same shape, and so a second
#: policy-labelled reason, if one is ever added, is one line here instead
#: of a second hand-maintained check at each caller.
POLICY_REFUSAL_REASONS = frozenset({"policy_violation"})

#: ``metadata["rejection_reason"]`` values that mean the deployment's store
#: is the obstacle -- degraded or stale -- rather than the command's own
#: input. Shared so :func:`trellis_cli.exit_codes.refusal_exit_code` and
#: the name-alias backfill's per-failure labelling
#: (:func:`trellis.mutate.name_aliases._classify_result`) classify a
#: reason exactly once; a reason added to the pipeline is categorized for
#: both callers by changing it here.
STORE_REFUSAL_REASONS = frozenset(
    {"degraded_store_write", "stale_store_write", "policy_scope_unresolved"}
)


class CommandResult(VersionedModel):
    """Result of executing a command through the pipeline.

    A REJECTED result names why in ``metadata["rejection_reason"]``, the
    ``reason`` its ``MUTATION_REJECTED`` event carries: ``policy_violation``,
    ``immutable_core``, ``validate`` (the operation registry's arg check),
    or a handler's ``ValidationError.code`` (``handler_validate`` when it
    set none). A caller holding only the result, such as the CLI choosing
    an exit code, can then tell a policy refusal from any other using
    :data:`POLICY_REFUSAL_REASONS` and :data:`STORE_REFUSAL_REASONS`.
    """

    command_id: str
    status: CommandStatus
    operation: Operation
    target_id: str | None = None
    created_id: str | None = None
    message: str = ""
    warnings: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    executed_at: datetime = Field(default_factory=utc_now)


class CommandBatch(VersionedModel):
    """A batch of commands to execute.

    ``requested_by`` follows the same ``<surface>:<verb>`` convention
    as :class:`Command` — see its docstring.
    """

    batch_id: str = Field(default_factory=generate_ulid)
    commands: list[Command]
    strategy: BatchStrategy = BatchStrategy.STOP_ON_ERROR
    requested_by: str = "unknown"
    metadata: dict[str, Any] = Field(default_factory=dict)


#: Operation-specific ``args`` keys whose value is a stored identifier or an
#: exact-match lookup key -- never scanned generically, because free-text
#: args (names, descriptions, reasons, comments) must stay accepted. Found
#: by reading every handler in ``mutate/handlers.py``: the value each key
#: names is either minted fresh (``entity.create``'s ``entity_id``,
#: ``alias.upsert``'s owner ``entity_id``, ``link.create``'s edge endpoints
#: and its ``edge_kind`` -- stored as ``edge_type`` and filtered on exactly
#: by ``get_edges`` --, ``precedent.promote``'s trace-sourced ``trace_id``),
#: an exact-match lookup key against a node that must already exist
#: (``entity.update``'s ``entity_id``: the id ``EntityUpdateHandler`` reads
#: with ``get_node`` and rewrites a new version of via
#: ``_rewrite_current_node`` -- neither minted nor read back from a stored
#: event), or read back later by an exact-match query against a stored
#: event's ``entity_id``/payload field (``precedent.demote``/``reinstate``,
#: ``label.add``/``remove``'s ``target_id`` and its ``label`` -- checked for
#: list membership against the stored ``labels`` property --, and
#: ``feedback.record``'s ``target_id`` and its join key ``pack_id`` -- see
#: ``_was_auto_promoted``, ``_is_promoted_precedent_event`` in handlers.py
#: and ``join_pack_feedback`` in ``learning/pack_observations.py``).
#: ``entity_type`` (both ``entity.create``'s and ``entity.update``'s) fits
#: none of those three: it is stored verbatim as the node's ``node_type``,
#: never minted and never read back from an event, and is compared exactly
#: against ``"precedent"`` by the retype/lifecycle guards in both handlers.
#:
#: A key is deliberately absent when: the handler already refuses a NUL
#: itself (``classify_rule.apply``/``revoke``'s ``key``/``target``/
#: ``source_event_id``, via ``_validate_classify_rule_identifier``); the
#: value only ever looks up a node that must already exist, and a failed
#: lookup refuses cleanly either way (``redaction.apply``'s ``target_id``
#: -- leaving it out also keeps governed redaction able to purge a row an
#: id-minting bug stored before this check existed); or the value is free
#: text/classification, not an identifier (``alias.upsert``'s
#: ``source_system``, every ``reason``/``name``/``description``). A few
#: lookup keys are left out because they are name-derived, not an
#: identifier any caller chooses: ``alias.upsert``'s ``raw_id`` and its
#: ``stale_owner_name_key``, and the name-alias binding ``entity.create``
#: and ``entity.update`` each perform on their own ``name`` arg
#: (``_bind_entity_name_alias`` in handlers.py, which derives one key from
#: the name and passes it as both ``raw_id`` and ``stale_owner_name_key``
#: to ``bind_alias_if_absent``). The name-alias backfill
#: (``mutate/name_aliases.py``) and that internal binding both derive the
#: key from the entity's free-text name, which may hold a NUL, so refusing
#: it would fail every backfill pass for such an entity while the
#: create/update path stores the same value anyway. This mapping's size
#: has a hand-read floor (``tests/unit/mutate/test_commands.py``) so
#: deleting an entry fails a test.
#:
#: An id that arrives nested inside a sub-object or a list in ``args`` --
#: rather than as a bare top-level string -- cannot be named here, because
#: the scan below reads ``command.args.get(key)`` directly. Those are
#: ``NESTED_ID_ARG_PATHS``'s job: ``trace.ingest``'s ``trace.trace_id``,
#: ``evidence.ingest``'s ``evidence.doc_id``, ``entity.create``'s and
#: ``entity.update``'s ``document_ids``, ``observation.record``'s
#: ``observation.observation_id`` and ``.subject_entity_id``,
#: ``measurement.record``'s matching pair, and
#: ``retention.restore``'s ``item_ids``. The one nested arg left
#: deliberately unscanned is ``retention.prune``'s ``criteria``:
#: ``RetentionCriteria`` carries no id field at all (``noise_documents``,
#: ``unconfirmed_mints``, ``lifecycle_states``, ``older_than_days``,
#: ``max_items``), so there is nothing in it a hazard could occupy.
ID_BEARING_ARG_KEYS: dict[Operation, frozenset[str]] = {
    Operation.ENTITY_CREATE: frozenset({"entity_id", "entity_type"}),
    Operation.ENTITY_UPDATE: frozenset({"entity_id", "entity_type"}),
    Operation.ALIAS_UPSERT: frozenset({"entity_id"}),
    Operation.LINK_CREATE: frozenset({"source_id", "target_id", "edge_kind"}),
    Operation.PRECEDENT_PROMOTE: frozenset({"trace_id"}),
    Operation.PRECEDENT_DEMOTE: frozenset({"target_id"}),
    Operation.PRECEDENT_REINSTATE: frozenset({"target_id"}),
    Operation.CLASSIFICATION_SET: frozenset({"doc_id"}),
    Operation.LABEL_ADD: frozenset({"target_id", "label"}),
    Operation.LABEL_REMOVE: frozenset({"target_id", "label"}),
    Operation.FEEDBACK_RECORD: frozenset({"target_id", "pack_id"}),
}


#: The nested counterpart to ``ID_BEARING_ARG_KEYS`` above: an id a handler
#: stores as a primary key, an edge endpoint or an exact-match lookup key,
#: but that arrives nested inside a dict/model sub-object or as an element
#: of a list, never as a bare top-level string. ``(arg, inner_key)`` means
#: ``args[arg]`` is a mapping or a pydantic model and ``inner_key`` is the
#: field to read inside it; ``(arg, None)`` means ``args[arg]`` IS the list
#: of ids to check, element by element. Verified against the handler that
#: stores each one: ``trace.ingest``'s ``trace.trace_id`` is
#: ``TraceStore.append``'s primary key (``TraceIngestHandler``);
#: ``evidence.ingest``'s ``evidence.doc_id`` is ``put_document``'s and the
#: vector store's key (``EvidenceIngestHandler`` -- ``evidence`` is always
#: a dict there, never a model); ``entity.create``'s ``document_ids`` is a
#: list of ids stored verbatim on the new node with no existence check
#: (``EntityCreateHandler``), and ``entity.update``'s replaces it verbatim
#: on the new version (``EntityUpdateHandler``); ``observation.record``'s
#: ``observation.observation_id`` is the node's own id and
#: ``.subject_entity_id`` is an edge endpoint (``ObservationRecordHandler``);
#: ``measurement.record``'s matching pair plays the same two roles
#: (``MeasurementRecordHandler``). Both also carry ``observer_agent_id``,
#: a third role this mapping scans for: an exact-match lookup key, not an
#: edge endpoint or a primary key. Every producer sets it to an opaque
#: agent handle -- see ``trellis_workers.extract.query_pattern_observer``
#: and MCP's ``record_observation`` -- and MCP's ``query_observations``
#: plus the REST ``GET /observations`` and ``GET /measurements`` routes
#: (there is no get-by-id route for either) filter on it with the same
#: exact-match ``properties=`` query every other scanned id uses; a
#: hazard-bearing value stored unchecked would be as permanently
#: unfindable through that filter as an unscanned ``target_id`` is through
#: a lookup (see this module's ``validate()``). ``retention.restore``'s
#: ``item_ids`` is a list of ids used for exact-match restore lookups
#: (``RetentionRestoreHandler``). Same hand-read floor discipline as
#: ``ID_BEARING_ARG_KEYS`` (``tests/unit/mutate/test_commands.py``).
NESTED_ID_ARG_PATHS: dict[Operation, frozenset[tuple[str, str | None]]] = {
    Operation.TRACE_INGEST: frozenset({("trace", "trace_id")}),
    Operation.EVIDENCE_INGEST: frozenset({("evidence", "doc_id")}),
    Operation.ENTITY_CREATE: frozenset({("document_ids", None)}),
    Operation.ENTITY_UPDATE: frozenset({("document_ids", None)}),
    Operation.OBSERVATION_RECORD: frozenset(
        {
            ("observation", "observation_id"),
            ("observation", "subject_entity_id"),
            ("observation", "observer_agent_id"),
        }
    ),
    Operation.MEASUREMENT_RECORD: frozenset(
        {
            ("measurement", "measurement_id"),
            ("measurement", "subject_entity_id"),
            ("measurement", "observer_agent_id"),
        }
    ),
    Operation.RETENTION_RESTORE: frozenset({("item_ids", None)}),
}


class OperationRegistry:
    """Registry of valid operations and their argument schemas."""

    def __init__(self) -> None:
        self._schemas: dict[Operation, set[str]] = {}
        self._register_defaults()

    def _register_defaults(self) -> None:
        """Register default required args for each operation."""
        self._schemas[Operation.TRACE_INGEST] = {"trace"}
        self._schemas[Operation.TRACE_APPEND_STEP] = {"trace_id", "step"}
        self._schemas[Operation.TRACE_RECORD_OUTCOME] = {"trace_id", "outcome"}
        self._schemas[Operation.EVIDENCE_INGEST] = {"evidence"}
        # ``sensitivity`` is validated against the closed vocabulary by the
        # handler, not here: this registry checks presence, not values.
        self._schemas[Operation.CLASSIFICATION_SET] = {"doc_id", "sensitivity"}
        self._schemas[Operation.EVIDENCE_ATTACH] = {
            "evidence_id",
            "target_id",
            "target_type",
        }
        # ``trace_id`` is optional: trace-mined precedents carry it, but a
        # learning-scoring promotion (``submit_learning_promotion``) sources a
        # graph entity, not a trace, and passes ``entity_type`` instead. Both
        # must reach ``get_lessons``, which reads ``PRECEDENT_PROMOTED`` events.
        self._schemas[Operation.PRECEDENT_PROMOTE] = {
            "title",
            "description",
        }
        # ``target_id`` names the precedent entity; ``reason`` is written
        # verbatim into ``Lifecycle.deprecation_reason``, the same
        # discipline ``redaction.apply`` uses for its own ``reason``.
        self._schemas[Operation.PRECEDENT_DEMOTE] = {"target_id", "reason"}
        # The reversal verb: ``target_id`` names the precedent entity,
        # ``reason`` is written verbatim into the audit trail, same shape
        # as ``precedent.demote``'s own schema.
        self._schemas[Operation.PRECEDENT_REINSTATE] = {"target_id", "reason"}
        self._schemas[Operation.ENTITY_CREATE] = {"entity_type", "name"}
        self._schemas[Operation.ENTITY_UPDATE] = {"entity_id"}
        self._schemas[Operation.ENTITY_MERGE] = {"source_id", "target_id"}
        self._schemas[Operation.ALIAS_UPSERT] = {
            "entity_id",
            "source_system",
            "raw_id",
        }
        self._schemas[Operation.LINK_CREATE] = {
            "source_id",
            "target_id",
            "edge_kind",
        }
        self._schemas[Operation.LINK_REMOVE] = {"edge_id"}
        self._schemas[Operation.LABEL_ADD] = {"target_id", "label"}
        self._schemas[Operation.LABEL_REMOVE] = {"target_id", "label"}
        self._schemas[Operation.FEEDBACK_RECORD] = {"target_id", "rating"}
        # Observation/Measurement record operations: the only required arg
        # is the (already-validated) entity payload. Per Item 1 Phase 1 of
        # plan-self-improvement-program.md the handler does the deep
        # schema validation by running Observation.model_validate /
        # Measurement.model_validate on the payload — missing required
        # fields raise loudly, no silent defaults.
        self._schemas[Operation.OBSERVATION_RECORD] = {"observation"}
        self._schemas[Operation.MEASUREMENT_RECORD] = {"measurement"}
        self._schemas[Operation.REDACTION_APPLY] = {"target_id", "reason"}
        # ``criteria`` names the candidate predicate and ``reason`` is
        # written verbatim to the audit log — the same discipline
        # ``redaction.apply`` uses. ``dry_run`` is optional and defaults to
        # True in the handler: destructive-by-default is right for a single
        # named redaction target and wrong for a criteria-driven batch.
        self._schemas[Operation.RETENTION_PRUNE] = {"criteria", "reason"}
        # Restore takes explicit ids rather than a predicate: it is a
        # corrective act on a known set (the ids ride the
        # ``RETENTION_PRUNED`` payload), and a criteria-driven undo would
        # re-derive the same population that was wrong the first time.
        self._schemas[Operation.RETENTION_RESTORE] = {"item_ids", "reason"}
        # Both schedule admin operations push their own field allowlist in
        # the handler (``ScheduleUpdateHandler`` / ``JobRunRequestHandler``):
        # every field but ``target_id`` is optional on an update, and a
        # run-request takes none, so neither has a required-arg set here.
        self._schemas[Operation.SCHEDULE_UPDATE] = set()
        self._schemas[Operation.JOB_RUN_REQUEST] = set()
        # ``value`` is required as a *key*, not a truthy value: ``None``
        # means "remove the override" (SettingsUpdateHandler's own
        # docstring), so the presence check below must not reject a
        # literal null the way an `if not command.args.get("value")` would.
        self._schemas[Operation.SETTINGS_UPDATE] = {"name", "value"}
        self._schemas[Operation.API_KEY_MINT] = {"record"}
        self._schemas[Operation.API_KEY_REVOKE] = {"key_id"}
        self._schemas[Operation.CLASSIFY_RULE_APPLY] = {
            "kind",
            "key",
            "target",
            "source_event_id",
        }
        self._schemas[Operation.CLASSIFY_RULE_REVOKE] = {"kind", "key"}

    def validate(self, command: Command) -> tuple[bool, list[str]]:
        """Validate a command's args against its operation's schema.

        Returns (valid, errors).
        """
        required = self._schemas.get(command.operation)
        if required is None:
            return False, [f"Unknown operation: {command.operation}"]
        missing = required - set(command.args.keys())
        if missing:
            return False, [f"Missing required args: {', '.join(sorted(missing))}"]
        # Lookup keys are refused rather than scrubbed for the audit log: a
        # replaced key would no longer match the raw key a later lookup sends.
        # ``target_id`` is exempted for ``redaction.apply``: its handler
        # never reads ``Command.target_id`` -- it reads ``args["target_id"]``
        # directly (deliberately absent from ID_BEARING_ARG_KEYS, see that
        # mapping's docstring), so this field is descriptive only here. It
        # rides the generic MUTATION_EXECUTED/REJECTED event's ``entity_id``
        # (scrubbed there -- see ``_jsonb_safe_or_original`` in executor.py)
        # and the success ``CommandResult.target_id`` echo, neither of which
        # is a lookup a rewritten value could miss. Refusing it anyway would
        # be the one thing standing between a NUL-minted row and the purge
        # that exists to remove it -- redaction is the main reason to target
        # such a row at all. ``target_type`` and ``idempotency_key`` stay
        # checked for every operation, redaction included: some handlers
        # (``settings.update``, ``classify_rule.apply``/``revoke``) compare
        # ``Command.target_id``/``target_type`` for an exact match, and a
        # rewritten ``idempotency_key`` would never match a later retry's
        # raw one.
        unsafe = [
            f"{field} contains a NUL byte or lone surrogate"
            for field in ("idempotency_key", "target_id", "target_type")
            if not (
                field == "target_id" and command.operation is Operation.REDACTION_APPLY
            )
            and (value := getattr(command, field))
            and contains_nul_or_lone_surrogate(value)
        ]
        # Same discipline, extended to the id-bearing args a handler reads
        # directly rather than through Command's own typed fields -- see
        # ID_BEARING_ARG_KEYS's docstring for which keys and why. Only a
        # str value is checked; a missing or non-string value is left to
        # the schema/handler validation that already covers it.
        unsafe.extend(
            f"args[{key!r}] contains a NUL byte or lone surrogate"
            for key in ID_BEARING_ARG_KEYS.get(command.operation, frozenset())
            if isinstance(value := command.args.get(key), str)
            and contains_nul_or_lone_surrogate(value)
        )
        # Same discipline once more, for an id nested inside a sub-object or
        # a list rather than a bare top-level string -- see
        # NESTED_ID_ARG_PATHS's docstring for which paths and why. A missing
        # arg, a non-str inner value, or a container of the wrong shape is
        # left alone: the schema/handler validation downstream already
        # covers those.
        for arg, inner_key in NESTED_ID_ARG_PATHS.get(command.operation, frozenset()):
            container = command.args.get(arg)
            if inner_key is None:
                if not isinstance(container, (list, tuple)):
                    continue
                unsafe.extend(
                    f"args[{arg!r}] contains a NUL byte or lone surrogate"
                    for element in container
                    if isinstance(element, str)
                    and contains_nul_or_lone_surrogate(element)
                )
                continue
            nested = (
                container.get(inner_key)
                if isinstance(container, Mapping)
                else getattr(container, inner_key, None)
            )
            if isinstance(nested, str) and contains_nul_or_lone_surrogate(nested):
                unsafe.append(
                    f"args[{arg!r}][{inner_key!r}] contains a NUL byte or "
                    "lone surrogate"
                )
        if unsafe:
            return False, unsafe
        # The one value checked here: a feedback rating is a 0.0-1.0 score,
        # and POST /api/v1/commands/batch and MCP ``execute_mutation`` pass
        # caller args straight through. The chained comparison is False for
        # NaN and +/-Infinity, and compares an int too large for a float
        # exactly, where ``math.isfinite`` would raise OverflowError.
        if command.operation is Operation.FEEDBACK_RECORD:
            rating = command.args["rating"]
            if (
                isinstance(rating, bool)
                or not isinstance(rating, int | float)
                or not 0.0 <= rating <= 1.0
            ):
                message = (
                    f"rating must be a finite number in [0.0, 1.0], got {rating!r}"
                )
                return False, [message]
        return True, []

    def get_required_args(self, operation: Operation) -> set[str]:
        """Get required args for an operation."""
        return self._schemas.get(operation, set())
