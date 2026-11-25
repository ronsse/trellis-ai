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
transcripts. And the sweep's egress check reads the event log for the
documents a session's packs served; this command opens no store, so a
transcript that names any pack is treated as having been served
``restricted`` content and is kept off an arm whose route is not declared
``local: true`` (recorded as ``egress_withheld``, never sent).

The deterministic checks it writes are measurements, not verdicts. In
particular ``evidence_grounded`` says the candidate's ``evidence`` string
appears verbatim (whitespace-normalised) in the parsed conversation; it is a
fabrication detector, not a usefulness score. A grounded memory can be
useless, and a paraphrased evidence line from a good memory reads as
ungrounded.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
import yaml
from pydantic import Field, ValidationError, field_validator, model_validator

from trellis.core.base import TrellisModel
from trellis.core.path_presence import path_is_present
from trellis.llm.egress import EgressGate, may_reach_route
from trellis.llm.routing import LLMConsumer, LLMRoutingError
from trellis.stores.registry import StoreRegistry
from trellis_workers.session_capture import distill
from trellis_workers.session_capture.capture import (
    GATE_SECRET_SCAN,
    gate_candidate,
)
from trellis_workers.session_capture.transcripts import (
    discover_sessions,
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

#: Call outcomes beyond :class:`~.distill.DistillOutcome`.
OUTCOME_SKIPPED_EMPTY = "skipped_empty"
OUTCOME_EGRESS_WITHHELD = "egress_withheld"

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


def quote_in(quote: str, text: str) -> bool | None:
    """Whether *quote* appears in *text*, whitespace-normalised; ``None`` if empty."""
    needle = normalise_whitespace(quote)
    if not needle:
        return None
    return needle in normalise_whitespace(text)


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
    """Whether the arm's route is declared local, read by :class:`EgressGate`."""
    try:
        return EgressGate.for_route(
            _arm_registry(arm), LLMConsumer.SESSION_CAPTURE, None
        ).local
    except LLMRoutingError as exc:
        msg = f"arm {arm.name!r} has an unusable route: {exc}"
        raise EvalConfigError(msg) from exc


def _eval_egress_permits(digest: SessionDigest, *, local: bool) -> bool:
    """Whether this transcript may go to the arm's route without a store read.

    A transcript that named no pack carries no served label, which is how the
    sweep reads it too. One that named a pack (or a pack line that did not
    parse) was served documents whose labels only a store can answer, so it is
    judged as if they were the strictest label.
    """
    if not digest.pack_ids and not digest.pack_ids_unparsed:
        return True
    return may_reach_route(_UNREAD_SERVED_LABEL, local=local)


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
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Run one arm's judge and the production gates on one transcript.

    Returns the call row and the candidate rows. Nothing is written anywhere.
    """
    call: dict[str, Any] = {
        "transcript_id": transcript_id,
        "arm": arm.name,
        "model": arm.model,
        "outcome": None,
        "unavailable_reason": None,
        "parse_error": None,
        "latency_s": None,
        "prompt_tokens": None,
        "completion_tokens": None,
        "candidates": 0,
    }
    if digest.is_empty:
        call["outcome"] = OUTCOME_SKIPPED_EMPTY
        return call, []
    if not _eval_egress_permits(digest, local=local):
        call["outcome"] = OUTCOME_EGRESS_WITHHELD
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
        redacted = decision.gate == GATE_SECRET_SCAN
        rows.append(
            {
                "transcript_id": transcript_id,
                "arm": arm.name,
                "candidate_index": index,
                # A candidate the secret scan blocked is never written out:
                # production never stores it, and neither does this file.
                "redacted": redacted,
                "title": None if redacted else candidate.title,
                "memory": None if redacted else candidate.memory,
                "evidence": None if redacted else candidate.evidence,
                "memory_type": candidate.memory_type,
                "signal": candidate.signal,
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
                "evidence_grounded": quote_in(candidate.evidence, conversation),
                "evidence_in_window": quote_in(candidate.evidence, shown),
                "within_arm_duplicate": duplicate,
            }
        )
    call["candidates"] = len(rows)
    return call, rows


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


def _append_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
        handle.flush()


def _key(row: Mapping[str, Any]) -> tuple[str, str]:
    return str(row.get("transcript_id")), str(row.get("arm"))


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
) -> dict[str, Any]:
    """Judge every transcript with every arm and write rows and summaries.

    Resumable: a ``(transcript, arm)`` pair that already has a row in
    ``calls.jsonl`` is skipped, failures included (delete the row to retry
    it). A failed or timed-out judge call is recorded and the run goes on.

    Raises:
        EvalConfigError: the output directory is inside a git work tree, or
            an arm's client cannot be built.
    """
    build = build_arm_client if client_factory is None else client_factory
    is_local = arm_route_is_local if local_of is None else local_of
    if inside_git_work_tree(out_dir):
        msg = (
            f"refusing output directory {out_dir}: it is inside a git work tree, "
            "and transcripts and distilled memories are private data"
        )
        raise EvalConfigError(msg)
    out_dir.mkdir(parents=True, exist_ok=True)
    calls_path = out_dir / CALLS_FILE
    candidates_path = out_dir / CANDIDATES_FILE
    done = {_key(row) for row in _read_jsonl(calls_path)}

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
        digest = parse_session(path)
        for arm in pending:
            if arm.name not in clients and not digest.is_empty:
                client = build(arm)
                if client is None:
                    msg = (
                        f"arm {arm.name!r}: no judge client could be built; is the "
                        f"environment variable {arm.api_key_env} set?"
                    )
                    raise EvalConfigError(msg)
                clients[arm.name] = client
            call, rows = judge_transcript(
                digest,
                transcript_id,
                arm,
                clients.get(arm.name),
                local=locality[arm.name],
                timeout=timeout,
            )
            # Candidates first: the call row is what marks the pair done.
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


def _match_score(candidate: Mapping[str, Any], gold: GoldMemory) -> float | None:
    """1.0 on an evidence-quote overlap, else the token Jaccard if it clears."""
    cand_evidence = normalise_text(str(candidate.get("evidence") or ""))
    gold_evidence = normalise_text(gold.evidence)
    if (
        cand_evidence
        and gold_evidence
        and (cand_evidence in gold_evidence or gold_evidence in cand_evidence)
    ):
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
        label = str(call.get("outcome"))
        reason = call.get("unavailable_reason")
        if reason:
            label = f"{label}:{reason}"
        outcomes[label] = outcomes.get(label, 0) + 1
    asked = [c for c in calls if c.get("latency_s") is not None]
    latencies = [float(c["latency_s"]) for c in asked]
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
        "evidence_in_window_rate": _rate(
            sum(1 for c in candidates if c.get("evidence_in_window")), proposed
        ),
        "duplicate_rate": _rate(
            sum(1 for c in candidates if c.get("within_arm_duplicate")), proposed
        ),
        "mean_latency_s": (
            round(sum(latencies) / len(latencies), 4) if latencies else None
        ),
        "calls_reporting_usage": len(usage),
        "prompt_tokens": sum(int(c["prompt_tokens"]) for c in usage),
        "completion_tokens": sum(int(c.get("completion_tokens") or 0) for c in usage),
    }


def _gold_block(
    arm: JudgeArm,
    calls: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    gold: Mapping[str, list[GoldMemory]],
    out_dir: Path,
) -> dict[str, Any]:
    judged = {str(c["transcript_id"]) for c in calls}
    unmatched_candidates: list[dict[str, Any]] = []
    unmatched_gold: list[dict[str, Any]] = []
    matched = accepted_total = gold_total = 0
    for transcript_id in sorted(judged):
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
    for name, payload in (
        (f"unmatched-candidates-{arm.name}.jsonl", unmatched_candidates),
        (f"unmatched-gold-{arm.name}.jsonl", unmatched_gold),
    ):
        target = out_dir / name
        target.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in payload),
            encoding="utf-8",
        )
    return {
        "matcher": (
            "greedy one-to-one over accepted candidates: evidence-quote "
            "containment, else normalised token Jaccard >= "
            f"{JACCARD_THRESHOLD}"
        ),
        "gold_rows": gold_total,
        "gold_transcripts_not_judged": len(set(gold) - judged),
        "matched": matched,
        "precision": _rate(matched, accepted_total),
        "recall": _rate(matched, gold_total),
    }


def write_summaries(
    out_dir: Path,
    arms: list[JudgeArm],
    *,
    gold: Mapping[str, list[GoldMemory]] | None = None,
) -> dict[str, Any]:
    """Summarise every row on disk, per arm, and write ``summary-<arm>.json``."""
    calls_by_key = {_key(row): row for row in _read_jsonl(out_dir / CALLS_FILE)}
    # Last row wins per (pair, index); a candidate row with no call row is
    # residue of an interrupted call that will be re-run, and is ignored.
    latest: dict[tuple[str, str, int], dict[str, Any]] = {}
    for row in _read_jsonl(out_dir / CANDIDATES_FILE):
        if _key(row) in calls_by_key:
            latest[(*_key(row), int(row.get("candidate_index", 0)))] = row
    summaries: dict[str, Any] = {}
    for arm in arms:
        calls = [row for key, row in calls_by_key.items() if key[1] == arm.name]
        candidates = [row for key, row in sorted(latest.items()) if key[1] == arm.name]
        summary = _summarise_arm(arm, calls, candidates)
        if gold is not None:
            summary["gold"] = _gold_block(arm, calls, candidates, gold, out_dir)
        (out_dir / f"summary-{arm.name}.json").write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        summaries[arm.name] = summary
    return {"output_dir": str(out_dir), "arms": summaries}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

#: Exit codes: 0 ran, 1 an unusable arm/gold file or output directory, or an
#: arm with no client (argparse itself exits 2 on bad arguments).
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


def eval_main(argv: list[str]) -> int:
    """``trellis-session-capture eval-judge``: print the summaries as JSON."""
    parser = argparse.ArgumentParser(
        prog="trellis-session-capture eval-judge",
        description=(
            "Run the capture judge and gates offline over a transcript directory, "
            "once per arm; write rows and summaries, never a store."
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
    args = parser.parse_args(argv)
    try:
        arms = load_arms(args.arms)
        gold = None if args.gold is None else load_gold(args.gold)
        report = run_eval(
            args.transcripts,
            arms,
            args.output,
            timeout=args.timeout,
            max_transcripts=args.max_transcripts,
            gold=gold,
        )
    except EvalConfigError as exc:
        sys.stderr.write(f"eval-judge: {exc}\n")
        return EXIT_CONFIG
    sys.stdout.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return EXIT_OK
