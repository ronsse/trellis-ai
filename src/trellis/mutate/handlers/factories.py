"""Handler-map factories: one per registration surface."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from trellis.mutate.commands import Operation
from trellis.mutate.evidence_ingest import EvidenceEmbedMode
from trellis.mutate.handlers.api_keys import ApiKeyMintHandler, ApiKeyRevokeHandler
from trellis.mutate.handlers.classification import (
    ClassificationSetHandler,
    ClassificationTagRegulatoryHandler,
)
from trellis.mutate.handlers.classify_rules import (
    ClassifyRuleApplyHandler,
    ClassifyRuleRevokeHandler,
)
from trellis.mutate.handlers.evidence_handler import EvidenceIngestHandler
from trellis.mutate.handlers.feedback import FeedbackRecordHandler
from trellis.mutate.handlers.graph import (
    AliasUpsertHandler,
    EntityCreateHandler,
    EntityUpdateHandler,
    LinkCreateHandler,
    MeasurementRecordHandler,
    ObservationRecordHandler,
)
from trellis.mutate.handlers.labels import LabelAddHandler, LabelRemoveHandler
from trellis.mutate.handlers.precedent import (
    PrecedentDemoteHandler,
    PrecedentPromoteHandler,
    PrecedentReinstateHandler,
)
from trellis.mutate.handlers.redaction import RedactionApplyHandler
from trellis.mutate.handlers.retention_handlers import (
    RetentionPruneHandler,
    RetentionRestoreHandler,
)
from trellis.mutate.handlers.settings import SettingsUpdateHandler
from trellis.mutate.handlers.trace_ingest import TraceIngestHandler
from trellis.stores.registry import StoreRegistry

if TYPE_CHECKING:
    from trellis.schemas.classification import Sensitivity


def create_curate_handlers(
    registry: StoreRegistry,
    *,
    evidence_embed: EvidenceEmbedMode = "soft",
    clearance: Sensitivity = "restricted",
) -> dict[str, Any]:
    """Create all curate operation handlers for a given registry.

    ``clearance`` is the caller's document clearance; the handlers that decide
    a document's label (``evidence.ingest``, ``classification.set``) refuse
    against it. The default is trusted-local, which clears everything.
    """
    return {
        Operation.EVIDENCE_INGEST: EvidenceIngestHandler(
            registry,
            embed=evidence_embed,
            clearance=clearance,
        ),
        Operation.CLASSIFICATION_SET: ClassificationSetHandler(
            registry, clearance=clearance
        ),
        Operation.CLASSIFICATION_TAG_REGULATORY: ClassificationTagRegulatoryHandler(
            registry, clearance=clearance
        ),
        Operation.TRACE_INGEST: TraceIngestHandler(registry),
        Operation.PRECEDENT_PROMOTE: PrecedentPromoteHandler(registry),
        Operation.PRECEDENT_DEMOTE: PrecedentDemoteHandler(registry),
        Operation.PRECEDENT_REINSTATE: PrecedentReinstateHandler(registry),
        Operation.LABEL_ADD: LabelAddHandler(registry),
        Operation.LABEL_REMOVE: LabelRemoveHandler(registry),
        Operation.FEEDBACK_RECORD: FeedbackRecordHandler(registry),
        Operation.ENTITY_CREATE: EntityCreateHandler(registry),
        Operation.ENTITY_UPDATE: EntityUpdateHandler(registry),
        Operation.ALIAS_UPSERT: AliasUpsertHandler(registry),
        Operation.LINK_CREATE: LinkCreateHandler(registry),
        Operation.OBSERVATION_RECORD: ObservationRecordHandler(registry),
        Operation.MEASUREMENT_RECORD: MeasurementRecordHandler(registry),
        Operation.REDACTION_APPLY: RedactionApplyHandler(registry),
        Operation.RETENTION_PRUNE: RetentionPruneHandler(registry),
        Operation.RETENTION_RESTORE: RetentionRestoreHandler(registry),
    }


def create_api_key_handlers(registry: StoreRegistry) -> dict[str, Any]:
    """Handlers for the admin-only API-key write path.

    Deliberately **not** folded into :func:`create_curate_handlers`, for the
    reason ``create_settings_handlers`` gives, and with more at stake: that
    dict backs REST ``POST /commands/batch`` and MCP ``execute_mutation``, so
    registering ``api_key.mint`` there would let a ``mutate``-scoped agent mint
    a credential. Only :func:`trellis.mutate.build_api_keys_executor` builds an
    executor from this dict.
    """
    return {
        Operation.API_KEY_MINT: ApiKeyMintHandler(registry),
        Operation.API_KEY_REVOKE: ApiKeyRevokeHandler(registry),
    }


def create_settings_handlers(registry: StoreRegistry) -> dict[str, Any]:
    """Handlers for the admin-only settings write path.

    Deliberately **not** folded into :func:`create_curate_handlers`. That
    dict backs every generic mutation surface — REST ``POST
    /commands/batch`` and MCP ``execute_mutation`` — which route any
    ``mutate``-scoped key straight past the admin-only ``GET``/``PUT``
    gate on ``/api/v1/settings``, so registering this handler there would
    let any ``mutate``-scoped key reach a write that is meant to require
    the admin scope. Only :func:`trellis.mutate.build_settings_executor`
    builds an executor
    from this dict, and only the admin REST route and
    ``trellis admin settings`` call that builder — so
    ``Operation.SETTINGS_UPDATE`` is unreachable from the generic surfaces
    by construction, not by a policy that could be misconfigured away.
    Stage 2 still always runs: this flows through
    ``MutationExecutor(..., policy_gate=build_policy_gate(registry), ...)``
    exactly like ``create_curate_handlers``, satisfying
    ``tests/unit/test_policy_gate_rule.py``.
    """
    return {
        Operation.SETTINGS_UPDATE: SettingsUpdateHandler(registry),
    }


def create_classify_handlers(registry: StoreRegistry) -> dict[str, Any]:
    """Handlers for the admin-only classify-overlay write path.

    Deliberately **not** folded into :func:`create_curate_handlers`, for
    the identical reason ``create_settings_handlers`` gives: that dict
    backs the generic mutation surfaces (REST ``POST /commands/batch``,
    MCP ``execute_mutation``), which have no admin-scope gate of their
    own. Only :func:`trellis.mutate.build_classify_rules_executor` builds
    an executor from this dict, and only the admin classify-rules REST
    routes and ``trellis classify apply-rule``/``revoke-rule`` call that
    builder. Stage 2 still always runs here, same as every other executor
    in this module.
    """
    return {
        Operation.CLASSIFY_RULE_APPLY: ClassifyRuleApplyHandler(registry),
        Operation.CLASSIFY_RULE_REVOKE: ClassifyRuleRevokeHandler(registry),
    }
