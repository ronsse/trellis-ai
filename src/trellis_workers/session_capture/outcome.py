"""What a session did, read from its own transcript: the session outcome.

The grade an agent gives a pack cannot be both the readout and the outcome,
and traces are nearly all "success", so neither tells a session that went
well from one that did not. The outcome is read from the transcript instead,
and from nothing a pack touched, so an analysis can compare sessions that
retrieved with sessions that did not. It records candidates; which of them is
the primary outcome is for a pre-registration to choose, not this module.

Counts, one duration and two flags, and nothing else. A ``Bash`` command and
the text of a qualifying result are read in memory, and only what they count
survives the parse: the digest is F8-safe and the event it rides is stored
where anyone with the event log can read it.

* **Assistant turns** are API messages, one per ``message.id``. Claude Code
  writes one record per content block and repeats the message's ``usage`` on
  each, input and cache counts unchanged and output growing, so a message's
  usage is each field's largest value, taken once. A record with no message id
  is a turn of its own.
* **Records Claude Code writes in the model's place**, an API error
  (``isApiErrorMessage``) or the ``<synthetic>`` model, are not turns and add
  no usage. An API error is an ending; the other kind changes nothing.
* **Token fields** are ``None`` when no message recorded them, because "not
  recorded" and "none" are different answers. A value that is not an ``int``
  (or is a ``bool``) is not recorded.
* **User turns** are user records that carry text, less the harness's
  ``isMeta`` records and compaction summaries. Harness notices that arrive as
  plain user text (a slash command, an interrupt marker) are counted too: no
  structural key tells them from a person's prompt.
* **Commits and pull requests** are ``Bash`` calls whose command runs
  ``git commit``, ``gh pr create`` or ``gh pr merge`` (see :func:`_steps`) and
  whose result is not an error, each kind counted once per call. ``pr_urls``
  counts the distinct pull request URLs those results print.
* **The wall clock** is the latest readable record ``timestamp`` less the
  earliest; Claude Code's stamps are not monotonic within a file.
* **The ending** is the state after the last record of the conversation: an
  errored tool result or an API error is ``ended_on_error``, a tool call left
  unanswered or an interrupt marker is ``ended_interrupted``.

Every transcript file gets its own outcome over every record in it, sidechain
records included, as with its pack ids. A sub-agent's work is in its own
``agent-*.jsonl``, and a parent never reads its ``Agent`` result, which
reports the sub-agent's totals and prints what it made. So summing a session
with its sub-agents counts each step once.
"""

from __future__ import annotations

import itertools
import re
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

#: The ``usage`` fields an outcome totals, as the API names them.
_TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)

#: The model Claude Code names on a record it writes in the model's place.
_SYNTHETIC_MODEL = "<synthetic>"

#: The start of the user text Claude Code writes when a person stops a turn.
_INTERRUPT_MARKER = "[Request interrupted by user"

#: A GitHub pull request URL as ``gh`` prints one. Counted, never kept.
_PR_URL = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")

#: Where one shell command ends and the next begins, near enough; see
#: :func:`_steps` for what that misses.
_COMMAND_BREAK = re.compile(r"[;&|()\n]")

#: A ``NAME=value`` word the shell reads as an environment assignment.
_ENV_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=\S*")

#: ``git`` global options whose value is the next word.
_GIT_VALUE_OPTIONS = frozenset({"-c", "-C"})

#: What a ``Bash`` call can make.
_COMMIT = "commit"
_PR_CREATE = "pr_create"
_PR_MERGE = "pr_merge"

#: Where the conversation stands after a record. The last one is the ending.
_REPLY = "reply"
_TOOL_USE = "tool_use"
_API_ERROR = "api_error"
_USER = "user"
_TOOL_OK = "tool_ok"
_TOOL_ERROR = "tool_error"
_INTERRUPT = "interrupt"
_ENDED_ON_ERROR = frozenset({_TOOL_ERROR, _API_ERROR})
_ENDED_INTERRUPTED = frozenset({_TOOL_USE, _INTERRUPT})


@dataclass(frozen=True)
class SessionOutcome:
    """What one transcript's session did, as counts, a duration and flags.

    The candidates a pre-registration chooses its outcome among; see the
    module docstring for how each is read. Every value is an ``int``, a
    ``float``, a ``bool`` or ``None``, and no transcript string is kept.
    """

    #: ``tool_use`` blocks, every tool.
    tool_calls: int = 0
    #: ``tool_result`` blocks flagged ``is_error``.
    tool_errors: int = 0
    #: API messages the model wrote, one per ``message.id``.
    assistant_turns: int = 0
    #: Of :attr:`assistant_turns`, those whose usage recorded a token field.
    assistant_turns_with_usage: int = 0
    #: User records carrying text, harness notices in plain text included.
    user_turns: int = 0
    #: Token totals over the messages that recorded each field, else ``None``.
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    #: The latest readable record ``timestamp`` less the earliest, in seconds.
    wall_clock_seconds: float | None = None
    #: ``Bash`` calls that ran ``git commit`` and did not error.
    commits: int = 0
    #: ``Bash`` calls that ran ``gh pr create`` and did not error.
    prs_created: int = 0
    #: ``Bash`` calls that ran ``gh pr merge`` and did not error.
    prs_merged: int = 0
    #: Distinct pull request URLs printed in those calls' results.
    pr_urls: int = 0
    #: The conversation ended on an errored tool result or an API error.
    ended_on_error: bool = False
    #: The conversation ended on an unanswered tool call or an interrupt.
    ended_interrupted: bool = False

    def to_payload(self) -> dict[str, Any]:
        """The pack join's ``outcome`` dict, keyed in field order."""
        return asdict(self)


class OutcomeTally:
    """Builds one transcript's :class:`SessionOutcome`, a record at a time.

    Parse-time scaffolding. Until a ``Bash`` call's result arrives it holds
    what the call's command would make, keyed by the call's use id, and it
    holds the pull request URLs it has seen; both stay in memory, and
    :meth:`finish` returns nothing of them but counts.
    """

    def __init__(self) -> None:
        self._tool_errors = 0
        self._user_turns = 0
        self._made: Counter[str] = Counter()
        self._pr_urls: set[str] = set()
        #: Use id -> the steps its ``Bash`` command would make.
        self._pending: dict[str, frozenset[str]] = {}
        #: Per API message, the largest value each token field reached. Keyed
        #: by ``message.id``, or by a counter for a record without one.
        self._usage: dict[str | int, dict[str, int]] = {}
        self._anonymous = itertools.count()
        self._earliest: float | None = None
        self._latest: float | None = None
        self._state: str | None = None

    def stamp(self, value: object) -> None:
        """Widen the session's span to a record's ``timestamp``, if readable."""
        moment = _parse_stamp(value)
        if moment is None:
            return
        if self._earliest is None or moment < self._earliest:
            self._earliest = moment
        if self._latest is None or moment > self._latest:
            self._latest = moment

    def assistant(self, record: dict[str, Any], message: dict[str, Any]) -> None:
        """Fold one assistant record: its turn, its usage, its ``Bash`` calls."""
        if record.get("isApiErrorMessage"):
            self._state = _API_ERROR
            return
        if message.get("model") == _SYNTHETIC_MODEL:
            return
        message_id = message.get("id")
        key = (
            message_id
            if isinstance(message_id, str) and message_id
            else next(self._anonymous)
        )
        recorded = self._usage.setdefault(key, {})
        usage = message.get("usage")
        if isinstance(usage, dict):
            for name in _TOKEN_FIELDS:
                value = usage.get(name)
                if isinstance(value, int) and not isinstance(value, bool):
                    recorded[name] = max(value, recorded.get(name, value))
        content = message.get("content")
        uses = [
            block
            for block in (content if isinstance(content, list) else [])
            if isinstance(block, dict) and block.get("type") == "tool_use"
        ]
        self._state = _TOOL_USE if uses else _REPLY
        for block in uses:
            self._hold(block)

    def _hold(self, block: dict[str, Any]) -> None:
        """Keep what a ``Bash`` call's command would make until it answers."""
        use_id = block.get("id")
        tool_input = block.get("input")
        if not (
            block.get("name") == "Bash"
            and isinstance(use_id, str)
            and use_id
            and isinstance(tool_input, dict)
        ):
            return
        command = tool_input.get("command")
        steps = _steps(command) if isinstance(command, str) else frozenset()
        if steps:
            self._pending[use_id] = steps

    def user(self, record: dict[str, Any], content: Any, texts: list[str]) -> None:
        """Fold one user record: its text, its tool results, what they made.

        *texts* is the record's natural-language text, as the digest reads it.
        """
        if record.get("isMeta") or record.get("isCompactSummary"):
            return
        results = [
            block
            for block in (content if isinstance(content, list) else [])
            if isinstance(block, dict) and block.get("type") == "tool_result"
        ]
        errored_any = False
        for block in results:
            errored = bool(block.get("is_error"))
            if errored:
                self._tool_errors += 1
                errored_any = True
            use_id = block.get("tool_use_id")
            steps = self._pending.pop(use_id, None) if isinstance(use_id, str) else None
            if steps and not errored:
                self._made.update(steps)
                self._pr_urls.update(_PR_URL.findall(_text_of(block.get("content"))))
        if texts:
            self._user_turns += 1
        if any(text.startswith(_INTERRUPT_MARKER) for text in texts):
            self._state = _INTERRUPT
        elif errored_any:
            self._state = _TOOL_ERROR
        elif results:
            self._state = _TOOL_OK
        elif texts:
            self._state = _USER

    def _total(self, name: str) -> int | None:
        """*name* summed over the messages that recorded it, else ``None``."""
        values = [fields[name] for fields in self._usage.values() if name in fields]
        return sum(values) if values else None

    def finish(self, tool_calls: int) -> SessionOutcome:
        """The outcome of every record folded so far.

        *tool_calls* is the digest's own count of ``tool_use`` blocks, taken
        rather than counted again so that the two cannot disagree.
        """
        wall = (
            round(self._latest - self._earliest, 3)
            if self._earliest is not None and self._latest is not None
            else None
        )
        return SessionOutcome(
            tool_calls=tool_calls,
            tool_errors=self._tool_errors,
            assistant_turns=len(self._usage),
            assistant_turns_with_usage=sum(
                1 for fields in self._usage.values() if fields
            ),
            user_turns=self._user_turns,
            input_tokens=self._total("input_tokens"),
            output_tokens=self._total("output_tokens"),
            cache_read_input_tokens=self._total("cache_read_input_tokens"),
            cache_creation_input_tokens=self._total("cache_creation_input_tokens"),
            wall_clock_seconds=wall,
            commits=self._made[_COMMIT],
            prs_created=self._made[_PR_CREATE],
            prs_merged=self._made[_PR_MERGE],
            pr_urls=len(self._pr_urls),
            ended_on_error=self._state in _ENDED_ON_ERROR,
            ended_interrupted=self._state in _ENDED_INTERRUPTED,
        )


def _steps(command: str) -> frozenset[str]:
    """What a ``Bash`` command line would make: a commit, a PR, a merge.

    A step counts where the shell would run it as a command: the first word
    of a command, after any ``NAME=value`` assignments, with a path to the
    program allowed; for ``git``, the subcommand after its global options.
    So ``echo git commit``, ``git log --grep commit`` and a quoted
    ``'git commit'`` in a ``grep`` count nothing.

    Not a shell parse. A separator inside quotes still splits, so a quoted
    ``; git commit`` counts; a step behind a wrapper (``sudo``, ``env``,
    ``bash -c``, ``xargs``) does not.
    """
    steps: set[str] = set()
    for segment in _COMMAND_BREAK.split(command):
        words = segment.split()
        start = 0
        while start < len(words) and _ENV_ASSIGNMENT.fullmatch(words[start]):
            start += 1
        if start == len(words):
            continue
        program, args = words[start].rsplit("/", 1)[-1], words[start + 1 :]
        if program == "git" and _git_subcommand(args) == "commit":
            steps.add(_COMMIT)
        elif program == "gh" and args[:2] == ["pr", "create"]:
            steps.add(_PR_CREATE)
        elif program == "gh" and args[:2] == ["pr", "merge"]:
            steps.add(_PR_MERGE)
    return frozenset(steps)


def _git_subcommand(args: list[str]) -> str | None:
    """The subcommand of a ``git`` invocation, past its global options."""
    index = 0
    while index < len(args) and args[index].startswith("-"):
        index += 2 if args[index] in _GIT_VALUE_OPTIONS else 1
    return args[index] if index < len(args) else None


def _parse_stamp(value: object) -> float | None:
    """A record ``timestamp`` as epoch seconds; ``None`` if unreadable.

    Claude Code writes ISO 8601 in UTC with a ``Z``. A stamp without a zone
    is read as UTC rather than as the sweep host's local time.
    """
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.timestamp()


def _text_of(content: object) -> str:
    """A tool result's text: a bare string, or its ``text`` blocks joined."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        block["text"]
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    )
