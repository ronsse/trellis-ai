"""A failed memory-extractor resolution is retried, bounded, and reported (#987).

``_get_memory_extractor`` used to cache ``None`` for the life of the process
when no LLM client could be built on first use, so one transient failure at
startup turned extraction off until a restart, with the extraction flag set and
only an ``info`` log line to show for it.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from trellis.mcp import server as server_mod
from trellis.mcp.server import McpError, save_memory
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry


class _FakeLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, **kwargs: Any) -> Any:
        from trellis.llm.types import LLMResponse, TokenUsage

        self.calls += 1
        return LLMResponse(
            content='{"entities": [], "edges": []}',
            model="fake",
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        )


class _Clock:
    """``time.monotonic`` plus an offset the test advances."""

    def __init__(self) -> None:
        self.offset = 0.0
        self._real = time.monotonic

    def __call__(self) -> float:
        return self._real() + self.offset


def _reset() -> None:
    server_mod._memory_extractor = None
    server_mod._memory_extractor_attempted = False
    server_mod._memory_extractor_retry_at = None
    server_mod._memory_extractor_reported = None
    server_mod._memory_extractor_reported_at = 0.0


@pytest.fixture(autouse=True)
def _fresh_cache() -> Any:
    _reset()
    yield
    _reset()


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    clock = _Clock()
    monkeypatch.setattr(server_mod.time, "monotonic", clock)
    return clock


@pytest.fixture
def flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRELLIS_ENABLE_MEMORY_EXTRACTION", "1")


class _Client:
    """A swappable ``_build_llm_client`` stand-in that counts its calls."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.client: Any = None
        self.builds = 0
        monkeypatch.setattr(server_mod, "_build_llm_client", self._build)

    def _build(self, _registry: Any, _consumer: Any) -> Any:
        self.builds += 1
        return self.client


@pytest.fixture
def llm(monkeypatch: pytest.MonkeyPatch) -> _Client:
    return _Client(monkeypatch)


def _states(registry: StoreRegistry) -> list[dict[str, Any]]:
    events = registry.operational.event_log.get_events(
        event_type=EventType.MEMORY_EXTRACTION_STATE, limit=100, order="asc"
    )
    return [e.payload for e in events]


@pytest.mark.usefixtures("flag_on", "clock")
class TestRetry:
    def test_a_failed_resolution_is_retried_once_the_interval_passes(
        self,
        temp_registry: StoreRegistry,
        llm: _Client,
        clock: _Clock,
    ) -> None:
        assert server_mod._get_memory_extractor(temp_registry) is None
        llm.client = _FakeLLM()

        # Within the interval nothing is rebuilt, so the write path pays one
        # float comparison per save and not an LLM-client build.
        for _ in range(25):
            assert server_mod._get_memory_extractor(temp_registry) is None
        assert llm.builds == 1

        clock.offset = server_mod._MEMORY_EXTRACTOR_RETRY_SECONDS + 1

        assert server_mod._get_memory_extractor(temp_registry) is not None
        assert llm.builds == 2

    def test_a_still_failing_retry_waits_a_whole_interval_again(
        self,
        temp_registry: StoreRegistry,
        llm: _Client,
        clock: _Clock,
    ) -> None:
        interval = server_mod._MEMORY_EXTRACTOR_RETRY_SECONDS
        server_mod._get_memory_extractor(temp_registry)

        clock.offset = interval + 1
        assert server_mod._get_memory_extractor(temp_registry) is None
        assert llm.builds == 2

        clock.offset = interval + 2  # inside the new interval
        assert server_mod._get_memory_extractor(temp_registry) is None
        assert llm.builds == 2

        clock.offset = 2 * interval + 2
        server_mod._get_memory_extractor(temp_registry)
        assert llm.builds == 3

    def test_a_built_extractor_is_not_rebuilt(
        self, temp_registry: StoreRegistry, llm: _Client, clock: _Clock
    ) -> None:
        llm.client = _FakeLLM()
        first = server_mod._get_memory_extractor(temp_registry)

        clock.offset = 10 * server_mod._MEMORY_EXTRACTOR_RETRY_SECONDS

        assert first is not None
        assert server_mod._get_memory_extractor(temp_registry) is first
        assert llm.builds == 1

    def test_flag_off_is_settled_and_never_builds_a_client(
        self,
        monkeypatch: pytest.MonkeyPatch,
        temp_registry: StoreRegistry,
        llm: _Client,
        clock: _Clock,
    ) -> None:
        monkeypatch.delenv("TRELLIS_ENABLE_MEMORY_EXTRACTION", raising=False)

        assert server_mod._get_memory_extractor(temp_registry) is None
        clock.offset = 10 * server_mod._MEMORY_EXTRACTOR_RETRY_SECONDS
        assert server_mod._get_memory_extractor(temp_registry) is None

        assert llm.builds == 0
        assert _states(temp_registry) == []

    def test_the_flag_going_off_ends_the_retries(
        self,
        monkeypatch: pytest.MonkeyPatch,
        temp_registry: StoreRegistry,
        llm: _Client,
        clock: _Clock,
    ) -> None:
        server_mod._get_memory_extractor(temp_registry)
        assert llm.builds == 1

        monkeypatch.delenv("TRELLIS_ENABLE_MEMORY_EXTRACTION", raising=False)
        clock.offset = server_mod._MEMORY_EXTRACTOR_RETRY_SECONDS + 1
        assert server_mod._get_memory_extractor(temp_registry) is None
        clock.offset = 5 * server_mod._MEMORY_EXTRACTOR_RETRY_SECONDS

        assert server_mod._get_memory_extractor(temp_registry) is None
        assert llm.builds == 1

    def test_save_memory_picks_extraction_up_after_the_outage(
        self,
        temp_registry: StoreRegistry,
        llm: _Client,
        clock: _Clock,
    ) -> None:
        assert save_memory("first note, saved while the route is down").startswith(
            "Memory saved:"
        )
        fake = _FakeLLM()
        llm.client = fake
        assert save_memory("second note, still inside the interval").startswith(
            "Memory saved:"
        )
        assert fake.calls == 0

        clock.offset = server_mod._MEMORY_EXTRACTOR_RETRY_SECONDS + 1
        assert save_memory("third note, after the interval").startswith("Memory saved:")

        assert fake.calls == 1


@pytest.mark.usefixtures("flag_on", "clock")
class TestStateEvents:
    def test_unavailable_is_written_once_per_state_not_per_retry(
        self,
        temp_registry: StoreRegistry,
        llm: _Client,
        clock: _Clock,
    ) -> None:
        interval = server_mod._MEMORY_EXTRACTOR_RETRY_SECONDS
        for step in range(4):
            clock.offset = step * (interval + 1)
            server_mod._get_memory_extractor(temp_registry)

        assert llm.builds == 4
        assert _states(temp_registry) == [
            {"state": "unavailable", "reason": "no_llm_client", "error_type": None}
        ]

    def test_recovery_writes_available_once(
        self,
        temp_registry: StoreRegistry,
        llm: _Client,
        clock: _Clock,
    ) -> None:
        server_mod._get_memory_extractor(temp_registry)
        llm.client = _FakeLLM()
        clock.offset = server_mod._MEMORY_EXTRACTOR_RETRY_SECONDS + 1
        for _ in range(5):
            server_mod._get_memory_extractor(temp_registry)

        assert [row["state"] for row in _states(temp_registry)] == [
            "unavailable",
            "available",
        ]

    def test_a_healthy_first_build_leaves_positive_evidence(
        self, temp_registry: StoreRegistry, llm: _Client
    ) -> None:
        # A restart that fixes the problem must be able to clear the warning,
        # and nothing else would write the row that does.
        llm.client = _FakeLLM()

        server_mod._get_memory_extractor(temp_registry)
        server_mod._get_memory_extractor(temp_registry)

        assert _states(temp_registry) == [
            {"state": "available", "reason": None, "error_type": None}
        ]

    def test_a_persistent_unavailable_is_refreshed_after_a_day(
        self,
        temp_registry: StoreRegistry,
        llm: _Client,
        clock: _Clock,
    ) -> None:
        server_mod._get_memory_extractor(temp_registry)
        clock.offset = server_mod._MEMORY_EXTRACTOR_REFRESH_SECONDS - 10
        server_mod._get_memory_extractor(temp_registry)
        assert len(_states(temp_registry)) == 1

        # Past the day, and past the retry interval the call above restarted.
        clock.offset = server_mod._MEMORY_EXTRACTOR_REFRESH_SECONDS + 100
        server_mod._get_memory_extractor(temp_registry)

        assert [row["state"] for row in _states(temp_registry)] == [
            "unavailable",
            "unavailable",
        ]

    def test_a_row_that_did_not_land_is_written_on_the_next_retry(
        self,
        monkeypatch: pytest.MonkeyPatch,
        temp_registry: StoreRegistry,
        llm: _Client,
        clock: _Clock,
    ) -> None:
        event_log = temp_registry.operational.event_log
        real_emit = event_log.emit
        down = {"on": True}

        def flaky(event_type: Any, *args: Any, **kwargs: Any) -> Any:
            if down["on"] and event_type == EventType.MEMORY_EXTRACTION_STATE:
                msg = "event log down"
                raise OSError(msg)
            return real_emit(event_type, *args, **kwargs)

        monkeypatch.setattr(event_log, "emit", flaky)

        assert server_mod._get_memory_extractor(temp_registry) is None
        assert _states(temp_registry) == []

        down["on"] = False
        clock.offset = server_mod._MEMORY_EXTRACTOR_RETRY_SECONDS + 1
        server_mod._get_memory_extractor(temp_registry)

        assert [row["state"] for row in _states(temp_registry)] == ["unavailable"]

    def test_nothing_escaping_the_report_reaches_the_caller(
        self,
        monkeypatch: pytest.MonkeyPatch,
        temp_registry: StoreRegistry,
        llm: _Client,
    ) -> None:
        # Advisory step on the write path: wrapped whole, so even an exception
        # type nobody listed is swallowed and the save is untouched.
        def explode(*args: Any, **kwargs: Any) -> None:
            msg = "unexpected"
            raise RuntimeError(msg)

        monkeypatch.setattr(server_mod, "emit_memory_extraction_state", explode)

        assert server_mod._get_memory_extractor(temp_registry) is None
        assert save_memory("saved although the report raised").startswith(
            "Memory saved:"
        )

    def test_a_construction_failure_raises_once_is_reported_and_settles(
        self,
        monkeypatch: pytest.MonkeyPatch,
        temp_registry: StoreRegistry,
        llm: _Client,
        clock: _Clock,
    ) -> None:
        import trellis.extract.save_memory as save_memory_mod

        def broken(**kwargs: Any) -> None:
            msg = "boom"
            raise ValueError(msg)

        llm.client = _FakeLLM()
        monkeypatch.setattr(save_memory_mod, "build_save_memory_extractor", broken)

        with pytest.raises(McpError):
            server_mod._get_memory_extractor(temp_registry)
        clock.offset = 10 * server_mod._MEMORY_EXTRACTOR_RETRY_SECONDS
        # Settled: a defect is not the transient cause the retry is for.
        assert server_mod._get_memory_extractor(temp_registry) is None

        assert llm.builds == 1
        assert _states(temp_registry) == [
            {
                "state": "unavailable",
                "reason": "build_failed",
                "error_type": "ValueError",
            }
        ]
