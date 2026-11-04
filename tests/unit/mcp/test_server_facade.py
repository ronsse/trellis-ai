"""The ``trellis.mcp.server`` façade keeps the old module's surface.

``server.py`` was split by tool family. Importers (the API, the CLI, a few dozen
test files) still say ``from trellis.mcp.server import X``, so the façade
re-exports every name the old module exposed, from the module that now owns it.

The mapping below is a literal, generated once from the pre-split module's
top-level definitions and committed, so a name dropped from the façade (or one
that quietly rebinds to a different object) fails here by name rather than at
some distant importer.

One group of names is deliberately *not* here: module state that a function
rebinds with ``global`` (``_registry``, ``_minhash_index`` and the
``_memory_extractor*`` flags). A copy on the façade would freeze at its
import-time value, so a test assigning to it there would stop biting without a
word. Those live on ``trellis.mcp.wiring`` only, and
``test_name_left_off_the_facade_lives_on_wiring`` keeps it that way.
"""

from __future__ import annotations

import importlib

import pytest

import trellis.mcp.server as server_mod

#: ``name -> the module that owns the object``.
FACADE_SURFACE: dict[str, str] = {
    "MUTATION_FAILED": "trellis.mcp.boundary",
    "POLICY_DENIED": "trellis.mcp.boundary",
    "PUBLIC_READ_BUSY": "trellis.mcp.boundary",
    "RESOURCE_NOT_FOUND": "trellis.mcp.boundary",
    "SAVE_MEMORY_SURFACE": "trellis.mcp.tools.memory",
    "_ExplainArgumentRejections": "trellis.mcp.rejections",
    "_FLAT_MAX_ITEMS": "trellis.mcp.tools.context",
    "_FailureNotingStore": "trellis.mcp.tools.feedback",
    "_GET_CONTEXT_SECTIONS_LEGACY_MAX_TOKENS": "trellis.mcp.tools.context",
    "_GET_ITEMS_MAX_IDS": "trellis.mcp.tools.lookup",
    "_LIST_API_KEYS_FAULT": "trellis.mcp.tools.admin",
    "_MAX_CITABLE_IDS_IN_ERROR": "trellis.mcp.tools.feedback",
    "_MAX_CITABLE_IDS_IN_MESSAGE": "trellis.mcp.tools.feedback",
    "_MAX_ECHOED_ID_CHARS": "trellis.mcp.tools.feedback",
    "_MAX_UNSERVED_IDS_IN_NOTE": "trellis.mcp.tools.feedback",
    "_MEMORY_EXTRACTOR_REFRESH_SECONDS": "trellis.mcp.wiring",
    "_MEMORY_EXTRACTOR_RETRY_SECONDS": "trellis.mcp.wiring",
    "_MINHASH_SEED_PAGE_SIZE": "trellis.mcp.wiring",
    "_MIN_INDEX_BUDGET_TOKENS": "trellis.mcp.tools.context",
    "_RECORDED_ARGUMENT_TOOLS": "trellis.mcp.rejections",
    "_REJECTION_RECOVERY": "trellis.mcp.rejections",
    "_REVOKE_API_KEY_FAULT": "trellis.mcp.tools.admin",
    "_SANITIZED_RESULT_STATUSES": "trellis.mcp.boundary",
    "_SanitizeUncaughtToolErrors": "trellis.mcp.rejections",
    "_TOOL_LABEL": "trellis.mcp.tools.context",
    "_UNKNOWN_PACK_NOTE": "trellis.mcp.tools.feedback",
    "_append_note": "trellis.mcp.tools.memory_common",
    "_apply_knowledge_supersedes": "trellis.mcp.tools.experience_knowledge",
    "_axis_note": "trellis.mcp.tools.context",
    "_build_alias_resolver": "trellis.mcp.wiring",
    "_build_pack_builder": "trellis.mcp.wiring",
    "_candidate_is_stale": "trellis.mcp.tools.memory",
    "_capture_warning_banner": "trellis.mcp.tools.context",
    "_check_memory_supersedes": "trellis.mcp.tools.memory",
    "_check_retrieval_phase": "trellis.mcp.boundary",
    "_close_registry": "trellis.mcp.wiring",
    "_commit_reconcile_verdict": "trellis.mcp.tools.memory",
    "_compute_reconcile_outcome": "trellis.mcp.tools.memory",
    "_context_banner": "trellis.mcp.tools.context",
    "_downgraded_unapplied": "trellis.mcp.tools.memory",
    "_echo_id": "trellis.mcp.tools.feedback",
    "_emit_memory_stored_and_enrich": "trellis.mcp.tools.memory",
    "_exception_detail": "trellis.mcp.boundary",
    "_flat_context": "trellis.mcp.tools.context",
    "_fuzzy_duplicate_response": "trellis.mcp.tools.memory",
    "_gather_reconcile_candidate": "trellis.mcp.tools.memory",
    "_get_memory_extractor": "trellis.mcp.wiring",
    "_get_minhash_index": "trellis.mcp.wiring",
    "_get_registry": "trellis.mcp.wiring",
    "_id_list_clause": "trellis.mcp.tools.feedback",
    "_index_stored_memory": "trellis.mcp.tools.memory",
    "_match_readable": "trellis.mcp.tools.memory_common",
    "_memory_exists_response": "trellis.mcp.tools.memory",
    "_memory_extractor_lock": "trellis.mcp.wiring",
    "_memory_extractor_resolution_due": "trellis.mcp.wiring",
    "_note_accepted_call": "trellis.mcp.rejections",
    "_note_noop_call": "trellis.mcp.rejections",
    "_plan_knowledge_supersedes": "trellis.mcp.tools.experience_knowledge",
    "_raise_create_failed": "trellis.mcp.tools.experience_knowledge",
    "_raise_if_supersede_failed": "trellis.mcp.tools.experience_knowledge",
    "_raise_internal": "trellis.mcp.boundary",
    "_raise_invalid_params": "trellis.mcp.boundary",
    "_raise_mutation_failed": "trellis.mcp.boundary",
    "_raise_not_found": "trellis.mcp.boundary",
    "_raise_policy_denied": "trellis.mcp.boundary",
    "_readable_document_exists": "trellis.mcp.tools.memory_common",
    "_readable_ids_if_checked": "trellis.mcp.tools.feedback",
    "_readable_served_ids": "trellis.mcp.tools.feedback",
    "_reconcile_egress_permits": "trellis.mcp.tools.memory",
    "_reconcile_result_message": "trellis.mcp.tools.memory",
    "_reconcile_subject": "trellis.mcp.tools.memory",
    "_record_boundary_rejection": "trellis.mcp.rejections",
    "_refuse_evidence_pointer_keys": "trellis.mcp.tools.mutation",
    "_refuse_label_below_request": "trellis.mcp.tools.memory_common",
    "_refuse_memory_pointer_keys": "trellis.mcp.tools.memory_common",
    "_refuse_near_duplicate_supersedes": "trellis.mcp.tools.memory",
    "_refuse_public_caller": "trellis.mcp.boundary",
    "_render_fetched_item": "trellis.mcp.tools.lookup",
    "_report_memory_extractor_state": "trellis.mcp.wiring",
    "_require_bodied_attribution": "trellis.mcp.tools.feedback",
    "_require_pack_attribution": "trellis.mcp.tools.feedback",
    "_resolve_evidence_pointer": "trellis.mcp.tools.experience_knowledge",
    "_resolve_execute_mutation_target": "trellis.mcp.tools.mutation",
    "_resolve_operation": "trellis.mcp.tools.mutation",
    "_result_message": "trellis.mcp.boundary",
    "_reverify_candidate": "trellis.mcp.tools.memory",
    "_save_memory_lock": "trellis.mcp.tools.memory",
    "_save_memory_reconciled": "trellis.mcp.tools.memory",
    "_sectioned_context": "trellis.mcp.tools.context",
    "_seed_minhash_index": "trellis.mcp.wiring",
    "_session_project": "trellis.mcp.wiring",
    "_stamp_sensitivity": "trellis.mcp.tools.memory_common",
    "_stamp_trace_project": "trellis.mcp.wiring",
    "_store_new_memory": "trellis.mcp.tools.memory",
    "_supersede_saved_memory": "trellis.mcp.tools.memory",
    "_track_tokens": "trellis.mcp.tools.context",
    "_unserved_citation_note": "trellis.mcp.tools.feedback",
    "_warn_index_holds_nothing": "trellis.mcp.wiring",
    "current_clearance": "trellis.mcp.auth",
    "execute_mutation": "trellis.mcp.tools.mutation",
    "get_context": "trellis.mcp.tools.context",
    "get_file_context": "trellis.mcp.tools.lookup",
    "get_graph": "trellis.mcp.tools.lookup",
    "get_items": "trellis.mcp.tools.lookup",
    "get_lessons": "trellis.mcp.tools.lookup",
    "get_objective_context": "trellis.mcp.tools.legacy_context",
    "get_sectioned_context": "trellis.mcp.tools.legacy_context",
    "get_task_context": "trellis.mcp.tools.legacy_context",
    "list_api_keys": "trellis.mcp.tools.admin",
    "main": "trellis.mcp.server",
    "mcp": "trellis.mcp.app",
    "query_observations": "trellis.mcp.tools.observation",
    "record_feedback": "trellis.mcp.tools.feedback",
    "record_observation": "trellis.mcp.tools.observation",
    "revoke_api_key": "trellis.mcp.tools.admin",
    "save_experience": "trellis.mcp.tools.experience_knowledge",
    "save_knowledge": "trellis.mcp.tools.experience_knowledge",
    "save_memory": "trellis.mcp.tools.memory",
    "search": "trellis.mcp.tools.legacy_context",
}

#: Names the pre-split module exposed that the façade no longer does. Module
#: state a function rebinds is read and patched on ``trellis.mcp.wiring``; the
#: two ``_build_llm_client*`` builders are left off because
#: ``test_llm_consumer_rule`` reads a string equal to a builder's name (an
#: ``__all__`` entry) as an escaped reference.
NOT_REEXPORTED = (
    "_build_llm_client",
    "_build_llm_client_from_env",
    "_registry",
    "_minhash_index",
    "_memory_extractor",
    "_memory_extractor_attempted",
    "_memory_extractor_reported",
    "_memory_extractor_reported_at",
    "_memory_extractor_retry_at",
)


@pytest.mark.parametrize(("name", "owner"), sorted(FACADE_SURFACE.items()))
def test_facade_name_is_the_owning_modules_object(name: str, owner: str) -> None:
    owning_module = importlib.import_module(owner)
    assert getattr(server_mod, name) is getattr(owning_module, name)


def test_every_facade_name_is_in_all() -> None:
    assert set(FACADE_SURFACE) <= set(server_mod.__all__)


def test_all_names_nothing_the_surface_does_not() -> None:
    assert sorted(server_mod.__all__) == sorted(FACADE_SURFACE)


def test_facade_surface_has_the_expected_size() -> None:
    assert len(FACADE_SURFACE) == 119


@pytest.mark.parametrize("name", NOT_REEXPORTED)
def test_name_left_off_the_facade_lives_on_wiring(name: str) -> None:
    assert not hasattr(server_mod, name)
    assert hasattr(importlib.import_module("trellis.mcp.wiring"), name)


def test_the_facade_owns_only_its_entry_point_names() -> None:
    owned_here = sorted(
        n for n, o in FACADE_SURFACE.items() if o == server_mod.__name__
    )
    assert owned_here == ["main"]
