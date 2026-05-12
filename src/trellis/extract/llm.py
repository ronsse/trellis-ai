"""LLMExtractor — tier=LLM extractor backed by ``LLMClient.generate()``.

Single LLM call produces JSON → :class:`EntityDraft` / :class:`EdgeDraft`
records.  Used for sources where no deterministic rules exist, or as
the residue handler inside a :class:`HybridJSONExtractor`.

Design notes
------------

* **Tolerant JSON parsing.**  The extractor strips markdown code fences
  and tries progressive strategies to recover a JSON object from
  realistic LLM output.  Failures never raise — they surface the raw
  response as :attr:`ExtractionResult.unparsed_residue` with
  ``overall_confidence=0.0`` so the caller can decide what to do.
* **Budget honored.**  ``context.max_llm_calls=0`` short-circuits
  before any network call; ``tokens_used`` / ``llm_calls`` are always
  populated on the result so cost analysis works.
* **``node_role=SEMANTIC`` always.**  LLM output is never structural
  — those nodes come from deterministic sources.  See
  :class:`~trellis.schemas.enums.NodeRole`.
* **Configurable prompt.**  Defaults to
  :data:`~trellis.extract.prompts.ENTITY_EXTRACTION_V1`; callers can
  pass any ``PromptTemplate`` that produces matching JSON.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

import structlog
from pydantic import ValidationError

from trellis.core.hashing import content_hash
from trellis.extract.base import ExtractorTier
from trellis.extract.prompts import ENTITY_EXTRACTION_V1, PromptTemplate, render
from trellis.extract.telemetry import (
    ExtractionFailureError,
    emit_extraction_failure,
)
from trellis.schemas.enums import NodeRole
from trellis.schemas.extraction import (
    EdgeDraft,
    EntityDraft,
    ExtractionProvenance,
    ExtractionResult,
)
from trellis.schemas.well_known import (
    canonicalize_edge_kind,
    canonicalize_entity_type,
    schema_alignment_for_edge_kind,
    schema_alignment_for_entity_type,
)

if TYPE_CHECKING:
    from trellis.extract.context import ExtractionContext
    from trellis.llm.protocol import LLMClient
    from trellis.stores.base.event_log import EventLog

logger = structlog.get_logger(__name__)


# Matches a leading ```json or ``` line and a trailing ``` line.
_CODE_FENCE_RE = re.compile(r"(^\s*```(?:json)?\s*\n)|(\n\s*```\s*$)", re.MULTILINE)


class LLMExtractor:
    """Tier=LLM extractor. One ``LLMClient.generate()`` call per extract.

    Safe to share across concurrent ``extract()`` calls — holds only
    immutable configuration.
    """

    tier = ExtractorTier.LLM

    def __init__(
        self,
        name: str = "llm_extractor",
        *,
        llm_client: LLMClient,
        prompt: PromptTemplate | None = None,
        entity_type_hints: list[str] | None = None,
        edge_kind_hints: list[str] | None = None,
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 2000,
        supported_sources: list[str] | None = None,
        version: str = "0.1.0",
        event_log: EventLog | None = None,
    ) -> None:
        self.name = name
        self._llm = llm_client
        self._prompt = prompt or ENTITY_EXTRACTION_V1
        self._entity_type_hints = list(entity_type_hints) if entity_type_hints else None
        self._edge_kind_hints = list(edge_kind_hints) if edge_kind_hints else None
        self._model = model
        self._temperature = temperature
        self._max_tokens = max_tokens
        self.supported_sources = list(supported_sources or [])
        self.version = version
        # ADR-extraction-failure-telemetry: when wired, parse/validation
        # failures emit EXTRACTION_FAILED via the telemetry helper and
        # then re-raise. The dispatcher is the one legitimate degrader.
        self._event_log = event_log

    async def extract(
        self,
        raw_input: Any,
        *,
        source_hint: str | None = None,
        context: ExtractionContext | None = None,
    ) -> ExtractionResult:
        text, _doc_id = _parse_input(raw_input)

        # Budget short-circuit: max_llm_calls=0 means "no LLM this pass".
        if context is not None and context.max_llm_calls == 0:
            logger.debug(
                "llm_extractor_budget_zero",
                extractor=self.name,
                source_hint=source_hint,
            )
            return _make_result(
                name=self.name,
                version=self.version,
                tier=self.tier,
                entities=[],
                edges=[],
                llm_calls=0,
                tokens_used=0,
                overall_confidence=0.0,
                unparsed_residue=text or None,
                source_hint=source_hint,
            )

        messages = render(
            self._prompt,
            text=text,
            entity_type_hints=self._entity_type_hints,
            edge_kind_hints=self._edge_kind_hints,
            domain=context.domain if context else None,
            source_system=context.source_system if context else None,
        )

        response = await self._llm.generate(
            messages=messages,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
            model=self._model,
        )
        tokens = response.usage.total_tokens if response.usage else 0

        # Prompt + source hashes for telemetry clustering. Both are
        # short SHA-256 digests; they let the analyzer group failures by
        # (extractor, prompt template, input shape) without storing raw
        # content. Computed lazily — we only pay the hash cost when the
        # parse fails below.
        parsed, parse_exc = _parse_json_with_exception(response.content)
        if parsed is None:
            prompt_hash = _prompt_hash(messages)
            source_excerpt_hash = content_hash(text) if text else None
            emit_extraction_failure(
                event_log=self._event_log,
                extractor_id=self.__class__.__name__,
                extractor_tier="llm",
                failure_kind="parse_error",
                source_hint=source_hint,
                prompt_hash=prompt_hash,
                source_excerpt_hash=source_excerpt_hash,
                model=response.model or self._model,
                error_class=(
                    type(parse_exc).__name__
                    if parse_exc is not None
                    else "JSONDecodeError"
                ),
                error_excerpt=(
                    str(parse_exc)
                    if parse_exc is not None
                    else f"unparseable response (len={len(response.content)})"
                ),
            )
            logger.info(
                "llm_extractor_parse_failed",
                extractor=self.name,
                response_len=len(response.content),
            )
            # POC directive: silent fallbacks are forbidden. The
            # dispatcher catches this and degrades explicitly with a
            # ``tier_fallback`` event; callers that want degradation
            # must do the same.
            msg = (
                "LLMExtractor: failed to parse JSON response "
                f"(len={len(response.content)})"
            )
            raise ExtractionFailureError(
                msg,
                failure_kind="parse_error",
                extractor_id=self.__class__.__name__,
            )

        try:
            entities, edges, confidence = _to_drafts(parsed)
        except ValidationError as exc:
            prompt_hash = _prompt_hash(messages)
            source_excerpt_hash = content_hash(text) if text else None
            emit_extraction_failure(
                event_log=self._event_log,
                extractor_id=self.__class__.__name__,
                extractor_tier="llm",
                failure_kind="validation_error",
                source_hint=source_hint,
                prompt_hash=prompt_hash,
                source_excerpt_hash=source_excerpt_hash,
                model=response.model or self._model,
                error_class=type(exc).__name__,
                error_excerpt=str(exc),
            )
            logger.info(
                "llm_extractor_validation_failed",
                extractor=self.name,
                error=str(exc)[:200],
            )
            msg = f"LLMExtractor: drafts failed Pydantic validation: {exc}"
            raise ExtractionFailureError(
                msg,
                failure_kind="validation_error",
                extractor_id=self.__class__.__name__,
            ) from exc

        return _make_result(
            name=self.name,
            version=self.version,
            tier=self.tier,
            entities=entities,
            edges=edges,
            llm_calls=1,
            tokens_used=tokens,
            overall_confidence=confidence,
            unparsed_residue=None if (entities or edges) else (text or None),
            source_hint=source_hint,
        )


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _parse_input(raw_input: Any) -> tuple[str, str | None]:
    """Normalize ``raw_input`` to ``(text, doc_id)``.

    Shares input conventions with :class:`AliasMatchExtractor` so the
    two can compose cleanly inside ``HybridJSONExtractor``.
    """
    if isinstance(raw_input, str):
        return raw_input, None
    if isinstance(raw_input, dict):
        text_value = raw_input.get("text", "")
        if not isinstance(text_value, str):
            msg = (
                "LLMExtractor: dict input requires a string 'text' field; "
                f"got {type(text_value).__name__}"
            )
            raise TypeError(msg)
        doc_id_value = raw_input.get("doc_id")
        if doc_id_value is not None and not isinstance(doc_id_value, str):
            msg = (
                "LLMExtractor: 'doc_id' must be a string or None; "
                f"got {type(doc_id_value).__name__}"
            )
            raise TypeError(msg)
        return text_value, doc_id_value
    msg = f"LLMExtractor expects a str or dict; got {type(raw_input).__name__}"
    raise TypeError(msg)


def _parse_json_tolerant(content: str) -> dict[str, Any] | None:
    """Recover a JSON object from realistic LLM output.

    Strategy:
      1. Strip leading/trailing markdown code fences (``` or ```json).
      2. Try to parse the whole stripped string.
      3. Fall back to parsing from the first ``{`` to the last ``}``.

    A bare JSON array is lifted into ``{"entities": [...], "edges": []}``
    since some prompts may coax that shape out of stubborn models.
    Returns ``None`` when nothing parseable is found.
    """
    parsed, _ = _parse_json_with_exception(content)
    return parsed


def _parse_json_with_exception(
    content: str,
) -> tuple[dict[str, Any] | None, Exception | None]:
    """Same as :func:`_parse_json_tolerant` but also returns the last
    exception encountered.

    The exception lets the telemetry helper record a meaningful
    ``error_class`` / ``error_excerpt`` instead of a generic message.
    Returns ``(None, None)`` when input is empty / blank.
    """
    if not content or not content.strip():
        return None, None

    stripped = _CODE_FENCE_RE.sub("", content).strip()
    if not stripped:
        return None, None

    data, last_exc = _try_json_loads_with_exc(stripped)
    if data is None:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start != -1 and end > start:
            data, last_exc = _try_json_loads_with_exc(stripped[start : end + 1])

    if isinstance(data, dict):
        return data, None
    if isinstance(data, list):
        return {"entities": data, "edges": []}, None
    return None, last_exc


def _try_json_loads(text: str) -> Any | None:
    """Parse ``text`` as JSON; return ``None`` on failure."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _try_json_loads_with_exc(text: str) -> tuple[Any | None, Exception | None]:
    """Parse ``text`` as JSON; return ``(value, None)`` on success or
    ``(None, exc)`` on failure."""
    try:
        return json.loads(text), None
    except json.JSONDecodeError as exc:
        return None, exc


def _prompt_hash(messages: list[Any]) -> str:
    """Return a short SHA-256 digest of the rendered prompt.

    Used as the clustering key for ``EXTRACTION_FAILED`` events so the
    analyzer can group "all parse errors from the same prompt template
    + injected vars" together. We hash ``role|content`` joined with
    newlines so the digest is stable across runs but sensitive to any
    prompt drift.
    """
    parts: list[str] = []
    for msg in messages:
        role = getattr(msg, "role", "")
        content = getattr(msg, "content", "")
        parts.append(f"{role}|{content}")
    return content_hash("\n".join(parts))


def _to_drafts(
    parsed: dict[str, Any],
) -> tuple[list[EntityDraft], list[EdgeDraft], float]:
    """Convert parsed JSON into drafts + compute an average confidence.

    Malformed entries are skipped individually rather than failing the
    whole result — an LLM that produces 5 good entities and 1 bad one
    should not have its entire output discarded.
    """
    entities: list[EntityDraft] = []
    edges: list[EdgeDraft] = []
    confidences: list[float] = []

    for raw in _iter_records(parsed, "entities"):
        ent_draft = _entity_draft_from_raw(raw)
        if ent_draft is None:
            continue
        entities.append(ent_draft)
        confidences.append(ent_draft.confidence)

    for raw in _iter_records(parsed, "edges"):
        edge_draft = _edge_draft_from_raw(raw)
        if edge_draft is None:
            continue
        edges.append(edge_draft)
        confidences.append(edge_draft.confidence)

    avg = sum(confidences) / len(confidences) if confidences else 0.0
    return entities, edges, avg


def _iter_records(parsed: dict[str, Any], key: str) -> list[dict[str, Any]]:
    raw = parsed.get(key, [])
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _entity_draft_from_raw(raw: dict[str, Any]) -> EntityDraft | None:
    name = raw.get("name")
    entity_type = raw.get("entity_type")
    if not isinstance(name, str) or not name.strip():
        return None
    if not isinstance(entity_type, str) or not entity_type.strip():
        return None

    entity_id: str | None
    entity_id_raw = raw.get("entity_id")
    if isinstance(entity_id_raw, str) and entity_id_raw.strip():
        entity_id = entity_id_raw
    else:
        entity_id = None

    properties = raw.get("properties")
    if not isinstance(properties, dict):
        properties = {}

    confidence = _clamp_confidence(raw.get("confidence"))

    # ADR Phase 1: collapse legacy spellings the LLM may emit ("person",
    # "system") onto canonical PascalCase, and stamp the schema_alignment
    # URI when one exists. Open-string types pass through untouched so
    # domain-specific extractors aren't rewritten silently.
    canonical_type = canonicalize_entity_type(entity_type)
    alignment = schema_alignment_for_entity_type(canonical_type)
    if alignment is not None:
        properties = {**properties, "schema_alignment": alignment}

    return EntityDraft(
        entity_id=entity_id,
        entity_type=canonical_type,
        name=name,
        properties=properties,
        node_role=NodeRole.SEMANTIC,
        confidence=confidence,
    )


def _edge_draft_from_raw(raw: dict[str, Any]) -> EdgeDraft | None:
    source_id = raw.get("source_id")
    target_id = raw.get("target_id")
    edge_kind = raw.get("edge_kind")
    fields = (source_id, target_id, edge_kind)
    if not all(isinstance(x, str) and x.strip() for x in fields):
        return None

    confidence = _clamp_confidence(raw.get("confidence"))

    canonical_kind = canonicalize_edge_kind(edge_kind)  # type: ignore[arg-type]
    alignment = schema_alignment_for_edge_kind(canonical_kind)
    properties: dict[str, Any] = (
        {"schema_alignment": alignment} if alignment is not None else {}
    )

    return EdgeDraft(
        source_id=source_id,  # type: ignore[arg-type]
        target_id=target_id,  # type: ignore[arg-type]
        edge_kind=canonical_kind,
        properties=properties,
        confidence=confidence,
    )


def _clamp_confidence(value: Any, default: float = 0.5) -> float:
    """Coerce an arbitrary value to a float in ``[0.0, 1.0]``."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number < 0.0:
        return 0.0
    if number > 1.0:
        return 1.0
    return number


def _make_result(
    *,
    name: str,
    version: str,
    tier: ExtractorTier,
    entities: list[EntityDraft],
    edges: list[EdgeDraft],
    llm_calls: int,
    tokens_used: int,
    overall_confidence: float,
    unparsed_residue: Any | None,
    source_hint: str | None,
) -> ExtractionResult:
    return ExtractionResult(
        entities=entities,
        edges=edges,
        extractor_used=name,
        tier=tier.value,
        llm_calls=llm_calls,
        tokens_used=tokens_used,
        overall_confidence=overall_confidence,
        unparsed_residue=unparsed_residue,
        provenance=ExtractionProvenance(
            extractor_name=name,
            extractor_version=version,
            source_hint=source_hint,
        ),
    )
