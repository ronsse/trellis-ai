"""Shared harness: real SQLite stores in a tmp config dir, injected embedder.

Same shape as ``tests/unit/cli/test_admin_reindex_vectors.py`` — the registry
resolves ``TRELLIS_EMBEDDING_FN`` (a dotted path) before any provider extra, so
the embedder below needs no network and no optional dependency.

The embedder is *scriptable*: :class:`EmbedRecorder` records every text it was
handed and can be told to start raising. Both halves matter. The recording is
how "did this pass double-embed anything?" becomes an assertion rather than an
inference from a summary the code under test wrote itself; the failure switch is
how an interruption is staged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from typer.testing import CliRunner

from trellis.schemas.enums import OutcomeStatus, TraceSource
from trellis.schemas.trace import Outcome, Trace, TraceContext, TraceStep
from trellis_cli.admin import admin_app
from trellis_cli.stores import _get_registry, _reset_registry

EMBED_FN_PATH = "tests.unit.workers.trace_embed.conftest.embed"

runner = CliRunner()


@dataclass
class EmbedRecorder:
    """Deterministic embedder that remembers what it embedded."""

    texts: list[str] = field(default_factory=list)
    #: Raise on every call once this many calls have succeeded (0 = never).
    fail_after: int = 0

    def __call__(self, text: str) -> list[float]:
        if self.fail_after and len(self.texts) >= self.fail_after:
            msg = "embedder unavailable (staged)"
            raise RuntimeError(msg)
        self.texts.append(text)
        # Length-derived so different traces get different vectors, and a
        # query built from a trace's own text lands nearest to that trace.
        return [1.0, float(len(text) % 97) / 97.0, float(text.count("e") % 53) / 53.0]

    def calls_containing(self, needle: str) -> int:
        """How many embed calls saw *needle*.

        The seeded intents use a zero-padded, delimited id
        (``widget-0001``) precisely so this is not a prefix match:
        ``widget 1`` would also match ``widget 10``, and the resulting
        "embedded 3 times" would be an artefact of the assertion rather
        than a defect in the code.
        """
        return sum(1 for t in self.texts if needle in t)


#: Module-level singleton, because ``TRELLIS_EMBEDDING_FN`` resolves a dotted
#: path and cannot carry a closure. Reset by the ``recorder`` fixture.
RECORDER = EmbedRecorder()


def embed(text: str) -> list[float]:
    return RECORDER(text)


@pytest.fixture
def recorder() -> EmbedRecorder:
    RECORDER.texts = []
    RECORDER.fail_after = 0
    return RECORDER


@pytest.fixture
def registry(tmp_path, monkeypatch, recorder):
    """Initialised SQLite stores with the scriptable embedder wired in."""
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("TRELLIS_EMBEDDING_FN", EMBED_FN_PATH)
    init = runner.invoke(admin_app, ["init"])
    assert init.exit_code == 0, init.output
    _reset_registry()
    yield _get_registry()
    _reset_registry()


@pytest.fixture
def watermark_path(tmp_path):
    return tmp_path / "trace-embed-watermark.json"


def make_trace(
    n: int,
    *,
    base: datetime | None = None,
    intent: str | None = None,
    summary: str | None = None,
    error: str | None = None,
    domain: str = "platform",
    status: OutcomeStatus = OutcomeStatus.SUCCESS,
) -> Trace:
    """A trace with a deterministic id and a timestamp ordered by *n*."""
    start = base or datetime(2026, 8, 1, tzinfo=UTC)
    created = start + timedelta(minutes=n)
    steps = []
    if error is not None:
        steps.append(TraceStep(step_type="tool_call", name=f"step-{n}", error=error))
    return Trace(
        trace_id=f"trace-{n:04d}",
        source=TraceSource.AGENT,
        intent=intent or f"Investigate widget-{n:04d}",
        steps=steps,
        outcome=Outcome(status=status, summary=summary or f"Finished widget {n}"),
        context=TraceContext(domain=domain, started_at=created),
        created_at=created,
        updated_at=created,
    )


def seed_traces(registry, count: int, **kwargs) -> list[Trace]:
    traces = [make_trace(n, **kwargs) for n in range(count)]
    for trace in traces:
        registry.operational.trace_store.append(trace)
    return traces
