"""The ``classify_rule.apply`` and ``classify_rule.revoke`` handlers."""

from __future__ import annotations

import json
import math
import unicodedata
from datetime import UTC, datetime
from typing import Any

from trellis.core.text_safety import (
    contains_nul_or_lone_surrogate,
    replace_nul_and_lone_surrogates,
)
from trellis.errors import (
    DegradedStoreWriteError,
    StaleStoreWriteError,
    ValidationError,
)
from trellis.learning.tag_evolution import MIN_TAG_KEYWORD_LENGTH
from trellis.mutate.commands import Command
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry


def _validate_classify_rule_metrics(command_args: dict[str, Any]) -> None:
    """Refuse a ``classify_rule.apply`` whose carried metrics are unusable.

    ``support``/``precision``/``lift`` are optional, caller-supplied,
    copied verbatim onto the ``CLASSIFY_RULE_APPLIED`` audit event -- the
    REST DTO (:class:`~trellis_wire.dtos.ClassifyRuleApplyRequest`, which
    sets ``allow_inf_nan=False``) narrows what reaches here for that one
    surface, but it is not a governed boundary: the CLI has no finiteness
    check of its own, and a direct ``MutationExecutor.execute`` from
    Python skips the DTO too. This is the one check every caller passes
    through -- MCP cannot reach ``classify_rule.apply`` at all, since its
    handlers register only on ``build_classify_rules_executor``, which
    ``build_curate_executor`` (the executor MCP's ``execute_mutation``
    uses) does not.

    Without it, a ``NaN``/``Infinity`` metric wrote the overlay row (
    :meth:`~trellis.stores.classify_overrides_store.ClassifyOverridesStore.put`
    has no opinion on these fields) and only then failed -- when the
    *audit event* was serialized, not the overlay write -- leaving an
    applied rule with no ``CLASSIFY_RULE_APPLIED`` event to show for it.
    Called before any write, so a rejected call leaves no trace at all.

    Ranges mirror what :mod:`trellis.learning.tag_evolution` computes
    each metric from: ``support`` is a document count (``>= 0``),
    ``precision`` is ``support / keyword_documents`` (``[0, 1]``), and
    ``lift`` is ``precision / base_rate`` (``>= 0``, unbounded above).

    Raises:
        ValidationError: a metric is present but not a plain, finite
            number in its valid range (``code="invalid_metric"``).
    """
    support = command_args.get("support")
    if support is not None and (
        isinstance(support, bool) or not isinstance(support, int) or support < 0
    ):
        msg = (
            "classify_rule.apply: 'support' must be a non-negative "
            f"integer or null, got {support!r}"
        )
        raise ValidationError(msg, errors=[msg], code="invalid_metric")

    for name, low, high in (("precision", 0.0, 1.0), ("lift", 0.0, None)):
        value = command_args.get(name)
        if value is None:
            continue
        # bool is an int subclass and NaN/inf are valid `float` instances,
        # so both need an explicit check beyond isinstance(value, float).
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not math.isfinite(value)
        ):
            msg = (
                f"classify_rule.apply: {name!r} must be a finite number or "
                f"null, got {value!r}"
            )
            raise ValidationError(msg, errors=[msg], code="invalid_metric")
        if value < low or (high is not None and value > high):
            bound = f"[{low}, {high}]" if high is not None else f">= {low}"
            msg = f"classify_rule.apply: {name!r} must be {bound}, got {value!r}"
            raise ValidationError(msg, errors=[msg], code="invalid_metric")


_MAX_CLASSIFY_RULE_IDENTIFIER_LENGTH = 200


def _validate_classify_rule_identifier(value: str, *, field: str, code: str) -> None:
    """Refuse a classify-rule identifier that is not already normal.

    Shared by :class:`ClassifyRuleApplyHandler` (``key``, ``target``, and
    ``source_event_id``) and :class:`ClassifyRuleRevokeHandler` (``key``),
    called before either touches the overlay store, so a refusal here
    leaves no row and no event -- the same posture as
    ``_validate_classify_rule_metrics`` above.

    Refuses rather than normalising:
    :class:`~trellis.schemas.classify_overrides.ClassifyOverrideRow` strips
    whitespace on assignment (``str_strip_whitespace=True``), so a key or
    target with leading/trailing whitespace would otherwise be stored
    stripped while ``row_id``/``entity_id``/the audit payload -- all built
    from the caller's original string -- stayed un-stripped, splitting one
    logical identifier across two different strings. A
    control character (:func:`unicodedata.category` category ``"Cc"``,
    which includes NUL and ESC) or a lone UTF-16 surrogate (category
    ``"Cs"``, checked with
    :func:`trellis.core.text_safety.contains_nul_or_lone_surrogate` rather
    than re-deriving the same surrogate-range test) can break a
    downstream consumer outright: a NUL embedded in a Postgres ``jsonb``
    audit payload fails to write at all, and a lone surrogate cannot be
    encoded to UTF-8 at all -- pydantic-core refuses one even earlier,
    raising its own ``pydantic_core.ValidationError`` the moment
    ``ClassifyOverrideRow`` is built, which this handler's callers do not
    catch, so an unchecked surrogate reaches the caller as a handler
    panic (``FAILED``) rather than a clean refusal. A key, target, or
    source_event_id over the length limit risks the same failure on some
    backends' index-row limits, writing the overlay row and then failing
    only when the audit event is emitted.

    Args:
        value: the string to check.
        field: which field this is, used only in the error message.
        code: the :class:`~trellis.errors.ValidationError` code to raise.
            Each caller passes its own (``"invalid_key"``,
            ``"invalid_target"``, ``"invalid_source_event_id"``) rather
            than this function deriving one from ``field``, since the
            type-check each caller already raises for the same field uses
            that same code and the two must not disagree.

    Raises:
        ValidationError: ``value`` has leading/trailing whitespace,
            contains a control character or an unpaired UTF-16 surrogate,
            or exceeds ``_MAX_CLASSIFY_RULE_IDENTIFIER_LENGTH`` characters
            (raised with the caller-supplied ``code``).
    """
    if value != value.strip():
        msg = (
            f"classify_rule: {field} {value!r} has leading or trailing "
            "whitespace -- refused rather than normalised, since the "
            "stored row strips whitespace but the audit trail would not"
        )
        raise ValidationError(msg, errors=[msg], code=code)
    if len(value) > _MAX_CLASSIFY_RULE_IDENTIFIER_LENGTH:
        msg = (
            f"classify_rule: {field} is {len(value)} characters, over the "
            f"{_MAX_CLASSIFY_RULE_IDENTIFIER_LENGTH}-character limit"
        )
        raise ValidationError(msg, errors=[msg], code=code)
    if contains_nul_or_lone_surrogate(value):
        msg = (
            f"classify_rule: {field} {value!r} contains a NUL byte or an "
            "unpaired UTF-16 surrogate"
        )
        raise ValidationError(msg, errors=[msg], code=code)
    for ch in value:
        if unicodedata.category(ch) == "Cc":
            msg = (
                f"classify_rule: {field} {value!r} contains a control "
                f"character ({unicodedata.category(ch)!r})"
            )
            raise ValidationError(msg, errors=[msg], code=code)


def _find_unsafe_jsonb_string(
    obj: Any, path: str = "payload"
) -> tuple[str, str] | None:
    """Walk ``obj`` for the first string a jsonb column cannot store.

    Recurses into ``dict``/``list``/``tuple`` nesting at any depth,
    checking dict *keys* as well as dict and sequence values -- a str
    key is exactly as storable (or not) as a str value, so a key is
    checked the same way rather than assumed safe for being a key; every
    other leaf type (``int``, ``float``, ``bool``, ``None``, a datetime
    already rendered to ``str`` by the caller) is skipped, since a NUL
    or lone surrogate can only ever occur inside a Python ``str``.

    Returns:
        ``(path, value)`` for the first unsafe string found, walking
        each dict item's key before its value, in insertion order, and
        list/tuple items in index order, or ``None`` if no string in the
        structure is unsafe.
    """
    if isinstance(obj, str):
        return (path, obj) if contains_nul_or_lone_surrogate(obj) else None
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(key, str) and contains_nul_or_lone_surrogate(key):
                return (f"{path} key", key)
            found = _find_unsafe_jsonb_string(value, f"{path}[{key!r}]")
            if found is not None:
                return found
        return None
    if isinstance(obj, (list, tuple)):
        for index, value in enumerate(obj):
            found = _find_unsafe_jsonb_string(value, f"{path}[{index}]")
            if found is not None:
                return found
    return None


def _validate_jsonb_safe_payload(payload: dict[str, Any], *, operation: str) -> None:
    """Refuse an audit ``payload`` a Postgres ``jsonb`` column cannot store.

    ``_validate_classify_rule_identifier`` already refuses a NUL or
    control character in ``key``/``target``/``source_event_id`` directly,
    but the payload also carries fields that validator never sees --
    caller identity (``command.metadata``'s ``key_id``/``key_name``) and
    whatever a future field adds -- so this is the backstop that checks
    every string in the payload, dict keys included, at any nesting
    depth, rather than one named field at a time. (A prior row's own
    ``applied_by``/``source_event_id``, which may predate that validator
    or have been written by hand-editing the overlay file, is scrubbed
    by :func:`_scrub_stranded_legacy_field` before it ever reaches this
    probe -- issue #869 -- so this backstop no longer refuses on a value
    this call cannot fix by resubmitting different input.) Called before
    the write it would otherwise follow (``store.put`` in Apply,
    ``store.remove`` in Revoke), so a refusal here leaves the prior state
    -- row present or absent -- exactly as it was, with no row written
    and no event emitted.

    Raises:
        ValidationError: some string value or dict key, at any nesting
            depth, contains a NUL byte or an unpaired UTF-16 surrogate
            (``code="unsafe_audit_payload"``).
    """
    found = _find_unsafe_jsonb_string(payload)
    if found is not None:
        path, value = found
        msg = (
            f"classify_rule.{operation}: audit payload {path} contains a "
            "NUL byte or an unpaired surrogate that a jsonb column cannot "
            f"store: {value!r}"
        )
        raise ValidationError(msg, errors=[msg], code="unsafe_audit_payload")


def _scrub_stranded_legacy_field(value: str) -> str:
    """Replace a NUL byte or lone surrogate in a value read off an existing
    overlay row, rather than refusing the payload it rides in on.

    ``existing.applied_by``/``existing.source_event_id`` may predate the
    identifier validator or come from a hand-edited overlay file. The
    caller cannot fix such a value by resubmitting, so refusing the audit
    payload over it would leave the row impossible to revoke or re-apply.
    """
    return replace_nul_and_lone_surrogates(value)


_CLASSIFY_RULE_IDENTITY_FIELD_CODES = (
    ("requested_by_claim", "invalid_requested_by_claim"),
    ("key_id", "invalid_key_id"),
    ("key_name", "invalid_key_name"),
)


def _validate_classify_rule_identity_fields(
    *, requested_by_claim: Any, key_id: Any, key_name: Any, operation: str
) -> None:
    """Refuse ``requested_by_claim``/``key_id``/``key_name`` unless str|None.

    Shared by :class:`ClassifyRuleApplyHandler` and
    :class:`ClassifyRuleRevokeHandler`, called before either builds or
    removes a row. No shipped surface sends anything else today --
    ``requested_by_claim`` comes from ``command.args``, which no surface
    sets, and REST/CLI only ever stamp ``metadata``'s ``key_id``/
    ``key_name`` as ``str | None`` -- but nothing upstream of this
    handler enforces that, and a non-str value here would otherwise
    reach the audit payload unchecked: a ``float("nan")`` claim passes
    straight through ``_validate_jsonb_safe_payload`` (it is not a
    ``str``) and only ``json.dumps(..., allow_nan=False)`` would catch
    it, while a ``set`` (or anything else ``json.dumps`` cannot encode
    at all) raises an uncaught ``TypeError`` that neither probe below
    catches, turning a bad claim into a handler panic rather than a
    clean refusal.

    Args:
        requested_by_claim: ``command.args.get("requested_by_claim")``.
        key_id: ``command.metadata.get("key_id")``.
        key_name: ``command.metadata.get("key_name")``.
        operation: ``"apply"`` or ``"revoke"``, for the error message.

    Raises:
        ValidationError: one of the three is neither ``str`` nor
            ``None`` (``code="invalid_requested_by_claim"``,
            ``"invalid_key_id"`` or ``"invalid_key_name"``, matching
            whichever field failed first).
    """
    values = (requested_by_claim, key_id, key_name)
    for value, (field, code) in zip(
        values, _CLASSIFY_RULE_IDENTITY_FIELD_CODES, strict=True
    ):
        if value is not None and not isinstance(value, str):
            msg = (
                f"classify_rule.{operation}: {field} must be a string or "
                f"null, got {type(value).__name__}"
            )
            raise ValidationError(msg, errors=[msg], code=code)


def _validate_json_serializable_payload(
    payload: dict[str, Any], *, operation: str
) -> None:
    """Refuse an audit ``payload`` ``json.dumps(..., allow_nan=False)`` rejects.

    Shared by :class:`ClassifyRuleApplyHandler` and
    :class:`ClassifyRuleRevokeHandler`, called before the write it would
    otherwise follow (``store.put`` in Apply, ``store.remove`` in
    Revoke). On Apply, ``_validate_classify_rule_metrics`` already
    refuses a non-finite support/precision/lift, so this should never
    fire there in practice -- it is the backstop for whatever the
    payload gains next that isn't a plain, finite number. Revoke carries
    no metrics of its own, but this is still the only probe that would
    catch one reaching its payload some other way (e.g. a legacy row's
    own fields). The actual event log backend serialises with
    ``json.dumps(..., allow_nan=False)`` regardless of which store backs
    it (SQLite and Postgres both), and a failure there, discovered only
    after the write already ran, would leave a rule applied or revoked
    with no event to show for it.

    Raises:
        ValidationError: ``json.dumps(payload, allow_nan=False)`` raises
            ``ValueError`` -- always for a non-finite float, which is
            the only shape reaching this point that it refuses
            (``code="invalid_metric"``).
    """
    try:
        json.dumps(payload, allow_nan=False)
    except ValueError as exc:
        # json.dumps(..., allow_nan=False) always raises ValueError(msg)
        # with a single string argument ("Out of range float values are
        # not JSON compliant: nan"), never a wrapped driver/library
        # exception, so args[0] is exactly that message -- a
        # structured-field read, not a bare str(exc) stringification of
        # the caught exception.
        detail = exc.args[0] if exc.args else "not serializable"
        msg = f"classify_rule.{operation}: audit payload is not serializable: {detail}"
        raise ValidationError(msg, errors=[msg], code="invalid_metric") from exc


class ClassifyRuleApplyHandler:
    """Apply one mined tag/domain candidate into the operator overlay.

    Persists a :class:`~trellis.schemas.classify_overrides.ClassifyOverrideRow`
    via :class:`~trellis.stores.classify_overrides_store.ClassifyOverridesStore`
    -- the governed, audited analogue of hand-pasting YAML into
    ``classify.domain_keywords`` / ``classify.domain_aliases`` in
    ``config.yaml``. Every real reader
    (:meth:`~trellis.stores.registry.StoreRegistry.build_ingestion_pipeline`,
    :meth:`~trellis.stores.registry.StoreRegistry.domain_keyword_map`,
    :meth:`~trellis.stores.registry.StoreRegistry.domain_alias_map`) builds
    a fresh overlay-aware map on every call, so an applied rule needs no
    restart to take effect.

    Re-applying an already-applied ``(kind, key)`` with a *different*
    ``target`` overwrites the row in place -- CLAUDE.md: "a wrong rule
    hides content" -- which is why this operation is classified
    ``DESTRUCTIVE`` in ``immutable_core.py``, the same reasoning as
    ``settings.update``.
    """

    def __init__(self, registry: StoreRegistry) -> None:
        self._registry = registry

    def handle(  # noqa: PLR0912, PLR0915
        self, command: Command
    ) -> tuple[str | None, str]:
        from trellis.classify.factory import (  # noqa: PLC0415
            config_sourced_alias_target,
            effective_domain_aliases,
            reject_reserved_rule_target,
        )
        from trellis.schemas.classify_overrides import (  # noqa: PLC0415
            ClassifyOverrideRow,
        )
        from trellis.stores.classify_overrides_store import (  # noqa: PLC0415
            ClassifyOverridesStore,
            resolve_classify_overrides_path,
        )

        kind = command.args["kind"]
        key = command.args["key"]
        target = command.args["target"]
        source_event_id = command.args["source_event_id"]

        if kind not in ("tag_keyword", "domain_alias"):
            msg = f"Unknown classify-rule kind: {kind!r}"
            raise ValidationError(msg, errors=[msg], code="unknown_kind")
        if not key or not isinstance(key, str):
            msg = "classify_rule.apply requires a non-empty string 'key'"
            raise ValidationError(msg, errors=[msg], code="invalid_key")
        _validate_classify_rule_identifier(key, field="key", code="invalid_key")
        if kind == "tag_keyword" and len(key) < MIN_TAG_KEYWORD_LENGTH:
            # The miner itself never proposes a token shorter than this
            # (``_TOKEN_RE`` in tag_evolution.py), because a short keyword
            # matches far too much under the classifier's substring
            # semantics and defeats the two-distinct-keyword minimum that
            # otherwise keeps one bad promotion from assigning a domain on
            # its own. apply-rule does not route through that regex, so
            # nothing else stops a one-letter key submitted by hand.
            msg = (
                f"classify_rule.apply: tag_keyword key {key!r} is shorter "
                f"than the {MIN_TAG_KEYWORD_LENGTH}-character minimum "
                "-- a short keyword matches far too much under the "
                "classifier's substring semantics and would defeat the "
                "two-hit minimum on its own."
            )
            raise ValidationError(msg, errors=[msg], code="tag_keyword_too_short")
        if not target or not isinstance(target, str):
            msg = "classify_rule.apply requires a non-empty string 'target'"
            raise ValidationError(msg, errors=[msg], code="invalid_target")
        _validate_classify_rule_identifier(
            target, field="target", code="invalid_target"
        )
        if not source_event_id or not isinstance(source_event_id, str):
            msg = (
                "classify_rule.apply requires a non-empty string "
                "'source_event_id' so the applied rule can be traced back "
                "to the candidate it was mined from"
            )
            raise ValidationError(msg, errors=[msg], code="invalid_source_event_id")
        _validate_classify_rule_identifier(
            source_event_id, field="source_event_id", code="invalid_source_event_id"
        )
        _validate_classify_rule_metrics(command.args)
        # Before any row is built: a non-str/non-None requested_by_claim
        # or metadata key_id/key_name would otherwise reach the audit
        # payload unchecked (see _validate_classify_rule_identity_fields's
        # own docstring for exactly what the two probes further down
        # would and would not have caught on their own).
        _validate_classify_rule_identity_fields(
            requested_by_claim=command.args.get("requested_by_claim"),
            key_id=command.metadata.get("key_id"),
            key_name=command.metadata.get("key_name"),
            operation="apply",
        )

        row_id = f"{kind}:{key}"
        # Defence in depth, same posture as SettingsUpdateHandler: this
        # handler is only ever registered via create_classify_handlers,
        # reachable only through the admin route/CLI, which both stamp
        # target_type/target_id correctly -- but a mistargeted Command
        # reaching here by some future wiring mistake must still refuse
        # rather than write.
        if command.target_type != "classify_rule" or command.target_id != row_id:
            msg = (
                "classify_rule.apply requires target_type='classify_rule' "
                f"and target_id={row_id!r}; got "
                f"target_type={command.target_type!r}, "
                f"target_id={command.target_id!r}"
            )
            raise ValidationError(msg, errors=[msg], code="untargeted_command")

        # A reserved policy namespace (sensitivity, lifecycle, ...) is never
        # a legitimate domain value -- checked before the row ever reaches
        # the overlay file, not discovered only once a later reader tries
        # to build a merged domain map from it and fails. For a
        # domain_alias row, the key is itself a domain name once merged
        # (the alias side of ``alias -> canonical``), so it is checked
        # too -- without this, ``sensitivity -> privacy`` was accepted
        # because only the clean target was ever looked at.
        try:
            reject_reserved_rule_target(target)
            if kind == "domain_alias":
                reject_reserved_rule_target(key, what="key", casefold=True)
        except ValueError as exc:
            # reject_reserved_rule_target always raises ValueError(msg) with
            # a single string it composed itself, never a wrapped
            # driver/library exception, so args[0] is exactly that message
            # -- a structured-field read, not a bare str(exc) stringification
            # of the caught exception (same convention as validate_value
            # above).
            msg = exc.args[0] if exc.args else "reserved namespace target"
            raise ValidationError(
                msg, errors=[msg], code="reserved_namespace_target"
            ) from exc

        path = resolve_classify_overrides_path(self._registry.stores_dir)
        if path is None:
            # Same precedent as SettingsUpdateHandler: no stores_dir means
            # no file-backed overlay store exists to write to -- not a
            # caller-input problem, so this is left for the executor's
            # handler-panic path rather than raised as ValidationError.
            msg = "stores_dir must be set on registry to use ClassifyOverridesStore"
            raise ValueError(msg)
        store = ClassifyOverridesStore(path)

        classify_config = self._registry.classify_config
        if kind == "domain_alias":
            # Validate the *prospective* merged alias map -- this key's
            # new target folded into every other already-applied alias
            # row -- before it ever reaches the overlay file. A self-map
            # (``a -> a``) or a chain this apply would introduce (this
            # key plus an existing row forming ``a -> b -> c``) must be
            # refused here; by the time a later reader calls
            # ``effective_domain_aliases`` on the written file, the bad
            # row is already live.
            prospective = dict(store.domain_aliases_map())
            prospective[key] = target
            try:
                effective_domain_aliases(classify_config, overlay_aliases=prospective)
            except ValueError as exc:
                # effective_domain_aliases always raises ValueError(msg) with
                # a single string it composed itself, never a wrapped
                # driver/library exception, so args[0] is exactly that
                # message -- a structured-field read, not a bare str(exc)
                # stringification of the caught exception.
                msg = exc.args[0] if exc.args else "invalid alias rule"
                raise ValidationError(
                    msg, errors=[msg], code="invalid_alias_rule"
                ) from exc

        # Captured before the write so the audit event can record exactly
        # what this apply replaces -- both the overlay row it overwrites
        # (re-applying an already-applied key) and, for a domain_alias,
        # the config.yaml value this key's overlay precedence now shadows.
        existing = store.get(kind, key)
        config_target = (
            config_sourced_alias_target(classify_config, key)
            if kind == "domain_alias"
            else None
        )

        # Probed before use, not just before the write: ``applied_by``
        # below can resolve to a caller-supplied metadata string that
        # ``ClassifyOverrideRow`` itself would reject with its own
        # ``pydantic_core.ValidationError`` (unable to encode a lone
        # surrogate) rather than this handler's ``ValidationError`` --
        # the row construction a few lines down happens before the
        # payload-level ``_validate_jsonb_safe_payload`` call ever runs,
        # so a NUL/surrogate confined to ``metadata`` needs its own,
        # earlier check to get the same ``unsafe_audit_payload`` code and
        # leave no row written.
        _validate_jsonb_safe_payload(
            {
                "key_id": command.metadata.get("key_id"),
                "key_name": command.metadata.get("key_name"),
            },
            operation="apply",
        )

        # Structured caller identity, when the surface supplies one
        # (REST: the authenticated key's id/name; CLI: the OS user),
        # beats the constant surface label in ``command.requested_by``
        # (e.g. ``"api:classify.apply-rule"``) for a field meant to
        # answer "who applied this", not "through what entry point".
        applied_by = (
            command.metadata.get("key_id")
            or command.metadata.get("key_name")
            or command.requested_by
        )
        row = ClassifyOverrideRow(
            kind=kind,
            key=key,
            target=target,
            applied_at=datetime.now(UTC),
            applied_by=applied_by,
            source_event_id=source_event_id,
        )
        payload: dict[str, Any] = {
            "kind": kind,
            "key": key,
            "target": target,
            "support": command.args.get("support"),
            "precision": command.args.get("precision"),
            "lift": command.args.get("lift"),
            "applied_by": applied_by,
            "key_id": command.metadata.get("key_id"),
            "key_name": command.metadata.get("key_name"),
            "source_event_id": source_event_id,
            "prior_target": existing.target if existing is not None else None,
            "prior_applied_by": (
                _scrub_stranded_legacy_field(existing.applied_by)
                if existing is not None
                else None
            ),
            "prior_applied_at": (
                existing.applied_at.isoformat() if existing is not None else None
            ),
            "prior_source_event_id": (
                _scrub_stranded_legacy_field(existing.source_event_id)
                if existing is not None
                else None
            ),
            "config_target": config_target,
        }
        requested_by_claim = command.args.get("requested_by_claim")
        if requested_by_claim is not None:
            payload["requested_by_claim"] = requested_by_claim

        # Built and probed BEFORE the overlay write, in two independent
        # passes over the same payload (shared with Revoke -- see each
        # helper's own docstring): the first catches a non-finite float
        # that ``_validate_classify_rule_metrics`` should already have
        # refused, so it should never fire in practice, but it is the
        # backstop for whatever this payload gains next that isn't a
        # plain, finite number. Discovered only after ``store.put``
        # already wrote the row, either failure would apply a rule with
        # no ``CLASSIFY_RULE_APPLIED`` event to show for it.
        _validate_json_serializable_payload(payload, operation="apply")

        # A second, independent probe on the same built payload: the
        # json-serializable check above catches a non-finite float, but
        # a NUL byte or lone surrogate round-trips through json.dumps
        # just fine and only fails once a Postgres jsonb column refuses
        # to store it.
        _validate_jsonb_safe_payload(payload, operation="apply")

        try:
            store.put(row)
        except (DegradedStoreWriteError, StaleStoreWriteError) as exc:
            raise ValidationError(
                exc.message, errors=[exc.message], code=exc.code.lower()
            ) from exc

        self._registry.operational.event_log.emit(
            EventType.CLASSIFY_RULE_APPLIED,
            source="mutation_executor",
            entity_id=row_id,
            payload=payload,
        )
        return row_id, f"Applied {kind} rule {key!r} -> {target!r}"


class ClassifyRuleRevokeHandler:
    """Revoke one applied rule from the operator overlay.

    Refuses -- as a :class:`~trellis.errors.ValidationError`, which the
    executor turns into a plain ``REJECTED`` result (REST: 400) rather
    than a panic -- when ``(kind, key)`` has no overlay row. The message
    names ``config.yaml`` specifically when the rule exists there instead:
    such a rule was never written by this store and cannot be un-applied
    through it. A genuinely unknown ``(kind, key)`` gets a different,
    equally refused message, so a typo is never told "edit config.yaml".
    """

    def __init__(self, registry: StoreRegistry) -> None:
        self._registry = registry

    def handle(  # noqa: PLR0912, PLR0915
        self, command: Command
    ) -> tuple[str | None, str]:
        from trellis.classify.factory import (  # noqa: PLC0415
            config_sourced_alias_target,
            config_sourced_tag_keyword_domains,
            effective_domain_aliases,
            is_config_sourced_alias,
        )
        from trellis.stores.classify_overrides_store import (  # noqa: PLC0415
            ClassifyOverridesStore,
            resolve_classify_overrides_path,
        )

        kind = command.args["kind"]
        key = command.args["key"]

        if kind not in ("tag_keyword", "domain_alias"):
            msg = f"Unknown classify-rule kind: {kind!r}"
            raise ValidationError(msg, errors=[msg], code="unknown_kind")
        if not key or not isinstance(key, str):
            msg = "classify_rule.revoke requires a non-empty string 'key'"
            raise ValidationError(msg, errors=[msg], code="invalid_key")
        _validate_classify_rule_identifier(key, field="key", code="invalid_key")
        # Before any row is removed: the same type guard Apply runs,
        # same reason -- see _validate_classify_rule_identity_fields's
        # own docstring.
        _validate_classify_rule_identity_fields(
            requested_by_claim=command.args.get("requested_by_claim"),
            key_id=command.metadata.get("key_id"),
            key_name=command.metadata.get("key_name"),
            operation="revoke",
        )

        row_id = f"{kind}:{key}"
        if command.target_type != "classify_rule" or command.target_id != row_id:
            msg = (
                "classify_rule.revoke requires target_type='classify_rule' "
                f"and target_id={row_id!r}; got "
                f"target_type={command.target_type!r}, "
                f"target_id={command.target_id!r}"
            )
            raise ValidationError(msg, errors=[msg], code="untargeted_command")

        path = resolve_classify_overrides_path(self._registry.stores_dir)
        if path is None:
            msg = "stores_dir must be set on registry to use ClassifyOverridesStore"
            raise ValueError(msg)
        store = ClassifyOverridesStore(path)

        existing = store.get(kind, key)
        if existing is None:
            if store.is_degraded:
                # A degraded read cannot tell "never applied" apart from
                # "the row that WAS applied failed to parse" -- defer to
                # the write path's own refusal (which answers 409 with
                # the real ``store_degradation``) rather than guessing a
                # 400 "no such rule" from a read that might be wrong.
                try:
                    store.remove(kind, key)
                except (DegradedStoreWriteError, StaleStoreWriteError) as exc:
                    raise ValidationError(
                        exc.message, errors=[exc.message], code=exc.code.lower()
                    ) from exc
                # remove() refuses before ever reaching a return on a
                # degraded store, so this is unreachable in practice --
                # kept only so a future change to that refusal order
                # fails loudly here instead of silently answering 400.
                msg = (
                    f"No applied {kind} rule found for key {key!r}, but the "
                    "classify-overrides store did not refuse the write."
                )
                raise ValueError(msg)

            classify_config = self._registry.classify_config
            if kind == "tag_keyword":
                config_domains = config_sourced_tag_keyword_domains(
                    classify_config, key
                )
                config_sourced = bool(config_domains)
                where = (
                    f"config.yaml (domain(s): {', '.join(config_domains)})"
                    if config_sourced
                    else None
                )
            else:
                config_sourced = is_config_sourced_alias(classify_config, key)
                where = "config.yaml" if config_sourced else None

            if config_sourced:
                msg = (
                    f"{kind} rule {key!r} comes from {where}, not the "
                    "operator overlay. Nothing to revoke here -- edit "
                    "config.yaml directly to change or remove it."
                )
                raise ValidationError(msg, errors=[msg], code="not_overlay_sourced")
            msg = f"No applied {kind} rule found for key {key!r}."
            raise ValidationError(msg, errors=[msg], code="no_such_rule")

        if kind == "domain_alias":
            # Revoking a row can re-expose a chain or self-map that was
            # only hidden because this overlay entry shadowed a
            # config.yaml alias -- e.g. config.yaml has ``fin -> finance``
            # and ``finance -> treasury`` (already a chain on its own,
            # but never merged because nothing read it that way until an
            # overlay row pointed at one end of it), or an overlay applies
            # ``fin -> money`` then ``finance -> treasury``, each fine
            # alone, and revoking ``fin`` exposes ``finance -> treasury``
            # chained with config's own ``fin -> finance``. Validate the
            # prospective merged map -- this store's rows minus the key
            # being revoked -- the same check Apply makes before it
            # writes, so the chain is refused here rather than only
            # discovered the next time a reader calls
            # ``effective_domain_aliases``.
            prospective = {
                k: v for k, v in store.domain_aliases_map().items() if k != key
            }
            try:
                effective_domain_aliases(
                    self._registry.classify_config, overlay_aliases=prospective
                )
            except ValueError as exc:
                # effective_domain_aliases always raises ValueError(msg)
                # with a single string it composed itself, never a
                # wrapped driver/library exception, so args[0] is exactly
                # that message -- same structured-field read Apply uses.
                msg = exc.args[0] if exc.args else "invalid alias rule"
                raise ValidationError(
                    msg, errors=[msg], code="invalid_alias_rule"
                ) from exc

        config_target = (
            config_sourced_alias_target(self._registry.classify_config, key)
            if kind == "domain_alias"
            else None
        )
        payload: dict[str, Any] = {
            "kind": kind,
            "key": key,
            "target": existing.target,
            "revoked_by": command.requested_by,
            "key_id": command.metadata.get("key_id"),
            "key_name": command.metadata.get("key_name"),
            "removed_applied_by": _scrub_stranded_legacy_field(existing.applied_by),
            "removed_applied_at": existing.applied_at.isoformat(),
            "removed_source_event_id": _scrub_stranded_legacy_field(
                existing.source_event_id
            ),
            "config_target": config_target,
        }
        requested_by_claim = command.args.get("requested_by_claim")
        if requested_by_claim is not None:
            payload["requested_by_claim"] = requested_by_claim

        # Built and probed BEFORE the store write, with the same two
        # probes Apply runs, in the same order. A NUL byte or lone
        # surrogate can still reach this payload inside a str the type
        # guard above lets through (``command.metadata``'s identity
        # fields) -- ``existing``'s own ``source_event_id``/``applied_by``
        # are pre-scrubbed above instead, since those may have been
        # written by an earlier build or by hand-editing the overlay
        # file, a value this call cannot fix by resubmitting different
        # input (issue #869). No current field can carry a
        # non-finite float here; the serializability probe is a
        # backstop for whatever this payload gains next. Discovering
        # either failure only after
        # ``store.remove`` already ran would revoke the rule with no
        # ``CLASSIFY_RULE_REVOKED`` event to show for it; probing first
        # leaves the row in place on a refusal.
        _validate_json_serializable_payload(payload, operation="revoke")
        _validate_jsonb_safe_payload(payload, operation="revoke")

        try:
            # ``existing`` above is non-None, so this row is live in a
            # non-degraded store -- the only ways to reach this point --
            # and ``remove`` refuses rather than returning ``False`` on a
            # degraded/stale store (caught below). ``removed`` is
            # therefore always ``True`` here; no variable is kept for it.
            store.remove(kind, key)
        except (DegradedStoreWriteError, StaleStoreWriteError) as exc:
            raise ValidationError(
                exc.message, errors=[exc.message], code=exc.code.lower()
            ) from exc

        self._registry.operational.event_log.emit(
            EventType.CLASSIFY_RULE_REVOKED,
            source="mutation_executor",
            entity_id=row_id,
            payload=payload,
        )
        return row_id, f"{kind} rule {key!r} revoked"
