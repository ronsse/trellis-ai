"""The withholding note has to reach the surface an agent reads (#404).

The core tests (``tests/unit/retrieve/test_withholding.py``) pin the
summary and the formatters. This module pins the wiring, because a note
that exists in ``PACK_ASSEMBLED`` and in a formatter parameter nobody
passes is exactly the "honest in JSON alone" failure the issue is about.

Every ``get_*`` context tool is exercised separately: they do **not** share
one renderer — the flat tools go through ``format_pack_as_markdown`` (or
the index renderer), the sectioned ones through
``format_sectioned_pack_as_markdown``, and the empty-pack case returns a
bare string that touches no formatter at all.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.unit.mcp.conftest import unwrap_tool
from trellis.mcp.server import get_context as _get_context
from trellis.mcp.server import get_objective_context as _get_objective_context
from trellis.mcp.server import get_task_context as _get_task_context
from trellis.stores.base.event_log import EventType

if TYPE_CHECKING:
    from trellis.stores.registry import StoreRegistry

get_context = unwrap_tool(_get_context)
get_objective_context = unwrap_tool(_get_objective_context)
get_task_context = unwrap_tool(_get_task_context)

INTENT = "failover runbook"

#: Per-document filler that makes each seeded body genuinely distinct.
#: Near-identical bodies are collapsed by the pack's semantic dedup, which
#: would make these assertions measure dedup instead of the noise gate.
_SUBJECTS = (
    "replication lag",
    "connection pooling",
    "vacuum scheduling",
    "wal shipping",
    "index bloat",
    "checkpoint tuning",
)

#: Prose that only ever belongs to a demoted document.
_NOISE_SUBJECTS = (
    "kangaroo pouch telemetry",
    "wombat burrow scratch notes",
    "platypus bill calibration",
)


def _body(subject: str) -> str:
    return (
        f"failover runbook for {subject}: drain the write queue, promote "
        f"the replica, then restart the {subject} sidecar. "
    ) * 6


def _seed(registry: StoreRegistry, count: int, *, noise: int = 0) -> None:
    store = registry.knowledge.document_store
    for i in range(count):
        store.put(f"doc-{i}", _body(_SUBJECTS[i]), {"title": f"Runbook {i}"})
    for i in range(noise):
        store.put(
            f"noise-{i}",
            _body(_NOISE_SUBJECTS[i]),
            {"content_tags": {"signal_quality": "noise"}, "title": f"Scratch {i}"},
        )


class TestAnAllWithheldPackDoesNotReadAsGreenfield:
    """The single most misleading pack this server can return.

    ``get_context`` short-circuits on an empty pack with "No context found
    for: …", which is the correct answer for an empty corpus and a false
    one for a corpus whose every match was demoted. Both rendered the same
    string, so an agent could not tell a greenfield repo from a redacted
    one — and an empty pack is precisely where a caller acts on the
    difference.
    """

    def test_the_empty_pack_says_what_it_withheld(
        self, temp_registry: StoreRegistry
    ) -> None:
        _seed(temp_registry, 0, noise=3)
        result = get_context(INTENT)

        assert "No context found" in result
        assert "**Withheld:** 3 items" in result
        assert "noise 3" in result

    def test_a_genuinely_empty_corpus_still_reads_as_empty(
        self, temp_registry: StoreRegistry
    ) -> None:
        """The other half. A caveat that always prints is one that always
        gets skipped."""
        result = get_context(INTENT)
        assert "No context found" in result
        assert "Withheld" not in result

    def test_the_empty_pack_names_no_withheld_id(
        self, temp_registry: StoreRegistry
    ) -> None:
        _seed(temp_registry, 0, noise=2)
        result = get_context(INTENT)
        assert "noise-0" not in result
        assert "kangaroo" not in result


class TestEachPackSurfaceRendersTheNote:
    def test_get_context_renders_it_above_the_items(
        self, temp_registry: StoreRegistry
    ) -> None:
        _seed(temp_registry, 3, noise=2)
        result = get_context(INTENT)

        assert "**Withheld:** 2 items" in result
        assert result.index("**Withheld:**") < result.index("## [document]")

    def test_get_context_index_mode_renders_it(
        self, temp_registry: StoreRegistry
    ) -> None:
        _seed(temp_registry, 3, noise=2)
        result = get_context(INTENT, index=True)
        assert "**Withheld:** 2 items" in result

    def test_get_task_context_renders_it(self, temp_registry: StoreRegistry) -> None:
        """Sectioned path — a different builder method and a different
        formatter, so it needs its own wire."""
        _seed(temp_registry, 3, noise=2)
        result = get_task_context(INTENT)
        assert "**Withheld:**" in result
        assert "noise 2" in result

    def test_get_objective_context_renders_it(
        self, temp_registry: StoreRegistry
    ) -> None:
        _seed(temp_registry, 3, noise=2)
        result = get_objective_context(INTENT)
        assert "**Withheld:**" in result
        assert "noise 2" in result

    def test_a_pack_that_withheld_nothing_carries_no_note(
        self, temp_registry: StoreRegistry
    ) -> None:
        _seed(temp_registry, 3)
        assert "Withheld" not in get_context(INTENT)


class TestTheMarkerIsNotOnlyInTheEventPayload:
    def test_the_note_is_on_the_read_surface_and_the_ids_are_not(
        self, temp_registry: StoreRegistry
    ) -> None:
        """The two audiences, and the line between them.

        ``PACK_ASSEMBLED`` is an operator surface: it names the withheld
        ids so a curator can inspect them. The rendered pack is an agent
        surface: counts and reasons only, because naming the ids invites a
        re-fetch of precisely what a gate decided not to serve.

        This test fails if the marker stops reaching the rendered surface
        while the payload keeps looking healthy — the shape in which this
        fix would most plausibly rot.
        """
        _seed(temp_registry, 3, noise=2)
        result = get_context(INTENT)

        payload = temp_registry.operational.event_log.get_events(
            event_type=EventType.PACK_ASSEMBLED, limit=1
        )[0].payload

        assert sorted(payload["withholding"]["withheld_item_ids"]) == [
            "noise-0",
            "noise-1",
        ]
        assert "**Withheld:** 2 items" in result
        assert "noise-0" not in result
        assert "kangaroo" not in result
