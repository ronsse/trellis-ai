"""Command handlers for curate operations.

This package replaced the single ``handlers.py`` module. Every name that module
exposed is still importable from ``trellis.mutate.handlers``; the code lives in
one submodule per handler family:

* ``_common`` -- helpers shared by more than one family
* ``evidence_handler``, ``trace_ingest``, ``feedback``, ``labels``, ``settings``
* ``classification`` -- ``classification.set`` and ``classification.tag_regulatory``
* ``classify_rules`` -- ``classify_rule.apply`` and ``classify_rule.revoke``
* ``graph`` -- entities, aliases, links, observations and measurements
* ``precedent`` -- promote, demote and reinstate
* ``redaction`` -- ``redaction.apply`` and the precedent scrub it drives
* ``retention_handlers`` -- ``retention.prune`` and ``retention.restore``
* ``api_keys`` -- ``api_key.mint`` and ``api_key.revoke``
* ``factories`` -- the ``create_*_handlers`` handler-map builders

A name that a test patches must be patched on the submodule that *uses* it, not
on this façade.
"""

from __future__ import annotations

# Defined beside the seam that enforces them (#1034); re-exported because
# ``trellis.learning.scoring`` and retention restore import them from here.
from trellis.core.document_write import (
    REDACTION_TOMBSTONE_REASON,
    RedactionTombstoneError,
    _guard_tombstone,
    is_redaction_tombstone,
    put_document,
)
from trellis.learning.candidate_auto_promote import AUTO_PROMOTE_REQUESTED_BY
from trellis.mutate.handlers._common import (
    _LINKED_SIGNAL_LIMIT,
    _REWRITE_ATTEMPTS,
    _current_lifecycle_state,
    _rewrite_current_node,
    _sync_vector_lifecycle,
)
from trellis.mutate.handlers.api_keys import (
    ApiKeyMintHandler,
    ApiKeyRevokeHandler,
    _api_key_actor,
)
from trellis.mutate.handlers.classification import (
    MAX_REGULATORY_TAG_LENGTH,
    MAX_REGULATORY_TAGS,
    ClassificationSetHandler,
    ClassificationTagRegulatoryHandler,
    _check_one_regulatory_tag,
    _check_regulatory_tags,
    _public_record_discards,
    _relabelled_record,
    _same_level_suffix,
    _stored_tags,
    _tag_list,
    normalize_regulatory_tags,
)
from trellis.mutate.handlers.classify_rules import (
    _CLASSIFY_RULE_IDENTITY_FIELD_CODES,
    _MAX_CLASSIFY_RULE_IDENTIFIER_LENGTH,
    ClassifyRuleApplyHandler,
    ClassifyRuleRevokeHandler,
    _find_unsafe_jsonb_string,
    _scrub_stranded_legacy_field,
    _validate_classify_rule_identifier,
    _validate_classify_rule_identity_fields,
    _validate_classify_rule_metrics,
    _validate_json_serializable_payload,
    _validate_jsonb_safe_payload,
)
from trellis.mutate.handlers.evidence_handler import (
    EvidenceIngestHandler,
    _evidence_document_id,
    _evidence_flag,
)
from trellis.mutate.handlers.factories import (
    create_api_key_handlers,
    create_classify_handlers,
    create_curate_handlers,
    create_settings_handlers,
)
from trellis.mutate.handlers.feedback import FeedbackRecordHandler
from trellis.mutate.handlers.graph import (
    AliasUpsertHandler,
    EntityCreateHandler,
    EntityUpdateHandler,
    LinkCreateHandler,
    MeasurementRecordHandler,
    ObservationRecordHandler,
    _bind_entity_name_alias,
)
from trellis.mutate.handlers.labels import LabelAddHandler, LabelRemoveHandler
from trellis.mutate.handlers.precedent import (
    AUTO_DEMOTE_REQUESTED_BY,
    DOCUMENT_ROW_SKIPPED_TOMBSTONE,
    MAX_DEMOTION_REASON_CHARS,
    PrecedentCandidateHandler,
    PrecedentDemoteHandler,
    PrecedentPromoteHandler,
    PrecedentReinstateHandler,
    StampOutcome,
    _is_promoted_precedent_event,
    _note_skipped_tombstone,
    _precedent_document_state,
    _stamp_precedent_document,
    _was_auto_promoted,
    _write_precedent_document,
    precedent_already_demoted_message,
)
from trellis.mutate.handlers.redaction import (
    _PRECEDENT_PAGE_SIZE,
    MAX_REDACTION_REASON_CHARS,
    RedactionApplyHandler,
    _binds_a_name_in,
    _log_blanked_before_failure,
    _log_scrubbed_before_failure,
    _precedents_sourced_from,
    _PrecedentScrub,
    _reembed_scrubbed_precedent,
    _rewrite_precedent_document,
    _scrub_precedent_excerpt,
    _scrub_precedents,
    _scrub_then_purge,
    _tombstone_document,
    _tombstone_redacted_documents,
)
from trellis.mutate.handlers.retention_handlers import (
    MAX_RETENTION_REASON_CHARS,
    RetentionPruneHandler,
    RetentionRestoreHandler,
    _pre_archival_lifecycle,
    _prune_message,
    _raise_nothing_restored,
    _validate_restore_args,
)
from trellis.mutate.handlers.settings import SettingsUpdateHandler
from trellis.mutate.handlers.trace_ingest import TraceIngestHandler

__all__ = [
    "AUTO_DEMOTE_REQUESTED_BY",
    "AUTO_PROMOTE_REQUESTED_BY",
    "AliasUpsertHandler",
    "ApiKeyMintHandler",
    "ApiKeyRevokeHandler",
    "ClassificationSetHandler",
    "ClassificationTagRegulatoryHandler",
    "ClassifyRuleApplyHandler",
    "ClassifyRuleRevokeHandler",
    "DOCUMENT_ROW_SKIPPED_TOMBSTONE",
    "EntityCreateHandler",
    "EntityUpdateHandler",
    "EvidenceIngestHandler",
    "FeedbackRecordHandler",
    "LabelAddHandler",
    "LabelRemoveHandler",
    "LinkCreateHandler",
    "MAX_DEMOTION_REASON_CHARS",
    "MAX_REDACTION_REASON_CHARS",
    "MAX_REGULATORY_TAGS",
    "MAX_REGULATORY_TAG_LENGTH",
    "MAX_RETENTION_REASON_CHARS",
    "MeasurementRecordHandler",
    "ObservationRecordHandler",
    "PrecedentCandidateHandler",
    "PrecedentDemoteHandler",
    "PrecedentPromoteHandler",
    "PrecedentReinstateHandler",
    "REDACTION_TOMBSTONE_REASON",
    "RedactionApplyHandler",
    "RedactionTombstoneError",
    "RetentionPruneHandler",
    "RetentionRestoreHandler",
    "SettingsUpdateHandler",
    "StampOutcome",
    "TraceIngestHandler",
    "_CLASSIFY_RULE_IDENTITY_FIELD_CODES",
    "_LINKED_SIGNAL_LIMIT",
    "_MAX_CLASSIFY_RULE_IDENTIFIER_LENGTH",
    "_PRECEDENT_PAGE_SIZE",
    "_PrecedentScrub",
    "_REWRITE_ATTEMPTS",
    "_api_key_actor",
    "_bind_entity_name_alias",
    "_binds_a_name_in",
    "_check_one_regulatory_tag",
    "_check_regulatory_tags",
    "_current_lifecycle_state",
    "_evidence_document_id",
    "_evidence_flag",
    "_find_unsafe_jsonb_string",
    "_guard_tombstone",
    "_is_promoted_precedent_event",
    "_log_blanked_before_failure",
    "_log_scrubbed_before_failure",
    "_note_skipped_tombstone",
    "_pre_archival_lifecycle",
    "_precedent_document_state",
    "_precedents_sourced_from",
    "_prune_message",
    "_public_record_discards",
    "_raise_nothing_restored",
    "_reembed_scrubbed_precedent",
    "_relabelled_record",
    "_rewrite_current_node",
    "_rewrite_precedent_document",
    "_same_level_suffix",
    "_scrub_precedent_excerpt",
    "_scrub_precedents",
    "_scrub_stranded_legacy_field",
    "_scrub_then_purge",
    "_stamp_precedent_document",
    "_stored_tags",
    "_sync_vector_lifecycle",
    "_tag_list",
    "_tombstone_document",
    "_tombstone_redacted_documents",
    "_validate_classify_rule_identifier",
    "_validate_classify_rule_identity_fields",
    "_validate_classify_rule_metrics",
    "_validate_json_serializable_payload",
    "_validate_jsonb_safe_payload",
    "_validate_restore_args",
    "_was_auto_promoted",
    "_write_precedent_document",
    "create_api_key_handlers",
    "create_classify_handlers",
    "create_curate_handlers",
    "create_settings_handlers",
    "is_redaction_tombstone",
    "normalize_regulatory_tags",
    "precedent_already_demoted_message",
    "put_document",
]
