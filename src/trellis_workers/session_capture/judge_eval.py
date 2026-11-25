"""Offline eval of the session-capture judge: several arms, same transcripts.

``trellis-session-capture eval-judge`` runs each transcript in a directory
through the production capture path, up to and including the candidate gates
(secret scan, injection guard, worthiness), once per **arm**: a judge model,
its endpoint, its key's environment variable, a salient-window size, a
declared context window and a prompt variant. It stops before any store
write: no ``MutationExecutor``, no document, no event, no vector, no
watermark. What it writes is JSONL rows and per-arm summaries in an output
directory the caller names, which must not sit inside a git work tree,
because transcripts and the memories distilled from them are private.

The production functions are reused, not copied:
:func:`~trellis_workers.session_capture.transcripts.parse_session`,
:func:`~trellis_workers.session_capture.distill.distill_session` (which builds
the prompt with
:func:`~trellis_workers.session_capture.distill.build_distill_messages` and
applies the context-window pre-flight) and
:func:`~trellis_workers.session_capture.capture.gate_candidate`, the function
the sweep's own ``_gate_candidates`` calls.

**Two places this differs from the sweep, on purpose.** Every non-empty
transcript is judged: the sweep's clean-session sampling is a cost decision,
not a quality one, and an eval that sampled would compare arms on different
transcripts. And by default (the ``strict`` egress rule) no store is opened, so
a transcript that names any pack is treated as having been served
``restricted`` content and is kept off an arm whose route is not declared
``local: true`` (recorded as ``egress_withheld``, never sent). With
``--egress-labels`` (the ``labels`` rule) the sweep's own check,
:func:`~trellis_workers.session_capture.capture._session_egress_verdict`, reads
the labels of the documents each pack served, through a registry opened for
reads only.

The sweep's skip of transcripts from throwaway project directories
(:func:`~trellis_workers.session_capture.transcripts.is_ephemeral_project`)
applies here too, unless ``--include-ephemeral`` is passed.

The deterministic checks it writes are measurements, not verdicts. In
particular ``evidence_grounded`` says the candidate's ``evidence`` string
appears verbatim (whitespace-normalised) in the parsed conversation; it is a
fabrication detector, not a usefulness score. A grounded memory can be
useless, and a paraphrased evidence line from a good memory reads as
ungrounded. Evidence shorter than :data:`MIN_EVIDENCE_CHARS` is reported as
``evidence_too_short``, neither grounded nor ungrounded.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
import yaml
from pydantic import Field, ValidationError, field_validator, model_validator

from trellis.core.base import TrellisModel
from trellis.core.path_presence import path_is_present
from trellis.errors import BackendNotInstalledError
from trellis.llm.egress import EgressGate, EgressVerdict, may_reach_route
from trellis.llm.routing import LLMConsumer, LLMRoutingError
from trellis.stores.registry import StoreRegistry
from trellis_workers.session_capture import distill
from trellis_workers.session_capture.capture import (
    GATE_SECRET_SCAN,
    _session_egress_verdict,
    gate_candidate,
)
from trellis_workers.session_capture.secret_scan import scan as scan_for_leak_classes
from trellis_workers.session_capture.transcripts import (
    discover_sessions,
    is_ephemeral_project,
    parse_session,
)

if TYPE_CHECKING:
    from trellis.llm import LLMClient
    from trellis.schemas.classification import Sensitivity
    from trellis_workers.session_capture.models import SessionDigest

logger = structlog.get_logger(__name__)

#: Output file names inside the caller's output directory.
CALLS_FILE = "calls.jsonl"
CANDIDATES_FILE = "candidates.jsonl"

#: Normalised-token Jaccard at or above which a candidate matches a gold row
#: when evidence quotes do not settle it.
JACCARD_THRESHOLD = 0.5

#: Shortest whitespace-normalised evidence, in characters, that can read as
#: grounded or settle a gold match by containment. A quote of a word or two
#: appears in almost any conversation and in almost any gold evidence line.
MIN_EVIDENCE_CHARS = 20

#: Values of a candidate row's ``evidence_check``.
EVIDENCE_GROUNDED = "grounded"
EVIDENCE_UNGROUNDED = "ungrounded"
EVIDENCE_TOO_SHORT = "evidence_too_short"
EVIDENCE_NONE = "no_evidence"

#: Call outcomes beyond :class:`~.distill.DistillOutcome`.
OUTCOME_SKIPPED_EMPTY = "skipped_empty"
OUTCOME_SKIPPED_EPHEMERAL = "skipped_ephemeral"
OUTCOME_EGRESS_WITHHELD = "egress_withheld"

#: The outcomes that mean the judge replied. Every other call (withheld,
#: skipped, timed out, failed, too large) judged nothing, so a gold row of its
#: transcript is not a miss.
_COMPLETED_OUTCOMES = frozenset(
    {
        distill.DistillOutcome.CANDIDATES.value,
        distill.DistillOutcome.EMPTY.value,
        distill.DistillOutcome.MALFORMED.value,
    }
)

#: ``egress_rule`` values on a call row.
EGRESS_RULE_STRICT = "strict"
EGRESS_RULE_LABELS = "labels"

#: ``egress_reason`` values on a withheld call row.
EGRESS_REASON_PACK_NAMED = "pack_named"
EGRESS_REASON_LABEL = "label_withheld"
EGRESS_REASON_UNREADABLE = "unreadable"

#: The arm fields that decide what a call asks and of whom; the key never is.
_FINGERPRINT_FIELDS = (
    "model",
    "base_url",
    "local",
    "salient_chars",
    "context_tokens",
    "prompt_variant",
)

#: The label assumed for what a transcript's packs served, which this command
#: cannot read without a store: the strictest, so nothing unread is sent off
#: a route that is not local.
_UNREAD_SERVED_LABEL: Sensitivity = "restricted"

_ARM_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_NON_WORD_RE = re.compile(r"[^\w]+")


class EvalConfigError(ValueError):
    """The arm file, gold file or output directory cannot be used."""


class JudgeArm(TrellisModel):
    """One judge configuration to evaluate."""

    name: str
    model: str
    base_url: str
    #: Name of the environment variable holding the endpoint's key. The key
    #: itself is read from the environment by the client builder only.
    api_key_env: str
    salient_chars: int = Field(default=distill.DEFAULT_MAX_SALIENT_CHARS, gt=0)
    context_tokens: int = Field(default=distill.DEFAULT_JUDGE_CONTEXT_TOKENS, gt=0)
    prompt_variant: str = distill.DEFAULT_PROMPT_VARIANT
    #: The operator's declaration that this endpoint keeps data on the host,
    #: exactly as ``llm.local`` in ``config.yaml``; never inferred.
    local: bool = False

    @field_validator("name")
    @classmethod
    def _name_is_a_file_name(cls, value: str) -> str:
        if not _ARM_NAME_RE.match(value):
            msg = "arm name must be 1-64 chars of letters, digits, '.', '_', '-'"
            raise ValueError(msg)
        return value

    @field_validator("prompt_variant")
    @classmethod
    def _variant_exists(cls, value: str) -> str:
        if value not in distill.PROMPT_VARIANTS:
            known = ", ".join(sorted(distill.PROMPT_VARIANTS))
            msg = f"unknown prompt_variant {value!r}; known: {known}"
            raise ValueError(msg)
        return value


class ArmFile(TrellisModel):
    """The arm file: ``{"arms": [...]}`` in YAML or JSON."""

    arms: list[JudgeArm] = Field(min_length=1)

    @model_validator(mode="after")
    def _names_unique(self) -> ArmFile:
        names = [arm.name for arm in self.arms]
        if len(set(names)) != len(names):
            msg = "arm names must be unique"
            raise ValueError(msg)
        return self


class GoldMemory(TrellisModel):
    """One reference memory a human expects for a transcript."""

    transcript_id: str
    text: str
    evidence: str = ""


# ---------------------------------------------------------------------------
# Loading and refusing
# ---------------------------------------------------------------------------


def load_arms(path: Path) -> list[JudgeArm]:
    """Read and validate an arm file (``.yaml``/``.yml`` or JSON)."""
    try:
        raw_text = path.read_text(encoding="utf-8")
        raw = (
            yaml.safe_load(raw_text)
            if path.suffix.lower() in {".yaml", ".yml"}
            else json.loads(raw_text)
        )
        return ArmFile.model_validate(raw).arms
    except (OSError, ValueError, yaml.YAMLError) as exc:
        # ValidationError and JSONDecodeError are ValueErrors.
        msg = f"arm file {path} is not usable: {exc}"
        raise EvalConfigError(msg) from exc


def load_gold(path: Path) -> dict[str, list[GoldMemory]]:
    """Read a gold JSONL file into reference memories per transcript id."""
    gold: dict[str, list[GoldMemory]] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        msg = f"gold file {path} is not readable: {exc}"
        raise EvalConfigError(msg) from exc
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = GoldMemory.model_validate(json.loads(line))
        except (ValueError, ValidationError) as exc:
            msg = f"gold file {path} line {number} is not a gold row: {exc}"
            raise EvalConfigError(msg) from exc
        gold.setdefault(row.transcript_id, []).append(row)
    return gold


def inside_git_work_tree(path: Path) -> bool:
    """Whether *path* (resolved) is in, or is, a git work tree."""
    resolved = path.resolve()
    return any(path_is_present(p / ".git") for p in (resolved, *resolved.parents))


# ---------------------------------------------------------------------------
# Deterministic text checks
# ---------------------------------------------------------------------------


def normalise_whitespace(text: str) -> str:
    """Collapse every whitespace run to one space."""
    return " ".join(text.split())


def normalise_text(text: str) -> str:
    """Casefold and reduce to word tokens separated by single spaces."""
    return " ".join(_NON_WORD_RE.sub(" ", text.casefold()).split())


def token_jaccard(a: str, b: str) -> float:
    """Jaccard similarity of the normalised token sets of *a* and *b*."""
    left = set(normalise_text(a).split())
    right = set(normalise_text(b).split())
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def evidence_check(quote: str, text: str) -> str:
    """Whether *quote* appears in *text*, whitespace-normalised.

    One of :data:`EVIDENCE_GROUNDED`, :data:`EVIDENCE_UNGROUNDED`,
    :data:`EVIDENCE_TOO_SHORT` (under :data:`MIN_EVIDENCE_CHARS`, which says
    nothing either way) or :data:`EVIDENCE_NONE`.
    """
    needle = normalise_whitespace(quote)
    if not needle:
        return EVIDENCE_NONE
    if len(needle) < MIN_EVIDENCE_CHARS:
        return EVIDENCE_TOO_SHORT
    if needle in normalise_whitespace(text):
        return EVIDENCE_GROUNDED
    return EVIDENCE_UNGROUNDED


def quote_in(quote: str, text: str) -> bool | None:
    """:func:`evidence_check` as a boolean; ``None`` for empty or too-short evidence."""
    check = evidence_check(quote, text)
    if check == EVIDENCE_GROUNDED:
        return True
    if check == EVIDENCE_UNGROUNDED:
        return False
    return None


def arm_fingerprint(arm: JudgeArm) -> str:
    """A digest of the arm fields a call depends on, never its key's variable."""
    fields = {name: getattr(arm, name) for name in _FINGERPRINT_FIELDS}
    blob = json.dumps(fields, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Clients and routes, built the way the sweep builds them
# ---------------------------------------------------------------------------


def _arm_registry(arm: JudgeArm) -> StoreRegistry:
    """A store-less registry whose ``llm:`` block is this arm."""
    return StoreRegistry(
        llm_config={
            "provider": "openai",
            "model": arm.model,
            "base_url": arm.base_url,
            "api_key_env": arm.api_key_env,
            "local": arm.local,
        }
    )


def build_arm_client(arm: JudgeArm) -> LLMClient | None:
    """The arm's judge client, built by the registry's own provider code.

    ``None`` when it cannot be built, typically because ``api_key_env`` is
    unset. An OpenAI-compatible endpoint that needs no key still needs the
    variable set to some value.
    """
    return _arm_registry(arm).build_llm_client(consumer=LLMConsumer.SESSION_CAPTURE)


def arm_route_is_local(arm: JudgeArm) -> bool:
    """Whether the arm's route is declared local, read by :class:`EgressGate`.

    An unresolvable route reads as not local (``EgressGate.for_route``).
    """
    return EgressGate.for_route(
        _arm_registry(arm), LLMConsumer.SESSION_CAPTURE, None
    ).local


def _eval_egress_permits(
    digest: SessionDigest,
    arm: JudgeArm,
    *,
    local: bool,
    stores: StoreRegistry | None,
) -> str | None:
    """``None`` if this transcript may go to the arm's route, else the reason.

    With no *stores* (the ``strict`` rule): a transcript that named no pack
    carries no served label, which is how the sweep reads it too. One that
    named a pack (or a pack line that did not parse) was served documents
    whose labels only a store can answer, so it is judged as if they were the
    strictest label.

    With *stores* (the ``labels`` rule): the sweep's own
    :func:`_session_egress_verdict` over a gate built the way the sweep builds
    it, from the arm's route and the stores' document store. It fails closed:
    anything but ``PERMITTED`` keeps the transcript off the route.
    """
    if stores is None:
        if not digest.pack_ids and not digest.pack_ids_unparsed:
            return None
        if may_reach_route(_UNREAD_SERVED_LABEL, local=local):
            return None
        return EGRESS_REASON_PACK_NAMED
    return _labels_withholding_reason(digest, arm, stores)


def _labels_withholding_reason(
    digest: SessionDigest, arm: JudgeArm, stores: StoreRegistry
) -> str | None:
    """The ``labels`` rule: the sweep's own check, failing closed."""
    try:
        gate = EgressGate.for_route(
            _arm_registry(arm),
            LLMConsumer.SESSION_CAPTURE,
            stores.knowledge.document_store,
        )
        verdict = _session_egress_verdict(stores, digest, gate).verdict
    except Exception:
        # GRACEFUL-DEGRADATION: fail closed, as the sweep does. A store that
        # could not even be opened is no permission to send the transcript.
        logger.warning("judge_eval_egress_read_failed", arm=arm.name, exc_info=True)
        return EGRESS_REASON_UNREADABLE
    if verdict is EgressVerdict.PERMITTED:
        return None
    if verdict is EgressVerdict.WITHHELD:
        return EGRESS_REASON_LABEL
    return EGRESS_REASON_UNREADABLE


def _eval_field_scan(candidate: Any) -> list[str]:
    """Secret classes in the model-authored fields the production scan omits.

    Eval only: production scans the rendered memory alone, which carries
    ``signal`` but not ``memory_type``; both are scanned here.
    """
    hits: set[str] = set()
    for value in (candidate.memory_type, candidate.signal):
        hits.update(scan_for_leak_classes(str(value)))
    return sorted(hits)


# ---------------------------------------------------------------------------
# One transcript, one arm
# ---------------------------------------------------------------------------


def judge_transcript(
    digest: SessionDigest,
    transcript_id: str,
    arm: JudgeArm,
    client: LLMClient | None,
    *,
    local: bool,
    timeout: float,
    stores: StoreRegistry | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Run one arm's judge and the production gates on one transcript.

    Returns the call row and the candidate rows, which share one
    ``attempt_id``. Nothing is written anywhere. *stores* selects the
    ``labels`` egress rule; ``None`` is the ``strict`` rule.
    """
    call = _call_row(transcript_id, arm)
    call["egress_rule"] = EGRESS_RULE_STRICT if stores is None else EGRESS_RULE_LABELS
    if digest.is_empty:
        call["outcome"] = OUTCOME_SKIPPED_EMPTY
        return call, []
    withheld = _eval_egress_permits(digest, arm, local=local, stores=stores)
    if withheld is not None:
        call["outcome"] = OUTCOME_EGRESS_WITHHELD
        call["egress_reason"] = withheld
        return call, []

    system_prompt = distill.PROMPT_VARIANTS[arm.prompt_variant]
    started = time.perf_counter()
    result = distill.distill_session(
        client,
        digest,
        timeout=timeout,
        salient_chars=arm.salient_chars,
        context_tokens=arm.context_tokens,
        system_prompt=system_prompt,
    )
    elapsed = time.perf_counter() - started
    call["outcome"] = result.outcome.value
    call["unavailable_reason"] = result.unavailable_reason
    call["parse_error"] = result.parse_error
    if result.unavailable_reason != "prompt_too_large":
        call["latency_s"] = round(elapsed, 4)
    if result.usage is not None:
        call["prompt_tokens"] = result.usage.prompt_tokens
        call["completion_tokens"] = result.usage.completion_tokens

    # The window the judge was shown: the user message of the same prompt.
    shown = distill.build_distill_messages(
        digest, salient_chars=arm.salient_chars, system_prompt=system_prompt
    )[-1].content
    conversation = digest.salient_text
    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    for index, candidate in enumerate(result.candidates):
        decision = gate_candidate(candidate)
        fingerprint = normalise_text(f"{candidate.title} {candidate.memory}")
        duplicate = fingerprint in seen
        seen.add(fingerprint)
        # The production gate scans the rendered memory; the eval also scans
        # the two model-authored fields that render leaves out.
        field_classes = _eval_field_scan(candidate)
        redacted = decision.gate == GATE_SECRET_SCAN or bool(field_classes)
        check = evidence_check(candidate.evidence, conversation)
        rows.append(
            {
                "transcript_id": transcript_id,
                "arm": arm.name,
                "attempt_id": call["attempt_id"],
                "candidate_index": index,
                # A candidate a secret scan blocked is never written out:
                # production never stores it, and neither does this file.
                # Every model-authored string is nulled, not just the rendered
                # ones.
                "redacted": redacted,
                "title": None if redacted else candidate.title,
                "memory": None if redacted else candidate.memory,
                "evidence": None if redacted else candidate.evidence,
                "memory_type": None if redacted else candidate.memory_type,
                "signal": None if redacted else candidate.signal,
                "non_derivable": candidate.non_derivable,
                "durable": candidate.durable,
                "actionable": candidate.actionable,
                "confidence": candidate.confidence,
                "gate": decision.gate,
                "accepted": decision.accepted,
                "worthiness": (
                    None if decision.worthiness is None else decision.worthiness.value
                ),
                "matched_classes": list(decision.matched_classes),
                "eval_field_scan_classes": field_classes,
                "evidence_check": check,
                "evidence_grounded": quote_in(candidate.evidence, conversation),
                "evidence_in_window": quote_in(candidate.evidence, shown),
                "within_arm_duplicate": duplicate,
            }
        )
    call["candidates"] = len(rows)
    return call, rows


def _call_row(transcript_id: str, arm: JudgeArm) -> dict[str, Any]:
    """A call row with every field present and a fresh ``attempt_id``."""
    return {
        "transcript_id": transcript_id,
        "arm": arm.name,
        "attempt_id": uuid.uuid4().hex,
        "arm_fingerprint": arm_fingerprint(arm),
        "model": arm.model,
        "outcome": None,
        "unavailable_reason": None,
        "parse_error": None,
        "egress_rule": None,
        "egress_reason": None,
        "latency_s": None,
        "prompt_tokens": None,
        "completion_tokens": None,
        "candidates": 0,
    }


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path_is_present(path):
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            # A line torn by an interrupted run; the call it belonged to has
            # no call row yet, so it is re-run.
            logger.warning("judge_eval_torn_row_skipped", file=path.name)
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _open_private(path: Path, flags: int) -> int:
    """``os.open``, creating *path* 0600; an existing file keeps its mode."""
    return os.open(path, flags | os.O_CREAT, 0o600)


def _write_private(path: Path, text: str) -> None:
    """Replace *path*'s content with *text*, creating it 0600."""
    fd = _open_private(path, os.O_WRONLY | os.O_TRUNC)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


def _ends_torn(path: Path) -> bool:
    """Whether *path* is non-empty and its last byte is not a newline."""
    if not path_is_present(path) or path.stat().st_size == 0:
        return False
    with path.open("rb") as handle:
        handle.seek(-1, os.SEEK_END)
        return handle.read(1) != b"\n"


def _append_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    # A line torn by an interrupted run must not swallow the next row.
    torn = _ends_torn(path)
    fd = _open_private(path, os.O_WRONLY | os.O_APPEND)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        if torn:
            handle.write("\n")
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
        handle.flush()


def _key(row: Mapping[str, Any]) -> tuple[str, str]:
    return str(row.get("transcript_id")), str(row.get("arm"))


def _build_client(
    build: Callable[[JudgeArm], LLMClient | None], arm: JudgeArm
) -> LLMClient:
    """The arm's client, or an :class:`EvalConfigError` naming what is missing."""
    try:
        client = build(arm)
    except (BackendNotInstalledError, LLMRoutingError) as exc:
        msg = f"arm {arm.name!r}: no judge client could be built: {exc}"
        raise EvalConfigError(msg) from exc
    if client is None:
        msg = (
            f"arm {arm.name!r}: no judge client could be built; is the "
            f"environment variable {arm.api_key_env} set?"
        )
        raise EvalConfigError(msg)
    return client


def _refuse_changed_arms(
    recorded: Iterable[Mapping[str, Any]], arms: list[JudgeArm]
) -> None:
    """Refuse to resume onto call rows made under a different arm config."""
    current = {arm.name: arm_fingerprint(arm) for arm in arms}
    changed = sorted(
        {
            str(row.get("arm"))
            for row in recorded
            if str(row.get("arm")) in current
            and row.get("arm_fingerprint") != current[str(row.get("arm"))]
        }
    )
    if changed:
        names = ", ".join(repr(name) for name in changed)
        msg = (
            f"arm {names} was recorded in this output directory with a different "
            "config (model, base_url, local, salient_chars, context_tokens or "
            "prompt_variant); use a new arm name or a new output directory"
        )
        raise EvalConfigError(msg)


def run_eval(
    transcripts_dir: Path,
    arms: list[JudgeArm],
    out_dir: Path,
    *,
    timeout: float = distill.DEFAULT_TIMEOUT_S,
    max_transcripts: int | None = None,
    gold: Mapping[str, list[GoldMemory]] | None = None,
    client_factory: Callable[[JudgeArm], LLMClient | None] | None = None,
    local_of: Callable[[JudgeArm], bool] | None = None,
    include_ephemeral: bool = False,
    stores: StoreRegistry | None = None,
) -> dict[str, Any]:
    """Judge every transcript with every arm and write rows and summaries.

    Resumable: a ``(transcript, arm)`` pair that already has a row in
    ``calls.jsonl`` is skipped, failures included (delete the row to retry
    it). A failed or timed-out judge call is recorded and the run goes on.
    *stores*, opened by the caller for reads only, selects the ``labels``
    egress rule; ``None`` keeps the ``strict`` one.

    Raises:
        EvalConfigError: the transcript directory is missing, the output
            directory is inside a git work tree, an arm's client cannot be
            built, or a recorded call was made with a different arm config.
    """
    build = build_arm_client if client_factory is None else client_factory
    is_local = arm_route_is_local if local_of is None else local_of
    if not transcripts_dir.is_dir():
        msg = f"transcript directory {transcripts_dir} does not exist"
        raise EvalConfigError(msg)
    if inside_git_work_tree(out_dir):
        msg = (
            f"refusing output directory {out_dir}: it is inside a git work tree, "
            "and transcripts and distilled memories are private data"
        )
        raise EvalConfigError(msg)
    if not out_dir.exists():
        out_dir.mkdir(parents=True)
        out_dir.chmod(0o700)
    calls_path = out_dir / CALLS_FILE
    candidates_path = out_dir / CANDIDATES_FILE
    recorded = _read_jsonl(calls_path)
    _refuse_changed_arms(recorded, arms)
    done = {_key(row) for row in recorded}

    paths = discover_sessions(transcripts_dir)
    if max_transcripts is not None:
        paths = paths[:max_transcripts]
    clients: dict[str, LLMClient | None] = {}
    locality = {arm.name: is_local(arm) for arm in arms}

    for path in paths:
        transcript_id = path.relative_to(transcripts_dir).as_posix()
        pending = [arm for arm in arms if (transcript_id, arm.name) not in done]
        if not pending:
            continue
        if not include_ephemeral and is_ephemeral_project(path, transcripts_dir):
            # The sweep skips these unread; so does the eval, on the record.
            for arm in pending:
                call = _call_row(transcript_id, arm)
                call["outcome"] = OUTCOME_SKIPPED_EPHEMERAL
                _append_jsonl(calls_path, [call])
            continue
        digest = parse_session(path)
        for arm in pending:
            if arm.name not in clients and not digest.is_empty:
                clients[arm.name] = _build_client(build, arm)
            call, rows = judge_transcript(
                digest,
                transcript_id,
                arm,
                clients.get(arm.name),
                local=locality[arm.name],
                timeout=timeout,
                stores=stores,
            )
            # Candidates first: the call row is what marks the pair done, and
            # its attempt_id is what makes these rows count.
            _append_jsonl(candidates_path, rows)
            _append_jsonl(calls_path, [call])
            logger.info(
                "judge_eval_call",
                arm=arm.name,
                outcome=call["outcome"],
                candidates=call["candidates"],
            )

    return write_summaries(out_dir, arms, gold=gold)


# ---------------------------------------------------------------------------
# Summaries
# ---------------------------------------------------------------------------


def _rate(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else round(numerator / denominator, 4)


def _mean(values: Sequence[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def _completed(call: Mapping[str, Any]) -> bool:
    """Whether the judge replied on this call (see :data:`_COMPLETED_OUTCOMES`)."""
    return str(call.get("outcome")) in _COMPLETED_OUTCOMES


def _outcome_label(call: Mapping[str, Any]) -> str:
    label = str(call.get("outcome"))
    reason = call.get("unavailable_reason")
    return f"{label}:{reason}" if reason else label


def _match_score(candidate: Mapping[str, Any], gold: GoldMemory) -> float | None:
    """1.0 on an evidence-quote overlap, else the token Jaccard if it clears.

    The contained side must be at least :data:`MIN_EVIDENCE_CHARS` long: a
    word or two is contained in almost any evidence line.
    """
    cand_evidence = normalise_text(str(candidate.get("evidence") or ""))
    gold_evidence = normalise_text(gold.evidence)
    if (
        len(cand_evidence) >= MIN_EVIDENCE_CHARS and cand_evidence in gold_evidence
    ) or (len(gold_evidence) >= MIN_EVIDENCE_CHARS and gold_evidence in cand_evidence):
        return 1.0
    text = f"{candidate.get('title') or ''} {candidate.get('memory') or ''}"
    score = token_jaccard(text, gold.text)
    return score if score >= JACCARD_THRESHOLD else None


def match_gold(
    candidates: Sequence[Mapping[str, Any]], gold: Sequence[GoldMemory]
) -> tuple[list[tuple[int, int]], list[int], list[int]]:
    """Greedy one-to-one match, candidates in order, each to its best gold row.

    Returns ``(pairs, unmatched_candidate_indexes, unmatched_gold_indexes)``.
    """
    taken: set[int] = set()
    pairs: list[tuple[int, int]] = []
    unmatched: list[int] = []
    for c_index, candidate in enumerate(candidates):
        best: tuple[float, int] | None = None
        for g_index, row in enumerate(gold):
            if g_index in taken:
                continue
            score = _match_score(candidate, row)
            if score is not None and (best is None or score > best[0]):
                best = (score, g_index)
        if best is None:
            unmatched.append(c_index)
        else:
            taken.add(best[1])
            pairs.append((c_index, best[1]))
    return pairs, unmatched, [i for i in range(len(gold)) if i not in taken]


def _summarise_arm(
    arm: JudgeArm,
    calls: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    outcomes: dict[str, int] = {}
    for call in calls:
        label = _outcome_label(call)
        outcomes[label] = outcomes.get(label, 0) + 1
    asked = [c for c in calls if c.get("latency_s") is not None]
    latencies = [float(c["latency_s"]) for c in asked if _completed(c)]
    failed_latencies = [float(c["latency_s"]) for c in asked if not _completed(c)]
    usage = [c for c in calls if c.get("prompt_tokens") is not None]
    accepted = [c for c in candidates if c.get("accepted")]
    rejected: dict[str, int] = {}
    for row in candidates:
        if row.get("accepted"):
            continue
        label = str(row.get("gate"))
        if row.get("worthiness"):
            label = f"{label}:{row['worthiness']}"
        rejected[label] = rejected.get(label, 0) + 1
    proposed = len(candidates)
    return {
        "arm": arm.name,
        "model": arm.model,
        "salient_chars": arm.salient_chars,
        "context_tokens": arm.context_tokens,
        "prompt_variant": arm.prompt_variant,
        "local": arm.local,
        "transcripts": len(calls),
        "outcomes": dict(sorted(outcomes.items())),
        "judge_calls": len(asked),
        "failures": sum(
            1 for c in calls if c.get("unavailable_reason") == "model_error"
        ),
        "timeouts": sum(1 for c in calls if c.get("unavailable_reason") == "timeout"),
        "candidates_proposed": proposed,
        "candidates_accepted": len(accepted),
        "rejected_by_gate": dict(sorted(rejected.items())),
        "grounded_rate": _rate(
            sum(1 for c in candidates if c.get("evidence_grounded")), proposed
        ),
        "accepted_grounded_rate": _rate(
            sum(1 for c in accepted if c.get("evidence_grounded")), len(accepted)
        ),
        "evidence_too_short": sum(
            1 for c in candidates if c.get("evidence_check") == EVIDENCE_TOO_SHORT
        ),
        "evidence_in_window_rate": _rate(
            sum(1 for c in candidates if c.get("evidence_in_window")), proposed
        ),
        "duplicate_rate": _rate(
            sum(1 for c in candidates if c.get("within_arm_duplicate")), proposed
        ),
        # Completed calls only; a timeout's latency is the timeout, not the judge.
        "mean_latency_s": _mean(latencies),
        "failed_calls_mean_latency_s": _mean(failed_latencies),
        "calls_reporting_usage": len(usage),
        "prompt_tokens": sum(int(c["prompt_tokens"]) for c in usage),
        "completion_tokens": sum(int(c.get("completion_tokens") or 0) for c in usage),
    }


def _gold_match(
    candidates: Sequence[Mapping[str, Any]],
    gold: Mapping[str, list[GoldMemory]],
    transcript_ids: Iterable[str],
) -> tuple[int, int, int, list[Mapping[str, Any]], list[dict[str, Any]]]:
    """Match accepted candidates to gold over *transcript_ids*.

    Returns ``(matched, accepted, gold_rows, unmatched_candidates,
    unmatched_gold)``.
    """
    unmatched_candidates: list[Mapping[str, Any]] = []
    unmatched_gold: list[dict[str, Any]] = []
    matched = accepted_total = gold_total = 0
    for transcript_id in sorted(transcript_ids):
        rows = [
            c
            for c in candidates
            if c.get("accepted") and str(c["transcript_id"]) == transcript_id
        ]
        refs = list(gold.get(transcript_id, []))
        pairs, lone_candidates, lone_gold = match_gold(rows, refs)
        matched += len(pairs)
        accepted_total += len(rows)
        gold_total += len(refs)
        unmatched_candidates.extend(rows[i] for i in lone_candidates)
        unmatched_gold.extend(refs[i].model_dump() for i in lone_gold)
    return matched, accepted_total, gold_total, unmatched_candidates, unmatched_gold


def _gold_block(
    arm: JudgeArm,
    calls: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    gold: Mapping[str, list[GoldMemory]],
    out_dir: Path,
) -> dict[str, Any]:
    recorded = {str(c["transcript_id"]) for c in calls}
    completed = {str(c["transcript_id"]) for c in calls if _completed(c)}
    # A transcript the judge never answered on is not a miss: its gold rows
    # leave both denominators and are counted here by outcome instead.
    not_judged: dict[str, int] = {}
    not_judged_gold_rows = 0
    for call in calls:
        if _completed(call):
            continue
        label = _outcome_label(call)
        not_judged[label] = not_judged.get(label, 0) + 1
        not_judged_gold_rows += len(gold.get(str(call["transcript_id"]), []))
    matched, accepted_total, gold_total, lone_candidates, lone_gold = _gold_match(
        candidates, gold, completed
    )
    for name, payload in (
        (f"unmatched-candidates-{arm.name}.jsonl", lone_candidates),
        (f"unmatched-gold-{arm.name}.jsonl", lone_gold),
    ):
        _write_private(
            out_dir / name,
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in payload),
        )
    return {
        "matcher": (
            "greedy one-to-one over accepted candidates: evidence-quote "
            f"containment (contained side >= {MIN_EVIDENCE_CHARS} chars), else "
            f"normalised token Jaccard >= {JACCARD_THRESHOLD}"
        ),
        "gold_rows": gold_total,
        "gold_transcripts_not_judged": len(set(gold) - recorded),
        "transcripts_not_judged_by_outcome": dict(sorted(not_judged.items())),
        "gold_rows_excluded_not_judged": not_judged_gold_rows,
        "matched": matched,
        "precision": _rate(matched, accepted_total),
        "recall": _rate(matched, gold_total),
    }


def _intersection_block(
    arms: list[JudgeArm],
    calls_by_arm: Mapping[str, list[dict[str, Any]]],
    candidates_by_arm: Mapping[str, list[dict[str, Any]]],
    gold: Mapping[str, list[GoldMemory]],
) -> dict[str, Any]:
    """Precision and recall per arm over the transcripts every arm completed."""
    shared: set[str] | None = None
    for arm in arms:
        done = {
            str(c["transcript_id"]) for c in calls_by_arm[arm.name] if _completed(c)
        }
        shared = done if shared is None else shared & done
    common = shared or set()
    per_arm: dict[str, Any] = {}
    for arm in arms:
        matched, accepted_total, gold_total, _, _ = _gold_match(
            candidates_by_arm[arm.name], gold, common
        )
        per_arm[arm.name] = {
            "matched": matched,
            "gold_rows": gold_total,
            "precision": _rate(matched, accepted_total),
            "recall": _rate(matched, gold_total),
        }
    return {"transcripts": len(common), "arms": per_arm}


def write_summaries(
    out_dir: Path,
    arms: list[JudgeArm],
    *,
    gold: Mapping[str, list[GoldMemory]] | None = None,
) -> dict[str, Any]:
    """Summarise every row on disk, per arm, and write ``summary-<arm>.json``."""
    calls_by_key = {_key(row): row for row in _read_jsonl(out_dir / CALLS_FILE)}
    # A candidate row counts only if its attempt_id is the one on its pair's
    # call row. Anything else is residue of an interrupted attempt (rows
    # written, call row not), whatever the attempt that replaced it produced.
    latest: dict[tuple[str, str, int], dict[str, Any]] = {}
    for row in _read_jsonl(out_dir / CANDIDATES_FILE):
        call = calls_by_key.get(_key(row))
        if call is not None and row.get("attempt_id") == call.get("attempt_id"):
            latest[(*_key(row), int(row.get("candidate_index", 0)))] = row
    summaries: dict[str, Any] = {}
    calls_by_arm: dict[str, list[dict[str, Any]]] = {}
    candidates_by_arm: dict[str, list[dict[str, Any]]] = {}
    for arm in arms:
        calls = [row for key, row in calls_by_key.items() if key[1] == arm.name]
        candidates = [row for key, row in sorted(latest.items()) if key[1] == arm.name]
        calls_by_arm[arm.name] = calls
        candidates_by_arm[arm.name] = candidates
        summary = _summarise_arm(arm, calls, candidates)
        if gold is not None:
            summary["gold"] = _gold_block(arm, calls, candidates, gold, out_dir)
        _write_private(
            out_dir / f"summary-{arm.name}.json",
            json.dumps(summary, indent=2, sort_keys=True) + "\n",
        )
        summaries[arm.name] = summary
    report: dict[str, Any] = {"output_dir": str(out_dir), "arms": summaries}
    if gold is not None:
        report["intersection"] = _intersection_block(
            arms, calls_by_arm, candidates_by_arm, gold
        )
    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

#: Exit codes: 0 ran; 1 an unusable arm/gold file, transcripts or output
#: directory, an arm with no client (missing key, extra or route), an arm whose
#: config changed since its recorded rows, or label stores that would not open
#: (argparse itself exits 2 on bad arguments).
EXIT_OK = 0
EXIT_CONFIG = 1


def _positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        msg = f"must be a positive integer, got {value!r}"
        raise argparse.ArgumentTypeError(msg)
    return number


def _positive_float(value: str) -> float:
    number = float(value)
    if number <= 0:
        msg = f"must be a positive number, got {value!r}"
        raise argparse.ArgumentTypeError(msg)
    return number


def open_label_stores() -> StoreRegistry:
    """The configured stores, for the ``labels`` egress rule's reads only.

    Resolved as the sweep resolves them (``StoreRegistry.from_config_dir()``).
    The eval only reads: pack events from the event log and served rows from
    the document store. It holds no executor and emits nothing.
    """
    try:
        return StoreRegistry.from_config_dir()
    except Exception as exc:
        msg = f"--egress-labels: the configured stores could not be opened: {exc}"
        raise EvalConfigError(msg) from exc


def eval_main(argv: list[str]) -> int:
    """``trellis-session-capture eval-judge``: print the summaries as JSON."""
    parser = argparse.ArgumentParser(
        prog="trellis-session-capture eval-judge",
        description=(
            "Run the capture judge and gates offline over a transcript directory, "
            "once per arm; write rows and summaries, never to a store."
        ),
    )
    parser.add_argument("--transcripts", type=Path, required=True)
    parser.add_argument(
        "--arms", type=Path, required=True, help="YAML or JSON arm file"
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="output directory; refused inside a git work tree",
    )
    parser.add_argument("--gold", type=Path, default=None, help="gold JSONL")
    parser.add_argument("--max-transcripts", type=_positive_int, default=None)
    parser.add_argument(
        "--timeout",
        type=_positive_float,
        default=distill.DEFAULT_TIMEOUT_S,
        help="per judge call, seconds",
    )
    parser.add_argument(
        "--include-ephemeral",
        action="store_true",
        help="judge transcripts from throwaway project directories the sweep skips",
    )
    parser.add_argument(
        "--egress-labels",
        action="store_true",
        help=(
            "apply the sweep's egress rule (labels of the documents each pack "
            "served), reading the configured stores; default is the strict rule"
        ),
    )
    args = parser.parse_args(argv)
    try:
        arms = load_arms(args.arms)
        gold = None if args.gold is None else load_gold(args.gold)
        stores = open_label_stores() if args.egress_labels else None
        try:
            report = run_eval(
                args.transcripts,
                arms,
                args.output,
                timeout=args.timeout,
                max_transcripts=args.max_transcripts,
                gold=gold,
                include_ephemeral=args.include_ephemeral,
                stores=stores,
            )
        finally:
            if stores is not None:
                stores.close()
    except EvalConfigError as exc:
        sys.stderr.write(f"eval-judge: {exc}\n")
        return EXIT_CONFIG
    sys.stdout.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return EXIT_OK
