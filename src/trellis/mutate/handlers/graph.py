"""Graph-plane handlers: entities, aliases, links, observations and measurements."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import structlog

from trellis.core.error_sanitize import describe_validation_error
from trellis.core.precedent_document import (
    SOURCE_EXCERPT_STATUS_KEY,
    is_promoted_precedent_document,
)
from trellis.errors import NotFoundError, StoreError, ValidationError
from trellis.extract.entity_resolution import NAME_ALIAS_SOURCE_SYSTEM
from trellis.mutate.commands import Command
from trellis.mutate.evidence_pointer_keys import node_evidence_ref_refusal
from trellis.mutate.handlers._common import (
    _current_lifecycle_state,
    _rewrite_current_node,
)
from trellis.mutate.retention import CURRENT_STATE
from trellis.retrieve.lifecycle import EVIDENCE_REF_KEY
from trellis.schemas.classification import LIFECYCLE_KEY
from trellis.schemas.measurement import Measurement
from trellis.schemas.observation import Observation
from trellis.schemas.well_known import (
    HAS_MEASUREMENT,
    HAS_OBSERVATION,
    MEASUREMENT,
    OBSERVATION,
    normalize_alias_raw_id,
    normalize_entity_name,
)
from trellis.stores.base.event_log import EventType
from trellis.stores.base.graph import AliasBindResult, AliasBindStatus, GraphStore
from trellis.stores.registry import StoreRegistry

if TYPE_CHECKING:
    from collections.abc import Mapping

    from trellis.schemas.classification import Sensitivity

logger = structlog.get_logger(__name__)

#: What ``entity.create`` with ``if_absent`` answers when the id already names
#: a node and nothing was written. A caller that must not claim a create it did
#: not make tells the two apart by this prefix (``save_knowledge``'s domain
#: mint), so the handler and its readers share one spelling.
ENTITY_LEFT_ALONE_MESSAGE = "Entity left alone, already exists (if_absent)"


def _refuse_node_evidence_ref(
    registry: StoreRegistry,
    properties: Mapping[str, Any],
    *,
    stored_properties: Mapping[str, Any] | None,
    clearance: Sensitivity,
    writer: str,
) -> None:
    """Raise a typed rejection when ``properties`` carries a refused ``evidence_ref``.

    The one place the three graph writers that keep the field (``entity.create``,
    ``entity.update``, ``observation.record``) apply
    :func:`~trellis.mutate.evidence_pointer_keys.node_evidence_ref_refusal`, so
    they cannot disagree. Runs before the write, so a refusal leaves no node
    version, no alias and no event.
    """
    refusal = node_evidence_ref_refusal(
        properties,
        stored_properties=stored_properties,
        document_store=registry.knowledge.document_store,
        clearance=clearance,
        writer=writer,
    )
    if refusal is not None:
        raise ValidationError(
            refusal.message, errors=[refusal.message], code=refusal.code
        )


def _bind_entity_name_alias(
    graph_store: GraphStore,
    *,
    entity_id: str,
    name: object,
) -> AliasBindResult | None:
    """Best-effort derived write inside an already-governed entity command."""
    if not isinstance(name, str):
        return None
    key = normalize_entity_name(name)
    if not key:
        return None

    try:
        twins = graph_store.query(properties={"name": name}, limit=2)
    except Exception:
        logger.exception(
            "entity_resolution_name_alias_twin_check_failed",
            entity_id=entity_id,
        )
        return None
    if any(str(node.get("node_id")) != entity_id for node in twins):
        logger.warning(
            "entity_resolution_name_alias_contested",
            entity_id=entity_id,
        )
        return None

    try:
        result = graph_store.bind_alias_if_absent(
            entity_id,
            NAME_ALIAS_SOURCE_SYSTEM,
            key,
            raw_name=name,
            stale_owner_name_key=key,
        )
    except Exception:
        logger.exception(
            "entity_resolution_alias_bind_failed",
            entity_id=entity_id,
        )
        return None
    if result.status is AliasBindStatus.CONFLICT:
        logger.warning(
            "entity_resolution_name_alias_lost_race",
            entity_id=entity_id,
            existing_entity_id=result.entity_id,
        )
    return result


def _carry_held_evidence_ref(
    properties: dict[str, Any], stored_properties: Mapping[str, Any] | None
) -> None:
    """Copy the stored ``evidence_ref`` into ``properties`` when they leave it out.

    For the writers that replace a node's whole bag (``entity.create`` over an
    existing id, ``observation.record`` and ``measurement.record`` of an
    existing id). A stored value
    that is empty or ``None`` is not held and is not copied.
    """
    if EVIDENCE_REF_KEY in properties or stored_properties is None:
        return
    held = stored_properties.get(EVIDENCE_REF_KEY)
    if held not in (None, ""):
        properties[EVIDENCE_REF_KEY] = held


class EntityCreateHandler:
    """Create entity node via graph store, return node_id.

    Supports optional ``node_role`` and ``generation_spec`` command args to
    create structural or curated nodes. Defaults to a semantic node when
    omitted. The graph store rejects invalid combinations (e.g., curated
    without a generation_spec) via ``validate_node_role_args``.

    ``document_ids`` follows the same omission semantics as
    :class:`EntityUpdateHandler`: an omitted field carries the stored link
    forward, an explicit value replaces it.

    **An existing precedent's id is refused unless it is current.**
    ENTITY_CREATE on any other existing id is a legitimate upsert (it
    opens a new SCD-2 version, e.g. re-extracting an entity), and
    re-approving a learning candidate whose target is already a *current*
    precedent is equally legitimate (``submit_learning_promotion`` writes
    it this way; ``_precedent_reapproval_refusal`` already lets a current
    precedent's re-approval through before this handler ever runs). What
    is refused is an unconditional property replace that would drop a
    *non-current* ``lifecycle`` — a demoted precedent would come back
    ``"current"`` with no reason recorded and no ``PRECEDENT_REINSTATED``
    event, the same silent-reinstate shape :class:`EntityUpdateHandler`'s
    guards close on the update path. A ``entity_type`` other than
    ``"precedent"`` over an existing precedent is refused outright,
    whatever its lifecycle state, mirroring
    :class:`EntityUpdateHandler`'s retype guard: it would otherwise
    retype the node away from precedent in the same call that replaces
    its properties, reaching the same end state as the two-step bypass
    that guard closes.

    **``if_absent`` creates a placeholder and never replaces.** With the
    arg set to exactly ``True`` (``"true"``, ``1`` and the like take the
    ordinary upsert path), an ``entity_id`` that already names a node is
    left alone: nothing is written (no new version, no name-alias bind, no
    ``ENTITY_CREATED`` event) and the existing id is returned with a message
    saying so. The executor still records the command as executed, because
    the call was accepted and did what it was asked to do, which was
    nothing; the message is how that is told apart from a create. An
    absent node is created as usual. The arg needs an ``entity_id`` to
    check and is refused without one. A skip writes nothing, so it cannot
    bypass the precedent refusals above. The check is a read before the
    write, like every guard in this handler, so it does not order two
    writers racing on one id; a single extraction run is one writer. In
    that window a placeholder can replace a real record written
    concurrently, and stays until the next run that holds the record.
    """

    def __init__(
        self, registry: StoreRegistry, *, clearance: Sensitivity = "restricted"
    ) -> None:
        self._registry = registry
        self._clearance = clearance

    def handle(self, command: Command) -> tuple[str | None, str]:
        props: dict[str, Any] = dict(command.args.get("properties", {}))
        props["name"] = command.args["name"]

        # Use caller-supplied entity_id if provided, otherwise auto-generate ULID
        caller_id = command.args.get("entity_id")
        node_role = command.args.get("node_role", "semantic")
        generation_spec = command.args.get("generation_spec")
        # Exactly ``True``: MCP ``execute_mutation`` passes args through
        # unchanged, so a truthy string such as ``"false"`` must not suppress
        # a write.
        if_absent = command.args.get("if_absent") is True
        if if_absent and caller_id is None:
            msg = (
                "entity.create with if_absent needs an entity_id: "
                "create-if-absent has nothing to check without one"
            )
            raise ValidationError(
                msg, errors=[msg], code="entity_create_if_absent_needs_id"
            )

        # One read serves both the precedent guard below and
        # ``_resolve_document_ids``'s stored-link lookup, so a caller_id
        # that already exists costs a single extra read either way.
        existing_node = (
            self._registry.knowledge.graph_store.get_node(caller_id)
            if caller_id is not None
            else None
        )
        if if_absent and existing_node is not None:
            name = command.args["name"]
            return (caller_id, f"{ENTITY_LEFT_ALONE_MESSAGE}: {name}")
        if existing_node is not None and existing_node["node_type"] == "precedent":
            supplied_entity_type = command.args["entity_type"]
            if supplied_entity_type != "precedent":
                msg = (
                    f"entity.create refuses to retype existing precedent "
                    f"{caller_id} to {supplied_entity_type!r}: a later "
                    "entity.update naming properties.lifecycle under that "
                    "non-precedent node_type would then slip past the guard "
                    "that refuses it on a precedent; use entity.update for "
                    "a non-retyping property edit"
                )
                raise ValidationError(
                    msg,
                    errors=[msg],
                    code="entity_create_retypes_existing_precedent",
                )
            if _current_lifecycle_state(existing_node) != CURRENT_STATE:
                msg = (
                    f"entity.create refuses to upsert over existing "
                    f"precedent {caller_id}: an unconditional property "
                    "replace would drop its lifecycle (a demoted precedent "
                    "would come back 'current' with no reason recorded and "
                    "no PRECEDENT_REINSTATED event); use "
                    "precedent.reinstate for that, or entity.update for a "
                    "non-lifecycle property edit"
                )
                raise ValidationError(
                    msg, errors=[msg], code="entity_create_over_existing_precedent"
                )

        # A caller-supplied ``evidence_ref`` names the document another item is
        # superseded by: it must exist, be readable at the caller's clearance,
        # and not replace one the node already holds. Judged after the skip
        # above (a left-alone node writes nothing) and before any write.
        _refuse_node_evidence_ref(
            self._registry,
            props,
            stored_properties=(
                existing_node.get("properties") if existing_node is not None else None
            ),
            clearance=self._clearance,
            writer="entity.create",
        )
        # An upsert replaces the whole bag, so a command that leaves the key out
        # would clear the pointer the rule above says a node keeps. Carry it
        # forward the way ``document_ids`` is below: omission means "leave it
        # alone", and a supplied value was judged above.
        if existing_node is not None:
            _carry_held_evidence_ref(props, existing_node.get("properties"))

        # ``document_ids`` is the first-class graph↔document link (Phase 4 of
        # ADR planes-and-substrates). Threading it here gives the pointer a
        # governed write path — the pointer-not-prose invariant depends on a
        # created entity being able to carry an ``evidence_ref`` document
        # pointer without smuggling it through metadata.
        #
        # ENTITY_CREATE on an existing id is an upsert (it opens a new SCD-2
        # version), so an *omitted* ``document_ids`` has to carry the stored
        # link forward the way EntityUpdateHandler does. Passing ``None``
        # straight through would write NULL and silently destroy a link some
        # other writer established — e.g. re-extracting an entity that
        # ``save_knowledge`` had linked to its document. Omission means
        # "leave it alone"; an explicit value still replaces.
        document_ids = self._resolve_document_ids(command, existing_node)
        node_id = self._registry.knowledge.graph_store.upsert_node(
            node_id=caller_id,
            node_type=command.args["entity_type"],
            properties=props,
            node_role=node_role,
            generation_spec=generation_spec,
            document_ids=document_ids,
        )
        _bind_entity_name_alias(
            self._registry.knowledge.graph_store,
            entity_id=node_id,
            name=command.args["name"],
        )

        self._registry.operational.event_log.emit(
            EventType.ENTITY_CREATED,
            source="mutation_executor",
            entity_id=node_id,
            entity_type=command.args["entity_type"],
            payload={
                "name": command.args["name"],
                "node_role": node_role,
                "document_ids": document_ids or [],
            },
        )
        return node_id, f"Entity created: {command.args['name']}"

    def _resolve_document_ids(
        self, command: Command, existing_node: dict[str, Any] | None
    ) -> list[str] | None:
        """Command's ``document_ids``, else the stored link, else ``None``.

        ``existing_node`` is whatever ``handle`` already read for the
        precedent guard (``None`` when ``entity_id`` was omitted or named
        no existing node) — reused here rather than re-read, so a caller_id
        that already exists still costs one store read, not two.
        """
        if "document_ids" in command.args:
            supplied = command.args["document_ids"]
            return cast("list[str] | None", supplied)
        if existing_node is None:
            return None
        # ``get_node`` returns a (possibly empty) list; normalise empty to
        # ``None`` so ``validate_document_ids`` sees a clean value.
        return cast("list[str] | None", existing_node.get("document_ids") or None)


class EntityUpdateHandler:
    """Update an existing entity node, emitting ``ENTITY_UPDATED``.

    Wires :data:`Operation.ENTITY_UPDATE` into the governed pipeline. The
    enum verb shipped with no handler, so the executor rejected every
    ``entity.update`` command with "No handler registered for:
    entity.update" — this closes that gap.

    **SCD-2 discipline.** The update is never an in-place edit. The new
    version is written by a compare-and-set on the version read
    (``_rewrite_current_node``), which closes the old version (``valid_to``
    is set) and inserts a fresh version row; ``get_node_history`` is the
    audit trail. A node purged between the read and the write is refused
    like a missing one, not re-created; a version replaced by another
    writer in between is re-read and the update re-applied. ``node_role``
    and ``generation_spec`` are immutable across versions (the graph store
    enforces role immutability via ``check_node_role_immutable``), so they
    are carried forward from the existing version — the same discipline
    :class:`LabelAddHandler` uses.

    **Partial update.** ``properties`` is *merged* into the existing bag
    (not replaced) so a caller can attach a single field — e.g. an
    ``evidence_ref`` pointer — without resending the whole node. Likewise
    ``document_ids`` is carried forward when the caller omits it: absent
    means "leave the graph↔document link untouched", present means "replace
    it". This keeps a version bump from silently dropping the
    pointer-not-prose link.

    Idempotency: a same-payload re-submission still creates a new version
    row (SCD-2 has no same-value short-circuit at this layer); use
    ``Command.idempotency_key`` when at-most-once semantics are required.

    **Precedents are the one exception to the property merge.** A write
    naming ``properties.lifecycle`` on a node whose current ``node_type``
    is ``"precedent"``, *or* whose ``entity_type`` arg names ``"precedent"``
    in the same call, is refused rather than merged: this used to be the
    only way to undo a ``precedent.demote`` (silently, with no reason
    recorded and no vector re-sync), which is exactly the gap
    :class:`PrecedentReinstateHandler` closes. Every other node's
    ``lifecycle`` key is an ordinary property this handler merges like any
    other — the save-surfaces' supersession stamp (``mcp.supersession``)
    still writes it this way.

    **A precedent's ``node_type`` may not be changed away from
    ``"precedent"`` at all**, lifecycle write or not: a two-step bypass
    (update 1: ``entity_type="concept"`` with no lifecycle payload, so the
    guard above never fires because nothing names ``lifecycle``; update 2:
    ``entity_type="precedent"`` plus ``properties.lifecycle.state="current"``,
    read against the *now-"concept"* stored node) would otherwise reinstate
    a demoted precedent with no ``PRECEDENT_REINSTATED`` event and no
    reason recorded. Refusing step 1 outright closes it before step 2 is
    ever reached; the ``entity_type == "precedent"`` keying above is the
    second, independent guard for the same end state.
    """

    def __init__(
        self, registry: StoreRegistry, *, clearance: Sensitivity = "restricted"
    ) -> None:
        self._registry = registry
        self._clearance = clearance

    def handle(self, command: Command) -> tuple[str | None, str]:
        entity_id = command.args["entity_id"]
        store = self._registry.knowledge.graph_store
        raw_properties = command.args.get("properties") or {}
        # ``props.update(...)`` below accepts a dict OR an iterable of
        # ``(key, value)`` pairs, and callers use both (MCP
        # ``execute_mutation`` and REST ``/commands/batch`` both carry
        # ``args`` as loosely-typed JSON, so a list-of-pairs payload reaches
        # here unchanged). Normalising to a dict *here*, before either
        # precedent guard reads it, means ``touches_lifecycle`` below sees
        # the same key regardless of which shape the caller sent --
        # otherwise a pairs-form ``[["lifecycle", {...}]]`` skipped the
        # ``isinstance(..., dict)`` check entirely and reinstated a demoted
        # precedent with no ``PRECEDENT_REINSTATED`` event.
        supplied_properties: dict[str, Any] = dict(raw_properties)
        supplied_entity_type = command.args.get("entity_type")
        touches_lifecycle = LIFECYCLE_KEY in supplied_properties
        touches_excerpt_status = SOURCE_EXCERPT_STATUS_KEY in supplied_properties

        # Read the existing node only when one of the two precedent guards
        # below could actually need it, so the common case -- a property
        # merge that names neither ``entity_type`` nor ``lifecycle`` --
        # stays the single read ``_rewrite_current_node`` does internally.
        existing_node = (
            store.get_node(entity_id)
            if (supplied_entity_type or touches_lifecycle or touches_excerpt_status)
            else None
        )
        is_existing_precedent = (
            existing_node is not None and existing_node["node_type"] == "precedent"
        )

        if (
            is_existing_precedent
            and supplied_entity_type
            and supplied_entity_type != "precedent"
        ):
            msg = (
                f"entity.update refuses to change {entity_id}'s entity_type "
                f"away from 'precedent' (to {supplied_entity_type!r}): a "
                "later update could then write properties.lifecycle under "
                "that non-precedent node_type and slip past the guard that "
                "refuses it on a precedent"
            )
            raise ValidationError(
                msg, errors=[msg], code="precedent_entity_type_change_refused"
            )

        if touches_lifecycle and (
            is_existing_precedent or supplied_entity_type == "precedent"
        ):
            msg = (
                f"entity.update refuses a write to properties.{LIFECYCLE_KEY} "
                f"on precedent {entity_id}: use precedent.demote or "
                "precedent.reinstate instead, which keep the audit trail "
                "and the vector row in sync"
            )
            raise ValidationError(
                msg, errors=[msg], code="precedent_lifecycle_via_entity_update"
            )

        if touches_excerpt_status and (
            is_existing_precedent
            or supplied_entity_type == "precedent"
            or is_promoted_precedent_document(entity_id)
        ):
            # The stamp is the promotion's own statement about the text it
            # wrote; serving reads it to exempt a precedent from its source's
            # label (#1080). A caller-set value would forge that exemption.
            msg = (
                f"entity.update refuses a write to properties."
                f"{SOURCE_EXCERPT_STATUS_KEY} on precedent {entity_id}: the "
                "promotion writes it, and a serve-time check trusts it"
            )
            raise ValidationError(
                msg, errors=[msg], code="precedent_excerpt_status_via_entity_update"
            )

        def update(existing: dict[str, Any]) -> dict[str, Any]:
            # Judged against the version being replaced, so a retry after a
            # concurrent write compares with the row it would overwrite.
            _refuse_node_evidence_ref(
                self._registry,
                supplied_properties,
                stored_properties=existing["properties"],
                clearance=self._clearance,
                writer="entity.update",
            )
            # Partial update: merge caller-supplied properties onto the
            # existing bag, then let an explicit ``name`` win.
            props: dict[str, Any] = dict(existing["properties"])
            props.update(supplied_properties)
            if "name" in command.args:
                props["name"] = command.args["name"]
            changes: dict[str, Any] = {"properties": props}
            # ``node_type`` is mutable across versions but carries forward
            # when the caller omits ``entity_type``.
            if command.args.get("entity_type"):
                changes["node_type"] = command.args["entity_type"]
            # The document link carries forward unless the caller supplies a
            # replacement.
            if "document_ids" in command.args:
                changes["document_ids"] = command.args["document_ids"]
            return changes

        written = _rewrite_current_node(store, entity_id, update)
        if written is None:
            # Input to fix, not a store failure: refused like a label on a
            # missing node (REJECTED, ``code`` the audit reason).
            msg = f"Node not found: {entity_id}"
            raise ValidationError(msg, errors=[msg], code="target_not_found")

        # Bound and emitted only after the write succeeded, so a refused
        # update leaves neither an alias nor an event.
        final_name = written["properties"].get("name")
        name_was_supplied = "name" in command.args or "name" in supplied_properties
        if name_was_supplied:
            _bind_entity_name_alias(
                store,
                entity_id=entity_id,
                name=final_name,
            )

        self._registry.operational.event_log.emit(
            EventType.ENTITY_UPDATED,
            source="mutation_executor",
            entity_id=entity_id,
            entity_type=written["node_type"],
            payload={
                "name": final_name,
                "node_role": written["node_role"],
                "document_ids": written["document_ids"] or [],
            },
        )
        return entity_id, f"Entity updated: {entity_id}"


class AliasUpsertHandler:
    """Bind or explicitly rebind an alias through the governed pipeline.

    **A Unity Catalog ``raw_id`` is bound under its normal form.** Under
    ``source_system == "unity_catalog"`` the id is passed through
    :func:`~trellis.schemas.well_known.normalize_alias_raw_id` first, whoever
    sent it, because ``save_knowledge`` looks that system's ids up by the
    normal form only: ``Main.Sales.Orders`` bound verbatim would be a row no
    lookup finds. A normalised id is unchanged by a second pass, so a draft
    that already carries one binds exactly as before. Every other system's id
    is bound verbatim. The command's own args are not rewritten.
    """

    def __init__(self, registry: StoreRegistry) -> None:
        self._registry = registry

    def handle(self, command: Command) -> tuple[str | None, str]:
        store = self._registry.knowledge.graph_store
        # Opt-in guard for callers that bind an alias in the same batch that
        # creates its entity (extraction): a create the policy gate refused
        # must not leave an alias pointing at a node that does not exist.
        # Absent or false keeps the handler's historical behaviour.
        if command.args.get("require_entity") and (
            store.get_node(command.args["entity_id"]) is None
        ):
            msg = f"Cannot bind alias: entity {command.args['entity_id']!r} not found"
            raise ValidationError(msg)
        kwargs = {
            "raw_name": command.args.get("raw_name"),
            "match_confidence": command.args.get("match_confidence", 1.0),
            "is_primary": command.args.get("is_primary", False),
        }
        try:
            raw_id = normalize_alias_raw_id(
                command.args["source_system"], command.args["raw_id"]
            )
            # Exact, as in EntityCreateHandler: MCP passes args through, and a
            # truthy string such as "false" must not suppress a rebind.
            if command.args.get("if_absent") is True:
                result = store.bind_alias_if_absent(
                    command.args["entity_id"],
                    command.args["source_system"],
                    raw_id,
                    stale_owner_name_key=command.args.get("stale_owner_name_key"),
                    **kwargs,
                )
                return result.alias_id, f"Alias bind outcome: {result.status.value}"

            alias_id = store.upsert_alias(
                command.args["entity_id"],
                command.args["source_system"],
                raw_id,
                **kwargs,
            )
        except Exception as exc:
            msg = "Alias store operation failed"
            raise StoreError(msg, store="graph") from exc
        return alias_id, "Alias upserted"


class LinkCreateHandler:
    """Validate both endpoints exist, then create edge via graph store.

    Pre-flight FK validation runs at the start of :meth:`handle` (before any
    side effect) so orphan edges can't be created in the first place. The
    legacy CLI ``graph-health`` command surfaces orphans post-hoc as a
    safety net; this handler closes the door at ingest time.

    The check resolves each endpoint via :meth:`_resolve_node` (direct
    ``get_node`` lookup, then property-based fallback on ``entity_id``).
    On miss, raises :class:`trellis.errors.ValidationError` with a message
    that names which side (source / target / both) failed and which IDs
    were attempted — the executor turns that into a ``MUTATION_REJECTED``
    event and a ``CommandStatus.FAILED`` result, so ``LINK_CREATED`` is
    never emitted for a dangling edge.

    Escape hatch: pass ``allow_dangling=True`` in ``command.args`` to skip
    FK validation. This is for bootstrap / edge-before-node ingest paths
    (e.g. extractors that emit edges in dependency order before their
    referenced nodes exist). Default is ``False`` — strict.
    """

    def __init__(self, registry: StoreRegistry) -> None:
        self._registry = registry

    def _resolve_node(self, node_id: str) -> str | None:
        """Resolve a node ID, falling back to property-based lookup.

        Tries:
          1. Direct ``get_node(node_id)`` (exact match on node_id column)
          2. Property lookup: ``properties->>'entity_id' = node_id``
        """
        store = self._registry.knowledge.graph_store
        if store.get_node(node_id) is not None:
            return node_id
        # Fallback: search by entity_id stored in properties
        results = store.query(properties={"entity_id": node_id}, limit=1)
        if results:
            node_id_val: str | None = results[0]["node_id"]
            return node_id_val
        return None

    def _resolve_endpoints(self, source_id: str, target_id: str) -> tuple[str, str]:
        """Resolve both edge endpoints or raise :class:`ValidationError`.

        Centralises the FK-validation block so the happy-path of
        :meth:`handle` doesn't carry the per-side error wiring. Both
        endpoints are checked even on a single miss so callers see all
        root causes in one round trip.
        """
        resolved_source = self._resolve_node(source_id)
        resolved_target = self._resolve_node(target_id)
        missing: list[str] = []
        if resolved_source is None:
            missing.append(
                f"source_id={source_id!r} does not reference an existing entity"
            )
        if resolved_target is None:
            missing.append(
                f"target_id={target_id!r} does not reference an existing entity"
            )
        if missing:
            msg = f"LINK_CREATE FK check failed: {'; '.join(missing)}"
            # ``code`` becomes the ``reason`` field on the MUTATION_REJECTED
            # event the executor emits — see Variant A' in
            # docs/design/adr-extraction-validation.md.
            raise ValidationError(msg, errors=missing, code="orphan_edge")
        # Both checks passed → both resolved values are non-None.
        return resolved_source, resolved_target  # type: ignore[return-value]

    def handle(self, command: Command) -> tuple[str | None, str]:
        source_id = command.args["source_id"]
        target_id = command.args["target_id"]
        edge_kind = command.args["edge_kind"]
        allow_dangling = bool(command.args.get("allow_dangling", False))
        store = self._registry.knowledge.graph_store

        if not allow_dangling:
            source_id, target_id = self._resolve_endpoints(source_id, target_id)

        # ``extractor_tier`` is forwarded only when the caller names one, so
        # a command without it reaches the store exactly as it always did.
        provenance: dict[str, Any] = {}
        if command.args.get("extractor_tier") is not None:
            provenance["extractor_tier"] = command.args["extractor_tier"]
        edge_id = store.upsert_edge(
            source_id=source_id,
            target_id=target_id,
            edge_type=edge_kind,
            properties=command.args.get("properties"),
            **provenance,
        )

        self._registry.operational.event_log.emit(
            EventType.LINK_CREATED,
            source="mutation_executor",
            entity_id=edge_id,
            payload={
                "source_id": source_id,
                "target_id": target_id,
                "edge_kind": edge_kind,
            },
        )
        return edge_id, f"Link created: {source_id} --[{edge_kind}]--> {target_id}"


class ObservationRecordHandler:
    """Persist an Observation as a graph node + ``hasObservation`` edge.

    Wires :data:`Operation.OBSERVATION_RECORD` into the governed mutation
    pipeline so empirical-observation ingestion follows the same audit /
    idempotency / policy contract as every other mutation. ``args``
    expects an ``observation`` key holding either an :class:`Observation`
    instance or a dict; dicts are routed through
    ``Observation.model_validate`` so any missing required field raises
    a :class:`pydantic.ValidationError` *inside* the handler — surfaced
    as ``CommandStatus.FAILED`` to the caller, never silently defaulted.

    Idempotency semantics: re-recording with the same id REPLACES the
    existing observation and re-emits the ``OBSERVATION_RECORDED`` event.
    This diverges from :class:`TraceIngestHandler`'s
    short-circuit-on-repeat behavior because Observations are mutable
    signals over time — an agent may revise its confidence as new
    evidence arrives. Use a new ``observation_id`` to record a distinct
    signal. See ``docs/design/adr-observation-entity-type.md`` §2.1;
    ``hasObservation`` is the canonical edge from the subject entity to
    the observation node.
    """

    def __init__(
        self, registry: StoreRegistry, *, clearance: Sensitivity = "restricted"
    ) -> None:
        self._registry = registry
        self._clearance = clearance

    def handle(self, command: Command) -> tuple[str | None, str]:
        raw = command.args["observation"]
        try:
            obs = (
                raw if isinstance(raw, Observation) else Observation.model_validate(raw)
            )
        except Exception as exc:
            # Loud-on-missing-required-field discipline. The executor
            # turns ValidationError into a structured rejection event.
            # describe_validation_error, not str(exc): pydantic's own text
            # embeds input_value=<the caller's field value>, and this
            # message becomes the immutable mutation.rejected audit event.
            msg = f"Observation validation failed: {describe_validation_error(exc)}"
            raise ValidationError(msg, code="observation_validation") from exc

        store = self._registry.knowledge.graph_store

        # Idempotent upsert — repeat submissions of the same observation_id
        # collapse onto the existing node. The graph store's SCD-2 layer
        # treats a same-payload upsert as a no-op version close + reopen.
        props: dict[str, Any] = {
            "observation_id": obs.observation_id,
            "subject_entity_id": obs.subject_entity_id,
            "subject_entity_type": obs.subject_entity_type,
            "observer_agent_id": obs.observer_agent_id,
            "content": obs.content,
            "confidence": obs.confidence,
            "observed_at": obs.observed_at.isoformat(),
        }
        # Re-recording an id replaces the node, so the stored pointer is the one
        # a different value would overwrite, and one the new record leaves out
        # would be cleared; it is carried forward instead.
        existing_observation = store.get_node(obs.observation_id)
        stored_properties = (
            existing_observation.get("properties")
            if existing_observation is not None
            else None
        )
        if obs.evidence_ref is not None:
            _refuse_node_evidence_ref(
                self._registry,
                {"evidence_ref": obs.evidence_ref},
                stored_properties=stored_properties,
                clearance=self._clearance,
                writer="observation.record",
            )
            props["evidence_ref"] = obs.evidence_ref
        else:
            _carry_held_evidence_ref(props, stored_properties)
        if obs.metadata is not None:
            props["metadata"] = obs.metadata

        node_id = store.upsert_node(
            node_id=obs.observation_id,
            node_type=OBSERVATION,
            properties=props,
        )

        # Best-effort hasObservation edge from the subject entity to the
        # new observation node. The subject may not be a graph node in
        # all deployments (e.g., raw trace IDs) — when the FK fails we
        # still keep the observation row, but the edge is skipped. This
        # matches the open-string entity-type rule (CLAUDE.md
        # type-extensibility).
        #
        # Narrow the catch to ``StoreError`` (which ``NotFoundError``
        # subclasses): unexpected exceptions (DB driver bugs, schema
        # errors) are escalated rather than silently swallowed — see
        # PR #122 (C2 Phase 5) for the silent-fallback discipline.
        try:
            store.upsert_edge(
                source_id=obs.subject_entity_id,
                target_id=node_id,
                edge_type=HAS_OBSERVATION,
            )
        except (NotFoundError, StoreError) as exc:
            logger.info(
                "observation_edge_skipped",
                subject_entity_id=obs.subject_entity_id,
                observation_id=node_id,
                reason=str(exc),
            )

        self._registry.operational.event_log.emit(
            EventType.OBSERVATION_RECORDED,
            source="mutation_executor",
            entity_id=node_id,
            entity_type=OBSERVATION,
            payload={
                "observation_id": node_id,
                "subject_entity_id": obs.subject_entity_id,
                "subject_entity_type": obs.subject_entity_type,
                "observer_agent_id": obs.observer_agent_id,
                "confidence": obs.confidence,
            },
        )
        return node_id, f"Observation recorded: {node_id}"


class MeasurementRecordHandler:
    """Persist a Measurement as an append-only graph node.

    Sibling of :class:`ObservationRecordHandler` keyed on
    ``measurement_id``. See ``docs/design/adr-observation-entity-type.md``
    §2.1 — Measurement rows are *append-only by convention* so the SCD-2
    cost of high-frequency metric streams stays bounded.

    Idempotency semantics: re-recording with the same id REPLACES the
    existing measurement and re-emits the ``MEASUREMENT_RECORDED`` event.
    This diverges from :class:`TraceIngestHandler`'s
    short-circuit-on-repeat behavior because Measurements are mutable
    signals over time — a metric may be re-measured with a corrected
    value. Use a new ``measurement_id`` to record a distinct signal.
    """

    def __init__(self, registry: StoreRegistry) -> None:
        self._registry = registry

    def handle(self, command: Command) -> tuple[str | None, str]:
        raw = command.args["measurement"]
        try:
            meas = (
                raw if isinstance(raw, Measurement) else Measurement.model_validate(raw)
            )
        except Exception as exc:
            # describe_validation_error, not str(exc): see the matching
            # comment in ObservationRecordHandler.handle above.
            msg = f"Measurement validation failed: {describe_validation_error(exc)}"
            raise ValidationError(msg, code="measurement_validation") from exc

        store = self._registry.knowledge.graph_store

        props: dict[str, Any] = {
            "measurement_id": meas.measurement_id,
            "subject_entity_id": meas.subject_entity_id,
            "subject_entity_type": meas.subject_entity_type,
            "metric_name": meas.metric_name,
            "metric_value": meas.metric_value,
            "observer_agent_id": meas.observer_agent_id,
            "measured_at": meas.measured_at.isoformat(),
        }
        if meas.unit is not None:
            props["unit"] = meas.unit
        if meas.metadata is not None:
            props["metadata"] = meas.metadata
        # A Measurement has no evidence_ref of its own, so re-recording an id that
        # holds one would clear it; it is carried forward instead.
        existing_measurement = store.get_node(meas.measurement_id)
        _carry_held_evidence_ref(
            props,
            existing_measurement.get("properties")
            if existing_measurement is not None
            else None,
        )

        node_id = store.upsert_node(
            node_id=meas.measurement_id,
            node_type=MEASUREMENT,
            properties=props,
        )

        # Same narrow-catch discipline as ObservationRecordHandler —
        # silent-fallback hygiene per PR #122 (C2 Phase 5). Measurement
        # gets its own ``hasMeasurement`` edge kind so consumers can
        # route on edge kind alone without inspecting the target node's
        # type (adr-observation-entity-type.md §2.2).
        try:
            store.upsert_edge(
                source_id=meas.subject_entity_id,
                target_id=node_id,
                edge_type=HAS_MEASUREMENT,
            )
        except (NotFoundError, StoreError) as exc:
            logger.info(
                "measurement_edge_skipped",
                subject_entity_id=meas.subject_entity_id,
                measurement_id=node_id,
                reason=str(exc),
            )

        self._registry.operational.event_log.emit(
            EventType.MEASUREMENT_RECORDED,
            source="mutation_executor",
            entity_id=node_id,
            entity_type=MEASUREMENT,
            payload={
                "measurement_id": node_id,
                "subject_entity_id": meas.subject_entity_id,
                "subject_entity_type": meas.subject_entity_type,
                "metric_name": meas.metric_name,
                "metric_value": meas.metric_value,
                "observer_agent_id": meas.observer_agent_id,
            },
        )
        return node_id, f"Measurement recorded: {node_id}"
