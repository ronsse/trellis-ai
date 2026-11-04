"""The ``trellis.mutate.handlers`` package façade keeps the old module's surface.

``handlers.py`` was split into a package with one submodule per handler family.
Importers (CLI, REST, workers, a dozen test files) still say
``from trellis.mutate.handlers import X``, so the package ``__init__`` re-exports
every name the module exposed: each top-level definition, plus the five names
the module deliberately re-exported from ``trellis.core.document_write`` and the
auto-promote identity it imported.

The mapping below is a literal, generated once from the pre-split module's
top-level definitions and committed, so a name dropped from the façade (or one
that quietly rebinds to a different object) fails here by name rather than at
some distant importer.
"""

from __future__ import annotations

import importlib

import pytest

from trellis.mutate import handlers

#: ``name -> the module that owns the object``.
FACADE_SURFACE: dict[str, str] = {
    "AUTO_DEMOTE_REQUESTED_BY": "trellis.mutate.handlers.precedent",
    "AUTO_PROMOTE_REQUESTED_BY": "trellis.learning.candidate_auto_promote",
    "AliasUpsertHandler": "trellis.mutate.handlers.graph",
    "ApiKeyMintHandler": "trellis.mutate.handlers.api_keys",
    "ApiKeyRevokeHandler": "trellis.mutate.handlers.api_keys",
    "ApiKeyUpdateHandler": "trellis.mutate.handlers.api_keys",
    "ClassificationSetHandler": "trellis.mutate.handlers.classification",
    "ClassificationTagRegulatoryHandler": "trellis.mutate.handlers.classification",
    "ClassifyRuleApplyHandler": "trellis.mutate.handlers.classify_rules",
    "ClassifyRuleRevokeHandler": "trellis.mutate.handlers.classify_rules",
    "DOCUMENT_ROW_SKIPPED_TOMBSTONE": "trellis.mutate.handlers.precedent",
    "EntityCreateHandler": "trellis.mutate.handlers.graph",
    "EntityUpdateHandler": "trellis.mutate.handlers.graph",
    "EvidenceIngestHandler": "trellis.mutate.handlers.evidence_handler",
    "FeedbackRecordHandler": "trellis.mutate.handlers.feedback",
    "LabelAddHandler": "trellis.mutate.handlers.labels",
    "LabelRemoveHandler": "trellis.mutate.handlers.labels",
    "LinkCreateHandler": "trellis.mutate.handlers.graph",
    "MAX_DEMOTION_REASON_CHARS": "trellis.mutate.handlers.precedent",
    "MAX_REDACTION_REASON_CHARS": "trellis.mutate.handlers.redaction",
    "MAX_REGULATORY_TAGS": "trellis.mutate.handlers.classification",
    "MAX_REGULATORY_TAG_LENGTH": "trellis.mutate.handlers.classification",
    "MAX_RETENTION_REASON_CHARS": "trellis.mutate.handlers.retention_handlers",
    "MeasurementRecordHandler": "trellis.mutate.handlers.graph",
    "ObservationRecordHandler": "trellis.mutate.handlers.graph",
    "PrecedentDemoteHandler": "trellis.mutate.handlers.precedent",
    "PrecedentPromoteHandler": "trellis.mutate.handlers.precedent",
    "PrecedentReinstateHandler": "trellis.mutate.handlers.precedent",
    "REDACTION_TOMBSTONE_REASON": "trellis.core.document_write",
    "RedactionApplyHandler": "trellis.mutate.handlers.redaction",
    "RedactionTombstoneError": "trellis.core.document_write",
    "RetentionPruneHandler": "trellis.mutate.handlers.retention_handlers",
    "RetentionRestoreHandler": "trellis.mutate.handlers.retention_handlers",
    "SettingsUpdateHandler": "trellis.mutate.handlers.settings",
    "StampOutcome": "trellis.mutate.handlers.precedent",
    "TraceIngestHandler": "trellis.mutate.handlers.trace_ingest",
    "_CLASSIFY_RULE_IDENTITY_FIELD_CODES": "trellis.mutate.handlers.classify_rules",
    "_LINKED_SIGNAL_LIMIT": "trellis.mutate.handlers._common",
    "_MAX_CLASSIFY_RULE_IDENTIFIER_LENGTH": "trellis.mutate.handlers.classify_rules",
    "_PRECEDENT_PAGE_SIZE": "trellis.mutate.handlers.redaction",
    "_PrecedentScrub": "trellis.mutate.handlers.redaction",
    "_REWRITE_ATTEMPTS": "trellis.mutate.handlers._common",
    "_api_key_actor": "trellis.mutate.handlers.api_keys",
    "_bind_entity_name_alias": "trellis.mutate.handlers.graph",
    "_binds_a_name_in": "trellis.mutate.handlers.redaction",
    "_check_one_regulatory_tag": "trellis.mutate.handlers.classification",
    "_check_regulatory_tags": "trellis.mutate.handlers.classification",
    "_current_lifecycle_state": "trellis.mutate.handlers._common",
    "_evidence_document_id": "trellis.mutate.handlers.evidence_handler",
    "_evidence_flag": "trellis.mutate.handlers.evidence_handler",
    "_find_unsafe_jsonb_string": "trellis.mutate.handlers.classify_rules",
    "_guard_tombstone": "trellis.core.document_write",
    "_is_promoted_precedent_event": "trellis.mutate.handlers.precedent",
    "_log_blanked_before_failure": "trellis.mutate.handlers.redaction",
    "_log_scrubbed_before_failure": "trellis.mutate.handlers.redaction",
    "_note_skipped_tombstone": "trellis.mutate.handlers.precedent",
    "_pre_archival_lifecycle": "trellis.mutate.handlers.retention_handlers",
    "_precedent_document_state": "trellis.mutate.handlers.precedent",
    "_precedents_sourced_from": "trellis.mutate.handlers.redaction",
    "_prune_message": "trellis.mutate.handlers.retention_handlers",
    "_public_record_discards": "trellis.mutate.handlers.classification",
    "_raise_nothing_restored": "trellis.mutate.handlers.retention_handlers",
    "_reembed_scrubbed_precedent": "trellis.mutate.handlers.redaction",
    "_relabelled_record": "trellis.mutate.handlers.classification",
    "_rewrite_current_node": "trellis.mutate.handlers._common",
    "_rewrite_precedent_document": "trellis.mutate.handlers.redaction",
    "_same_level_suffix": "trellis.mutate.handlers.classification",
    "_scrub_precedent_excerpt": "trellis.mutate.handlers.redaction",
    "_scrub_precedents": "trellis.mutate.handlers.redaction",
    "_scrub_stranded_legacy_field": "trellis.mutate.handlers.classify_rules",
    "_scrub_then_purge": "trellis.mutate.handlers.redaction",
    "_stamp_precedent_document": "trellis.mutate.handlers.precedent",
    "_stored_tags": "trellis.mutate.handlers.classification",
    "_sync_vector_lifecycle": "trellis.mutate.handlers._common",
    "_tag_list": "trellis.mutate.handlers.classification",
    "_tombstone_document": "trellis.mutate.handlers.redaction",
    "_tombstone_redacted_documents": "trellis.mutate.handlers.redaction",
    "_validate_classify_rule_identifier": "trellis.mutate.handlers.classify_rules",
    "_validate_classify_rule_identity_fields": "trellis.mutate.handlers.classify_rules",
    "_validate_classify_rule_metrics": "trellis.mutate.handlers.classify_rules",
    "_validate_json_serializable_payload": "trellis.mutate.handlers.classify_rules",
    "_validate_jsonb_safe_payload": "trellis.mutate.handlers.classify_rules",
    "_validate_restore_args": "trellis.mutate.handlers.retention_handlers",
    "_was_auto_promoted": "trellis.mutate.handlers.precedent",
    "_write_precedent_document": "trellis.mutate.handlers.precedent",
    "create_api_key_handlers": "trellis.mutate.handlers.factories",
    "create_classify_handlers": "trellis.mutate.handlers.factories",
    "create_curate_handlers": "trellis.mutate.handlers.factories",
    "create_settings_handlers": "trellis.mutate.handlers.factories",
    "is_redaction_tombstone": "trellis.core.document_write",
    "normalize_regulatory_tags": "trellis.mutate.handlers.classification",
    "precedent_already_demoted_message": "trellis.mutate.handlers.precedent",
    "put_document": "trellis.core.document_write",
}


@pytest.mark.parametrize(("name", "owner"), sorted(FACADE_SURFACE.items()))
def test_facade_name_is_the_owning_modules_object(name: str, owner: str) -> None:
    owning_module = importlib.import_module(owner)
    assert getattr(handlers, name) is getattr(owning_module, name)


def test_every_facade_name_is_in_all() -> None:
    assert set(FACADE_SURFACE) <= set(handlers.__all__)


def test_facade_surface_has_the_expected_size() -> None:
    # 93 = 87 top-level definitions of the pre-split module (``logger`` stays
    # private to each submodule, ``ApiKeyUpdateHandler`` joined later) + 6
    # re-exported imports.
    assert len(FACADE_SURFACE) == 93
