"""A replayed key over a redaction tombstone is refused, not answered ok (#1049).

The idempotency stage answers a seen key DUPLICATE before the handler runs, so a
re-put of content first written under an id that was later redacted read as "in
place" while nothing was written and the tombstone stayed. A handler can now say
a replay is refused through an optional ``replay_refusal`` method, asked only on
a hit, in both arms (the in-memory cache and the event log).

Two layers: the executor's side of the contract against a counting handler, and
the evidence handler's answer against the real guard it must agree with.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from tests.redaction_tombstone import (
    ORIGINAL_TEXT,
    make_tombstone,
    tombstone_metadata,
)
from trellis.core.document_write import (
    REINGEST_STAMP_KEY,
    STAMP_RESERVED_CODE,
    TOMBSTONE_OVERWRITE_CODE,
    TOMBSTONE_OVERWRITE_MESSAGE,
    RedactionTombstoneError,
    is_redaction_tombstone,
    put_document,
)
from trellis.core.vector_metadata import resolve_vector_store
from trellis.errors import StoreError, ValidationError
from trellis.mutate import build_curate_executor
from trellis.mutate.commands import Command, CommandStatus, Operation
from trellis.mutate.evidence_ingest import build_evidence_ingest_command
from trellis.mutate.executor import MutationExecutor
from trellis.mutate.handlers import EvidenceIngestHandler
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry
from trellis.stores.sqlite.event_log import SQLiteEventLog

KEY = "replay-key"
REFUSAL_CODE = "refused_on_replay"


class _CountingHandler:
    """A handler that counts the writes it ran and the replays it was asked about."""

    def __init__(
        self,
        *,
        refusal: ValidationError | None = None,
        raises: Exception | None = None,
    ) -> None:
        self.refusal = refusal
        self.raises = raises
        self.handled = 0
        self.asked = 0

    def handle(self, command: Command) -> tuple[str | None, str]:
        self.handled += 1
        return "created-1", "ok"

    def replay_refusal(self, command: Command) -> ValidationError | None:
        self.asked += 1
        if self.raises is not None:
            raise self.raises
        return self.refusal


class _NoHook:
    """The same handler without the optional method."""

    def __init__(self) -> None:
        self.handled = 0

    def handle(self, command: Command) -> tuple[str | None, str]:
        self.handled += 1
        return "created-1", "ok"


def _cmd(**kwargs: Any) -> Command:
    return Command(
        operation=Operation.ENTITY_CREATE,
        args={"entity_type": "service", "name": "auth"},
        **kwargs,
    )


def _refusal() -> ValidationError:
    return ValidationError("no longer writable", code=REFUSAL_CODE)


def _executor(
    handler: Any, tmp_path: Path, *, name: str = "events.db"
) -> tuple[MutationExecutor, SQLiteEventLog]:
    log = SQLiteEventLog(tmp_path / name)
    return (
        MutationExecutor(event_log=log, handlers={Operation.ENTITY_CREATE: handler}),
        log,
    )


def _without_ids(result: Any) -> dict[str, Any]:
    dumped = result.model_dump()
    dumped.pop("command_id")
    dumped.pop("executed_at")
    return dumped


# --- the executor's side of the contract ------------------------------------


class TestExecutorAsksOnlyOnAHit:
    def test_a_miss_never_asks(self, tmp_path: Path) -> None:
        """A first write, a keyless write and a second distinct key pay nothing."""
        handler = _CountingHandler(refusal=_refusal())
        executor, _ = _executor(handler, tmp_path)

        results = [
            executor.execute(_cmd(idempotency_key="k1")),
            executor.execute(_cmd()),
            executor.execute(_cmd()),
            executor.execute(_cmd(idempotency_key="k2")),
        ]

        assert [r.status for r in results] == [CommandStatus.SUCCESS] * 4
        assert handler.asked == 0
        assert handler.handled == 4

    def test_a_cache_hit_asks_once_and_a_refusal_is_rejected(
        self, tmp_path: Path
    ) -> None:
        """Defective subject: skip the hook in the in-memory arm and the replay
        answers DUPLICATE."""
        handler = _CountingHandler()
        executor, log = _executor(handler, tmp_path)
        assert executor.execute(_cmd(idempotency_key=KEY)).status is (
            CommandStatus.SUCCESS
        )
        handler.refusal = _refusal()

        result = executor.execute(_cmd(idempotency_key=KEY))

        assert handler.asked == 1
        assert handler.handled == 1
        assert result.status is CommandStatus.REJECTED
        assert result.metadata == {"rejection_reason": REFUSAL_CODE}
        assert result.message == "no longer writable"
        rejected = log.get_events(event_type=EventType.MUTATION_REJECTED)
        assert [e.payload["reason"] for e in rejected] == [REFUSAL_CODE]

    def test_a_persisted_hit_asks_once_and_a_refusal_is_rejected(
        self, tmp_path: Path
    ) -> None:
        """A fresh executor over the same event log has an empty cache, so its
        hit is the persisted arm. Defective subject: skip the hook there."""
        first, log = _executor(_CountingHandler(), tmp_path)
        assert first.execute(_cmd(idempotency_key=KEY)).status is (
            CommandStatus.SUCCESS
        )
        handler = _CountingHandler(refusal=_refusal())
        second = MutationExecutor(
            event_log=log, handlers={Operation.ENTITY_CREATE: handler}
        )

        result = second.execute(_cmd(idempotency_key=KEY))

        assert handler.asked == 1
        assert handler.handled == 0
        assert result.status is CommandStatus.REJECTED
        assert result.metadata == {"rejection_reason": REFUSAL_CODE}
        rejected = log.get_events(event_type=EventType.MUTATION_REJECTED)
        assert [e.payload["reason"] for e in rejected] == [REFUSAL_CODE]

    @pytest.mark.parametrize("arm", ["cache", "persisted"])
    def test_none_leaves_the_duplicate_unchanged(
        self, tmp_path: Path, arm: str
    ) -> None:
        """A hook that has no objection answers exactly what no hook answers."""
        results = {}
        for name, make in (("hook", _CountingHandler), ("none", _NoHook)):
            first, log = _executor(make(), tmp_path, name=f"{arm}-{name}.db")
            first.execute(_cmd(idempotency_key=KEY))
            second = (
                first
                if arm == "cache"
                else MutationExecutor(
                    event_log=log, handlers={Operation.ENTITY_CREATE: make()}
                )
            )
            result = second.execute(_cmd(idempotency_key=KEY))
            events = log.get_events(event_type=EventType.MUTATION_REJECTED)
            results[name] = (
                _without_ids(result),
                [
                    {k: v for k, v in e.payload.items() if k != "command_id"}
                    for e in events
                ],
            )

        assert results["hook"][0]["status"] == CommandStatus.DUPLICATE
        assert results["hook"] == results["none"]
        assert results["hook"][1][0]["reason"] == "idempotency_replay"

    def test_a_refused_replay_reads_as_the_handler_s_own_refusal(
        self, tmp_path: Path
    ) -> None:
        """The same ``ValidationError``, raised by ``handle`` on a miss or
        returned by ``replay_refusal`` on a hit, gives the same result and the
        same ``mutation.rejected`` payload, so health and a REST status treat
        them alike."""

        class _Raising(_CountingHandler):
            def handle(self, command: Command) -> tuple[str | None, str]:
                raise _refusal()

        miss_executor, miss_log = _executor(_Raising(), tmp_path, name="miss.db")
        miss = miss_executor.execute(_cmd(idempotency_key=KEY))

        hit_handler = _CountingHandler()
        hit_executor, hit_log = _executor(hit_handler, tmp_path, name="hit.db")
        hit_executor.execute(_cmd(idempotency_key=KEY))
        hit_handler.refusal = _refusal()
        hit = hit_executor.execute(_cmd(idempotency_key=KEY))

        assert miss.status is hit.status is CommandStatus.REJECTED
        assert _without_ids(miss) == _without_ids(hit)

        def payload(log: SQLiteEventLog) -> dict[str, Any]:
            (event,) = log.get_events(event_type=EventType.MUTATION_REJECTED)
            return {k: v for k, v in event.payload.items() if k != "command_id"}

        assert payload(miss_log) == payload(hit_log)

    @pytest.mark.parametrize("event_log", ["with log", "no log"])
    def test_a_refused_replay_does_not_consume_the_key(
        self, tmp_path: Path, event_log: str
    ) -> None:
        """A refused command leaves its key as it found it: once the handler has
        no objection the same key is the duplicate it was.

        With an event log the persisted arm would answer DUPLICATE even if the
        refusal had dropped the key from the cache, so the cache alone is
        pinned with no log. Defective subject: pop the key on a refusal and the
        handler runs a second time."""
        handler = _CountingHandler()
        if event_log == "with log":
            executor, _ = _executor(handler, tmp_path)
        else:
            executor = MutationExecutor(
                event_log=None, handlers={Operation.ENTITY_CREATE: handler}
            )
        executor.execute(_cmd(idempotency_key=KEY))
        handler.refusal = _refusal()
        assert executor.execute(_cmd(idempotency_key=KEY)).status is (
            CommandStatus.REJECTED
        )
        handler.refusal = None

        assert executor.execute(_cmd(idempotency_key=KEY)).status is (
            CommandStatus.DUPLICATE
        )
        assert handler.handled == 1


class TestAHookThatRaisesFailsClosed:
    @pytest.mark.parametrize("arm", ["cache", "persisted"])
    @pytest.mark.parametrize(
        "boom",
        [StoreError("backend down", store="document"), RuntimeError("panic")],
        ids=["typed", "panic"],
    )
    def test_the_replay_fails_with_idempotency_check_failed(
        self, tmp_path: Path, arm: str, boom: Exception
    ) -> None:
        """Defective subject: swallow the hook's exception and fall through to
        DUPLICATE (or to the handler)."""
        first, log = _executor(_CountingHandler(), tmp_path)
        first.execute(_cmd(idempotency_key=KEY))
        handler = _CountingHandler(raises=boom)
        if arm == "cache":
            second = first
            second.register_handler(Operation.ENTITY_CREATE, handler)
        else:
            second = MutationExecutor(
                event_log=log, handlers={Operation.ENTITY_CREATE: handler}
            )

        result = second.execute(_cmd(idempotency_key=KEY))

        assert handler.asked == 1
        assert handler.handled == 0
        assert result.status is CommandStatus.FAILED
        assert result.message == f"Idempotency check failed: {type(boom).__name__}"
        rejected = log.get_events(event_type=EventType.MUTATION_REJECTED)
        assert [e.payload["reason"] for e in rejected] == ["idempotency_check_failed"]


class _Answers(_CountingHandler):
    """A hook whose answer is whatever it was given, refusal or not."""

    def __init__(self, answer: Any) -> None:
        super().__init__()
        self.answer = answer

    def replay_refusal(self, command: Command) -> Any:
        self.asked += 1
        return self.answer


class _BareCode:
    code = REFUSAL_CODE


class TestAnAnswerThatIsNoRefusalFailsClosed:
    @pytest.mark.parametrize("arm", ["cache", "persisted"])
    @pytest.mark.parametrize(
        "answer",
        [
            MagicMock(),
            "refused",
            True,
            False,
            _BareCode(),
            RuntimeError("returned, not raised"),
        ],
        ids=["mock", "str", "True", "False", "object with a code", "returned error"],
    )
    def test_the_replay_fails_with_idempotency_check_failed(
        self, tmp_path: Path, arm: str, answer: Any
    ) -> None:
        """Only ``None`` or a ``ValidationError`` is an answer. Defective
        subject: read ``.code`` off whatever came back and ``execute`` raises
        with no result, no event and a REST 500."""
        first, log = _executor(_CountingHandler(), tmp_path)
        first.execute(_cmd(idempotency_key=KEY))
        handler = _Answers(answer)
        if arm == "cache":
            second = first
            second.register_handler(Operation.ENTITY_CREATE, handler)
        else:
            second = MutationExecutor(
                event_log=log, handlers={Operation.ENTITY_CREATE: handler}
            )

        result = second.execute(_cmd(idempotency_key=KEY))

        assert handler.asked == 1
        assert handler.handled == 0
        assert result.status is CommandStatus.FAILED
        assert result.message == "Idempotency check failed: TypeError"
        rejected = log.get_events(event_type=EventType.MUTATION_REJECTED)
        assert [e.payload["reason"] for e in rejected] == ["idempotency_check_failed"]

    @pytest.mark.parametrize("arm", ["cache", "persisted"])
    def test_a_handler_that_answers_any_attribute_has_no_hook(
        self, tmp_path: Path, arm: str
    ) -> None:
        """A bare mock (or a ``__getattr__`` proxy) answers ``replay_refusal``
        like any name, so asking the instance would read its reply as a
        refusal. The hook is looked up on the type."""

        class _Proxy(_NoHook):
            asked = 0

            def __getattr__(self, name: str) -> Any:
                if name == "replay_refusal":
                    type(self).asked += 1
                    return lambda command: _refusal()
                raise AttributeError(name)

        mock = MagicMock()
        mock.handle.return_value = ("created-1", "ok")
        for handler in (mock, _Proxy()):
            executor, log = _executor(handler, tmp_path, name=f"{id(handler)}.db")
            executor.execute(_cmd(idempotency_key=KEY))
            replayer = (
                executor
                if arm == "cache"
                else MutationExecutor(
                    event_log=log, handlers={Operation.ENTITY_CREATE: handler}
                )
            )

            result = replayer.execute(_cmd(idempotency_key=KEY))

            assert result.status is CommandStatus.DUPLICATE
        assert _Proxy.asked == 0


# --- the evidence handler against the real guard ----------------------------

DOC = "doc-replay"
NEW_TEXT = "Quartz harbor rewritten text"
FORGED = {REINGEST_STAMP_KEY: "2020-01-01T00:00:00+00:00"}


@pytest.fixture
def registry(tmp_path: Path):
    stores_dir = tmp_path / "stores"
    stores_dir.mkdir()
    reg = StoreRegistry(stores_dir=stores_dir)
    yield reg
    reg.close()


def _evidence_command(
    *,
    content: str = NEW_TEXT,
    metadata: dict[str, Any] | None = None,
    flag: Any = False,
) -> Command:
    return build_evidence_ingest_command(
        doc_id=DOC,
        content=content,
        metadata=metadata,
        reingest_redacted=flag,
        requested_by="test:replay",
        idempotency_key=KEY,
    )


def _seed(registry: StoreRegistry, state: str) -> None:
    store = registry.knowledge.document_store
    if state == "tombstone":
        make_tombstone(store, resolve_vector_store(registry), DOC)
    elif state == "live":
        put_document(store, None, DOC, ORIGINAL_TEXT, {}, preserve_updated_at=False)


# (state of the stored row, command) pairs; the hook must answer as the guard
# does for each, read off ``put_document`` itself rather than restated here.
CASES = {
    "live row": ("live", _evidence_command()),
    "tombstone, unflagged": ("tombstone", _evidence_command()),
    "tombstone, flagged": ("tombstone", _evidence_command(flag=True)),
    "no row": ("absent", _evidence_command()),
    "tombstone, blank write keeping the stamp": (
        "tombstone",
        _evidence_command(content="", metadata=tombstone_metadata()),
    ),
    "tombstone, blank write dropping the stamp": (
        "tombstone",
        _evidence_command(content="   "),
    ),
    "tombstone, forged re-ingest stamp": (
        "tombstone",
        _evidence_command(metadata=FORGED),
    ),
    "live row, forged re-ingest stamp": ("live", _evidence_command(metadata=FORGED)),
    "no row, forged re-ingest stamp": ("absent", _evidence_command(metadata=FORGED)),
    "tombstone, flagged, stale re-ingest stamp": (
        "tombstone",
        _evidence_command(metadata=FORGED, flag=True),
    ),
}

# Where the guard's stamp check refuses a write that is not a replay over a
# tombstone, the hook still answers None: nothing was lost, so the replay is a
# true duplicate, and a flagged replay is left to it by design.
STAMP_REFUSED_BUT_REPLAY_IS_A_DUPLICATE = (
    "live row, forged re-ingest stamp",
    "no row, forged re-ingest stamp",
    "tombstone, flagged, stale re-ingest stamp",
)


def _guard_code(registry: StoreRegistry, command: Command) -> str | None:
    """The code ``put_document`` refuses ``command`` with now, else ``None``."""
    evidence = command.args["evidence"]
    try:
        put_document(
            registry.knowledge.document_store,
            None,
            DOC,
            evidence["content"],
            dict(evidence["metadata"]),
            preserve_updated_at=False,
            reingest_redacted=evidence.get("reingest_redacted") is True,
        )
    except RedactionTombstoneError as exc:
        return exc.code
    return None


@pytest.mark.parametrize("case", CASES, ids=list(CASES))
def test_the_hook_refuses_exactly_what_put_document_would(
    registry: StoreRegistry, case: str
) -> None:
    """Over an unflagged tombstone the hook answers as the guard does, in both
    refusal codes; anywhere else it answers ``None`` (next test). Defective
    subject: invert the handler's predicate, or compare a different pair of
    fields than the guard does, and a case below disagrees."""
    state, command = CASES[case]
    _seed(registry, state)
    store = registry.knowledge.document_store
    before = store.get(DOC)
    handler = EvidenceIngestHandler(registry)

    answer = handler.replay_refusal(command)

    assert store.get(DOC) == before, "the hook read and wrote"
    expected = (
        None
        if case in STAMP_REFUSED_BUT_REPLAY_IS_A_DUPLICATE
        else _guard_code(registry, command)
    )
    assert (answer.code if answer is not None else None) == expected
    if answer is not None:
        assert isinstance(answer, RedactionTombstoneError)


@pytest.mark.parametrize("case", STAMP_REFUSED_BUT_REPLAY_IS_A_DUPLICATE)
def test_a_stamp_the_guard_refuses_is_no_refusal_of_a_replay_that_lost_nothing(
    registry: StoreRegistry, case: str
) -> None:
    """The guard's stamp check refuses a forged stamp on any write, tombstone or
    not. A replay over a live row or an absent one lost nothing, so answering
    its refusal would turn a true duplicate into a rejection. Defective subject:
    run the guard in full on whatever the row is."""
    state, command = CASES[case]
    _seed(registry, state)
    assert _guard_code(registry, command) == STAMP_RESERVED_CODE

    assert EvidenceIngestHandler(registry).replay_refusal(command) is None


def test_the_refusal_is_the_guard_s_own_words(registry: StoreRegistry) -> None:
    _seed(registry, "tombstone")

    answer = EvidenceIngestHandler(registry).replay_refusal(_evidence_command())

    assert answer is not None
    assert answer.code == TOMBSTONE_OVERWRITE_CODE
    assert str(answer) == TOMBSTONE_OVERWRITE_MESSAGE
    assert ORIGINAL_TEXT not in str(answer)
    forged = EvidenceIngestHandler(registry).replay_refusal(
        _evidence_command(metadata=FORGED)
    )
    assert forged is not None
    assert forged.code == STAMP_RESERVED_CODE


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"evidence": "not a mapping"},
        {"evidence": {"content": NEW_TEXT}},
        {"evidence": {"doc_id": 7, "content": NEW_TEXT}},
        {"evidence": {"doc_id": DOC, "content": None}},
    ],
    ids=["no evidence", "not a mapping", "no doc id", "doc id not a string", "no text"],
)
def test_a_command_the_hook_cannot_read_is_left_to_the_duplicate(
    registry: StoreRegistry, args: dict[str, Any]
) -> None:
    _seed(registry, "tombstone")
    command = Command(
        operation=Operation.EVIDENCE_INGEST,
        args=args,
        requested_by="test:replay",
        idempotency_key=KEY,
    )

    assert EvidenceIngestHandler(registry).replay_refusal(command) is None


@pytest.mark.parametrize("flag", ["yes", 1, [True]], ids=repr)
def test_only_a_true_flag_counts_as_flagged(registry: StoreRegistry, flag: Any) -> None:
    """``handle`` refuses a non-boolean flag for its shape; over a tombstone the
    replay is refused too, rather than read as the flagged write it resembles."""
    _seed(registry, "tombstone")

    answer = EvidenceIngestHandler(registry).replay_refusal(
        _evidence_command(flag=flag)
    )

    assert answer is not None
    assert answer.code == TOMBSTONE_OVERWRITE_CODE


def test_a_store_read_that_raises_propagates() -> None:
    """Not swallowed here: the executor fails the command closed."""
    broken = MagicMock()
    broken.knowledge.document_store.get.side_effect = StoreError(
        "document store down", store="document"
    )

    with pytest.raises(StoreError):
        EvidenceIngestHandler(broken).replay_refusal(_evidence_command())


# --- end to end through the executor the surfaces build ---------------------


def _redact(registry: StoreRegistry) -> None:
    """Blank ``DOC`` as a redaction does, after a write went through the executor."""
    put_document(
        registry.knowledge.document_store,
        resolve_vector_store(registry),
        DOC,
        "",
        tombstone_metadata(),
        preserve_updated_at=False,
    )
    assert is_redaction_tombstone(registry.knowledge.document_store.get(DOC))


def _derived(flag: Any = False) -> Command:
    """The command a REST route builds: a derived key, no caller key."""
    return build_evidence_ingest_command(
        doc_id=DOC,
        content=NEW_TEXT,
        metadata={"a": 1},
        reingest_redacted=flag,
        requested_by="test:replay",
    )


@pytest.mark.parametrize("arm", ["cache", "persisted"])
def test_a_replay_over_a_tombstone_is_refused_and_nothing_is_written(
    registry: StoreRegistry, arm: str
) -> None:
    executor = build_curate_executor(registry, clearance="restricted")
    assert executor.execute(_derived()).status is CommandStatus.SUCCESS
    _redact(registry)
    # A new executor has an empty cache: its hit is the persisted arm.
    replayer = (
        executor
        if arm == "cache"
        else build_curate_executor(registry, clearance="restricted")
    )

    result = replayer.execute(_derived())

    assert result.status is CommandStatus.REJECTED
    assert result.metadata == {"rejection_reason": TOMBSTONE_OVERWRITE_CODE}
    assert result.message == TOMBSTONE_OVERWRITE_MESSAGE
    row = registry.knowledge.document_store.get(DOC)
    assert is_redaction_tombstone(row)
    assert REINGEST_STAMP_KEY not in row["metadata"]
    events = registry.operational.event_log.get_events(
        event_type=EventType.MUTATION_REJECTED
    )
    assert [e.payload["reason"] for e in events] == [TOMBSTONE_OVERWRITE_CODE]


@pytest.mark.parametrize("arm", ["cache", "persisted"])
def test_a_replay_over_a_live_row_stays_a_duplicate(
    registry: StoreRegistry, arm: str
) -> None:
    executor = build_curate_executor(registry, clearance="restricted")
    executor.execute(_derived())
    replayer = (
        executor
        if arm == "cache"
        else build_curate_executor(registry, clearance="restricted")
    )

    result = replayer.execute(_derived())

    assert result.status is CommandStatus.DUPLICATE
    assert registry.knowledge.document_store.get(DOC)["content"] == NEW_TEXT


@pytest.mark.parametrize("arm", ["cache", "persisted"])
def test_a_flagged_replay_stays_a_duplicate_and_writes_nothing(
    registry: StoreRegistry, arm: str
) -> None:
    """The flagged key digests the flag, so this replays a write that already put
    content back over the tombstone; a replay writes nothing, so a row redacted
    again since is still a tombstone and the answer is still DUPLICATE."""
    make_tombstone(
        registry.knowledge.document_store, resolve_vector_store(registry), DOC
    )
    executor = build_curate_executor(registry, clearance="restricted")
    assert executor.execute(_derived(flag=True)).status is CommandStatus.SUCCESS
    _redact(registry)
    replayer = (
        executor
        if arm == "cache"
        else build_curate_executor(registry, clearance="restricted")
    )

    result = replayer.execute(_derived(flag=True))

    assert result.status is CommandStatus.DUPLICATE
    assert is_redaction_tombstone(registry.knowledge.document_store.get(DOC))


@pytest.mark.parametrize("arm", ["cache", "persisted"])
def test_a_replay_of_a_write_that_carried_the_rows_own_stamp_is_a_duplicate(
    registry: StoreRegistry, arm: str
) -> None:
    """The row is live again and a later write dropped the stamp the replayed
    command carries. Nothing is lost, so the replay is the duplicate it was and
    not the guard's ``reingest_stamp_reserved``. Defective subject: run the
    guard's stamp check on a row that is not a tombstone."""
    store = registry.knowledge.document_store
    make_tombstone(store, resolve_vector_store(registry), DOC)
    executor = build_curate_executor(registry, clearance="restricted")

    def write(
        content: str,
        *,
        key: str,
        metadata: dict[str, Any] | None = None,
        flag: bool = False,
    ) -> Command:
        return build_evidence_ingest_command(
            doc_id=DOC,
            content=content,
            metadata=metadata,
            reingest_redacted=flag,
            requested_by="test:replay",
            idempotency_key=key,
        )

    # 1-2. Tombstone, then the flagged re-ingest that stamps the row.
    assert (
        executor.execute(write("first", key="k-flag", flag=True)).status
        is CommandStatus.SUCCESS
    )
    stamp = store.get(DOC)["metadata"][REINGEST_STAMP_KEY]
    # 3. A refresh that carries the row's own stamp.
    refresh = write("refreshed", key="k-refresh", metadata={REINGEST_STAMP_KEY: stamp})
    assert executor.execute(refresh).status is CommandStatus.SUCCESS
    # 4. A rebuild without the stamp: the row is live and no longer holds it.
    assert (
        executor.execute(write("rebuilt", key="k-rebuild")).status
        is CommandStatus.SUCCESS
    )
    row = store.get(DOC)
    assert not is_redaction_tombstone(row)
    assert REINGEST_STAMP_KEY not in row["metadata"]
    replayer = (
        executor
        if arm == "cache"
        else build_curate_executor(registry, clearance="restricted")
    )

    # 5. Step 3 again, same key.
    result = replayer.execute(refresh)

    assert result.status is CommandStatus.DUPLICATE
    assert store.get(DOC) == row
    events = registry.operational.event_log.get_events(
        event_type=EventType.MUTATION_REJECTED
    )
    assert [e.payload["reason"] for e in events] == ["idempotency_replay"]


def test_the_flagged_and_unflagged_writes_do_not_share_a_key() -> None:
    """The premise the flagged-replay decision rests on."""
    assert _derived().idempotency_key != _derived(flag=True).idempotency_key
