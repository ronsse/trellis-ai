"""Deterministic evidence for verifiable trace fields (#308).

Some facts about an agent run are *verifiable from the trace itself*: the
files it touched are named in its Edit/Write tool payloads, the commands
it ran are the ``command`` args of its shell steps.  An LLM asked to
summarise the same trace can hallucinate every one of them.  This module
is the claim-floor philosophy of #299-#301 ("extraction attests mention,
never possession") applied one level deeper: **whenever a field is
verifiable from the source material, the deterministic parse wins and the
LLM value is at most additive.**

Two halves:

* :func:`parse_trace_evidence` — pure parse of a
  :class:`~trellis.schemas.trace.Trace`'s ``tool_call`` steps into a
  :class:`TraceEvidence` record: files touched (edit/write tool payloads
  plus unified-diff ``+++ b/`` hunks in patch args), files read, and
  commands run.  Zero LLM, zero stores.
* :func:`apply_trace_evidence` — the override gate.  Rewrites the
  verifiable properties on a result's Activity draft so that every
  evidence value survives verbatim and any extractor-supplied value
  outside the evidence is *kept but demoted* to a ``*_unverified``
  companion property.  A supplied value can never displace, rename, or
  remove an attested one — deterministic wins on conflict — and for
  ``files_touched`` it does not reach the attested key at all
  (see :data:`_EVIDENCE_ONLY_PROPERTIES`).

The gate runs at the shared trace-extraction seam
(:func:`trellis.extract.trace_ingest_hook.extract_trace_batch`) — the one
production path both the live post-ingest hook and the ``trellis extract
traces`` backfill route through — rather than inside
:class:`~trellis.extract.trace.TraceExtractor`.  Deliberately so: the
override must govern *whatever extractor* produced the result, and
placing it in the deterministic extractor would both leave a future LLM
residue pass (the follow-up deferred in ``trace.py``'s module footer)
ungoverned and create an import cycle (``trace.py`` supplies
:func:`~trellis.extract.trace.normalize_slug` to this module).  Today
the extractor supplies no verifiable values, so the gate simply
*populates* the fields from evidence; when an LLM stage joins the path
its claims meet the merge rule with zero further wiring.  This mirrors
the ``DETERMINISTIC > HYBRID > LLM`` dispatcher priority at the
granularity of a single field.

Deliberate scope limits
-----------------------

* **Shell side effects are not evidence.**  A ``bash`` step's command is
  recorded verbatim, but the files it may have touched are not inferred
  from it — parsing shell for writes would be guesswork wearing a
  deterministic badge.
* **Exit codes are not parsed.**  ``TraceStep.result`` has no payload
  contract (it is a free dict), so there is no key an exit code can be
  read from without inventing one.  When observer capture defines a
  result shape, exit codes join :class:`TraceEvidence` the same way.
* **No list caps.**  Evidence silently dropped for size would defeat the
  point of an evidence channel; only individual command strings are
  bounded (:data:`MAX_COMMAND_CHARS`) because heredoc payloads can run
  to kilobytes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from pydantic import Field

from trellis.core.base import TrellisModel
from trellis.extract.trace import normalize_slug

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from trellis.schemas.extraction import EntityDraft, ExtractionResult
    from trellis.schemas.trace import Trace, TraceStep

logger = structlog.get_logger(__name__)

__all__ = [
    "COMMANDS_RUN_PROPERTY",
    "FILES_READ_PROPERTY",
    "FILES_TOUCHED_PROPERTY",
    "MAX_COMMAND_CHARS",
    "EvidenceMerge",
    "TraceEvidence",
    "apply_trace_evidence",
    "merge_with_evidence",
    "parse_trace_evidence",
    "unverified_property_key",
]

#: Activity-node property keys the evidence override governs.
FILES_TOUCHED_PROPERTY = "files_touched"
FILES_READ_PROPERTY = "files_read"
COMMANDS_RUN_PROPERTY = "commands_run"

#: Governed properties whose attested key carries **evidence only**.
#: #308's reference behaviour discards a model's claim about what it
#: modified outright ("the model cannot hallucinate what it edited"), so
#: an unattested path lands under the ``*_unverified`` companion and
#: nowhere else.  The other two take the union the issue grants — a file
#: the model saw that the trace did not record is plausible-but-
#: unverified, not false — with the companion still naming which members
#: are unattested.
_EVIDENCE_ONLY_PROPERTIES = frozenset({FILES_TOUCHED_PROPERTY})

#: Suffix for the companion property carrying supplied-but-unverified
#: extensions (see :func:`unverified_property_key`).
_UNVERIFIED_SUFFIX = "_unverified"

#: Upper bound on a single recorded command string.  Truncation is marked
#: with a trailing ellipsis so a bounded value can't be mistaken for the
#: whole command.
MAX_COMMAND_CHARS = 500

#: The only step type whose payload is tool evidence (mirrors the
#: ``_TOOL_STEP_TYPES`` routing in ``trace.py``).
_TOOL_CALL_STEP_TYPE = "tool_call"

#: Tool-name slugs (post-:func:`normalize_slug`) whose payload names a
#: file being written or edited.  Covers Claude Code (``Edit`` / ``Write``
#: / ``MultiEdit`` / ``NotebookEdit``) and the docs' worked examples
#: (``edit_file``).
_EDIT_TOOL_SLUGS = frozenset(
    {
        "edit",
        "edit-file",
        "write",
        "write-file",
        "create-file",
        "multiedit",
        "multi-edit",
        "notebookedit",
        "notebook-edit",
    }
)

#: Tool-name slugs whose payload names a file being read.
_READ_TOOL_SLUGS = frozenset({"read", "read-file", "view", "view-file"})

#: The str-replace editor multiplexes reads and writes through a
#: ``command`` discriminator, so its slug cannot sit in
#: :data:`_EDIT_TOOL_SLUGS`: a ``view`` invocation only read the file,
#: and recording it as touched would be the evidence channel asserting a
#: modification that never happened.  A discriminator that is absent or
#: outside both sets contributes nothing — a missing shape is honest, a
#: mislabelled one is not.
_STR_REPLACE_EDITOR_SLUG = "str-replace-editor"
_STR_REPLACE_VIEW_COMMAND = "view"
_STR_REPLACE_EDIT_COMMANDS = frozenset({"create", "str_replace", "insert", "undo_edit"})

#: Tool-name slugs whose payload carries a shell command.
_SHELL_TOOL_SLUGS = frozenset({"bash", "shell", "run-command", "execute-command"})

#: ``args`` keys that name a file path, in the spellings the known tool
#: shapes use.  Every present string-valued key contributes.
_PATH_ARG_KEYS = ("file_path", "notebook_path", "path", "file", "filename")

#: ``args`` keys that carry a shell command.
_COMMAND_ARG_KEYS = ("command", "cmd", "script")

#: ``args`` keys that carry unified-diff text on *any* tool.  Tools whose
#: slug contains ``patch`` (``apply_patch``, ``git-apply-patch``, ...)
#: additionally have every string arg scanned — but an arbitrary tool's
#: free-text args are NOT scanned, because a ``Write`` whose ``content``
#: quotes a diff would otherwise claim the quoted paths as touched.
_PATCH_ARG_KEYS = ("patch", "diff", "unified_diff")

#: Unified-diff file headers are only read as the full ``---`` / ``+++``
#: / ``@@`` triple that opens a hunk block.  Matching either marker on
#: its own anywhere in the text would read diff *body* lines as
#: filenames: a removed SQL comment ``-- drop the temp table`` carries
#: the hunk's own ``-`` prefix on the wire and arrives as
#: ``--- drop the temp table``.  The triple is the one shape a body line
#: cannot forge — every line inside a hunk carries a ``+``/``-``/space
#: prefix, so none of them can begin with ``@@``.
#:
#: Both header sides count as touched: the ``---`` side is the only name
#: a deletion hunk carries, and for a plain modification the two sides
#: dedupe to one path after prefix stripping.
_DIFF_OLD_MARKER = "--- "
_DIFF_NEW_MARKER = "+++ "
_DIFF_HUNK_MARKER = "@@"

#: Git's conventional diff prefixes, stripped from header paths.
_DIFF_PATH_PREFIXES = ("a/", "b/")

#: The null path a creation/deletion hunk uses for its absent side.
_DIFF_DEV_NULL = "/dev/null"


class TraceEvidence(TrellisModel):
    """Verifiable fields parsed deterministically from one trace.

    Every list is ordered by first appearance in the step sequence and
    de-duplicated exactly (paths are case-sensitive; no normalization is
    applied beyond diff-prefix stripping, because rewriting a path is a
    contradiction of the evidence).
    """

    trace_id: str
    files_touched: list[str] = Field(default_factory=list)
    files_read: list[str] = Field(default_factory=list)
    commands_run: list[str] = Field(default_factory=list)

    def is_empty(self) -> bool:
        """True when the trace yielded no verifiable values at all."""
        return not (self.files_touched or self.files_read or self.commands_run)


class EvidenceMerge(TrellisModel):
    """Outcome of merging supplied values with parsed evidence.

    ``values`` is the final field value: every evidence value in evidence
    order, then every supplied extension in supplied order.  The split is
    preserved so a consumer can tell attested facts from model claims.
    """

    values: list[str] = Field(default_factory=list)
    evidence_values: list[str] = Field(default_factory=list)
    unverified_values: list[str] = Field(default_factory=list)


def unverified_property_key(property_key: str) -> str:
    """Companion property carrying supplied-but-unverified extensions."""
    return f"{property_key}{_UNVERIFIED_SUFFIX}"


def parse_trace_evidence(trace: Trace) -> TraceEvidence:
    """Parse a trace's ``tool_call`` payloads into verifiable evidence.

    Pure and total: an unrecognized tool or payload shape contributes
    nothing rather than raising.  Only ``step_type == "tool_call"`` steps
    are read — a ``decision`` or ``observation`` step's args are prose,
    not tool payloads.
    """
    files_touched: list[str] = []
    files_read: list[str] = []
    commands_run: list[str] = []

    for step in trace.steps:
        if step.step_type != _TOOL_CALL_STEP_TYPE:
            continue
        slug = normalize_slug(step.name)
        if slug == _STR_REPLACE_EDITOR_SLUG:
            command = step.args.get("command")
            if command == _STR_REPLACE_VIEW_COMMAND:
                files_read.extend(_path_args(step))
            elif isinstance(command, str) and command in _STR_REPLACE_EDIT_COMMANDS:
                files_touched.extend(_path_args(step))
        elif slug in _EDIT_TOOL_SLUGS:
            files_touched.extend(_path_args(step))
        elif slug in _READ_TOOL_SLUGS:
            files_read.extend(_path_args(step))
        elif slug in _SHELL_TOOL_SLUGS:
            commands_run.extend(_command_args(step))
        files_touched.extend(_patch_paths(step, slug))

    return TraceEvidence(
        trace_id=trace.trace_id,
        files_touched=_deduped(files_touched),
        files_read=_deduped(files_read),
        commands_run=_deduped(commands_run),
    )


def merge_with_evidence(
    evidence_values: Sequence[str],
    supplied_values: Iterable[str],
) -> EvidenceMerge:
    """Merge supplied values into parsed evidence, evidence-first.

    The contract of #308: every evidence value survives verbatim and in
    evidence order; a supplied value already attested by evidence merges
    into it (that *is* the "deterministic wins on conflict" case — the
    evidence copy is the one kept); a supplied value outside the evidence
    is classified as an unverified extension.  Nothing is ever removed on
    the evidence side, so a supplied list cannot contradict the parse by
    omission either.

    ``values`` offers the union for the fields that grant one; whether a
    caller uses it or ``evidence_values`` is the per-field policy in
    :data:`_EVIDENCE_ONLY_PROPERTIES`.
    """
    evidence = _deduped(evidence_values)
    seen = set(evidence)
    unverified: list[str] = []
    for value in supplied_values:
        if not isinstance(value, str):
            continue
        stripped = value.strip()
        if not stripped or stripped in seen:
            continue
        seen.add(stripped)
        unverified.append(stripped)
    return EvidenceMerge(
        values=[*evidence, *unverified],
        evidence_values=evidence,
        unverified_values=unverified,
    )


def apply_trace_evidence(
    result: ExtractionResult,
    evidence: TraceEvidence,
) -> ExtractionResult:
    """Override the Activity draft's verifiable properties with evidence.

    For each governed property (:data:`FILES_TOUCHED_PROPERTY`,
    :data:`FILES_READ_PROPERTY`, :data:`COMMANDS_RUN_PROPERTY`) on the
    draft whose ``entity_id`` is ``trace:<evidence.trace_id>``:

    * the property is rewritten from the :func:`merge_with_evidence`
      result — evidence values verbatim, plus supplied extensions for
      the fields that grant a union (:data:`_EVIDENCE_ONLY_PROPERTIES`
      names the field that does not);
    * extensions are listed under the ``*_unverified`` companion key so
      a demoted claim stays legible to retrieval and review instead of
      being silently dropped;
    * a field with neither evidence nor supplied values stays absent.

    Idempotent (re-applying changes nothing) and pure — returns a new
    :class:`~trellis.schemas.extraction.ExtractionResult`; drafts other
    than the Activity pass through untouched.  A result with no Activity
    draft (an LLM stage that ignored the instruction to emit one) passes
    through with a warning: there is nowhere to pin the evidence, and
    minting a node here would bypass the extractor's dedup/canonicalize
    path.
    """
    activity_id = f"trace:{evidence.trace_id}"
    governed: tuple[tuple[str, list[str]], ...] = (
        (FILES_TOUCHED_PROPERTY, evidence.files_touched),
        (FILES_READ_PROPERTY, evidence.files_read),
        (COMMANDS_RUN_PROPERTY, evidence.commands_run),
    )

    entities: list[EntityDraft] = []
    activity_found = False
    for entity in result.entities:
        if entity.entity_id != activity_id:
            entities.append(entity)
            continue
        activity_found = True
        entities.append(_override_entity(entity, governed, activity_id))

    if not activity_found:
        if not evidence.is_empty():
            logger.warning(
                "trace_evidence_activity_draft_missing",
                trace_id=evidence.trace_id,
                extractor=result.extractor_used,
            )
        return result

    return result.model_copy(update={"entities": entities})


# ----------------------------------------------------------------------
# Internals
# ----------------------------------------------------------------------


def _override_entity(
    entity: EntityDraft,
    governed: tuple[tuple[str, list[str]], ...],
    activity_id: str,
) -> EntityDraft:
    """Rewrite one draft's governed properties from evidence."""
    properties = dict(entity.properties)
    for key, evidence_values in governed:
        merge = merge_with_evidence(evidence_values, _supplied_values(properties, key))
        unverified_key = unverified_property_key(key)
        attested = (
            merge.evidence_values if key in _EVIDENCE_ONLY_PROPERTIES else merge.values
        )
        if attested:
            properties[key] = attested
        else:
            # Leave no empty-list residue where nothing was attested.
            properties.pop(key, None)
        if merge.unverified_values:
            properties[unverified_key] = merge.unverified_values
            logger.info(
                "trace_evidence_unverified_extension",
                entity_id=activity_id,
                property=key,
                unverified=merge.unverified_values,
            )
        else:
            # Clear a stray companion so re-application is idempotent.
            properties.pop(unverified_key, None)
    return entity.model_copy(update={"properties": properties})


def _supplied_values(properties: dict[str, object], key: str) -> list[str]:
    """Extractor-supplied claims for a governed property, as a list.

    A prior application's ``*_unverified`` companion is folded back in so
    re-applying the gate re-classifies rather than double-counts.
    """
    supplied: list[str] = []
    for source_key in (key, unverified_property_key(key)):
        raw = properties.get(source_key)
        if isinstance(raw, str):
            supplied.append(raw)
        elif isinstance(raw, list):
            supplied.extend(v for v in raw if isinstance(v, str))
    return supplied


def _path_args(step: TraceStep) -> list[str]:
    """Every path-shaped arg value present on the step."""
    return [
        value.strip()
        for key in _PATH_ARG_KEYS
        if isinstance(value := step.args.get(key), str) and value.strip()
    ]


def _command_args(step: TraceStep) -> list[str]:
    """Every command-shaped arg value present on the step, bounded."""
    return [
        _truncate_command(value.strip())
        for key in _COMMAND_ARG_KEYS
        if isinstance(value := step.args.get(key), str) and value.strip()
    ]


def _truncate_command(command: str) -> str:
    if len(command) <= MAX_COMMAND_CHARS:
        return command
    return command[:MAX_COMMAND_CHARS] + "…"


def _patch_paths(step: TraceStep, slug: str) -> list[str]:
    """File paths named by unified-diff headers in the step's patch args."""
    is_patch_tool = "patch" in slug or slug == "git-apply"
    texts = [
        value
        for key, value in step.args.items()
        if isinstance(value, str)
        and (key in _PATCH_ARG_KEYS or is_patch_tool)
        and value.strip()
    ]
    paths: list[str] = []
    for text in texts:
        paths.extend(_diff_header_paths(text))
    return paths


def _diff_header_paths(text: str) -> list[str]:
    """Paths named by ``---`` / ``+++`` / ``@@`` header triples in *text*."""
    lines = text.splitlines()
    paths: list[str] = []
    for index in range(len(lines) - 2):
        old_line, new_line, hunk_line = lines[index : index + 3]
        if not (
            old_line.startswith(_DIFF_OLD_MARKER)
            and new_line.startswith(_DIFF_NEW_MARKER)
            and hunk_line.startswith(_DIFF_HUNK_MARKER)
        ):
            continue
        sides = (
            old_line[len(_DIFF_OLD_MARKER) :],
            new_line[len(_DIFF_NEW_MARKER) :],
        )
        paths.extend(path for side in sides if (path := _diff_header_path(side)))
    return paths


def _diff_header_path(side: str) -> str:
    """One header path: ``diff -u`` tab metadata and ``a/``/``b/`` stripped."""
    path = side.split("\t", 1)[0].strip()
    for prefix in _DIFF_PATH_PREFIXES:
        if path.startswith(prefix):
            path = path[len(prefix) :]
            break
    return "" if path == _DIFF_DEV_NULL else path


def _deduped(values: Iterable[str]) -> list[str]:
    """Order-preserving exact de-duplication of stripped values.

    Stripping happens here because ``TrellisModel`` stores strings
    stripped: deduping the raw form would let ``"a.py"`` and ``" a.py"``
    both survive the parse and then collide once stored.
    """
    return list(
        dict.fromkeys(stripped for value in values if (stripped := value.strip()))
    )
