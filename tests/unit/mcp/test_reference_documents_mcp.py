"""``save_memory(summary=, uri=)`` and ``get_items`` for reference documents (#1250).

Text here is invented.
"""

from __future__ import annotations

from typing import Any

import pytest
from mcp.shared.exceptions import MCPError

import trellis.mcp.server as server_mod
from tests.unit.mcp.conftest import unwrap_tool
from trellis.retrieve.embed_ingest_hook import EMBED_INPUT_CHAR_CAP
from trellis.stores.base.event_log import EventType
from trellis.stores.registry import StoreRegistry

save_memory = unwrap_tool(server_mod.save_memory)
get_items = unwrap_tool(server_mod.get_items)

BODY = "The kiln handbook opens here. " + ("Firing schedules go on at length. " * 600)
SUMMARY = "Invented handbook for the quasar kiln: schedules and safety checks."
URI = "https://docs.example.test/kiln/handbook"


def _saved_id(reply: str) -> str:
    return reply.split("Memory saved: ", maxsplit=1)[1].split(maxsplit=1)[0]


def _count(registry: StoreRegistry) -> int:
    return registry.knowledge.document_store.count(include_chunks=True)


def _rejections(registry: StoreRegistry) -> list[Any]:
    return [
        event
        for event in registry.operational.event_log.get_events(
            event_type=EventType.WRITE_REJECTED, limit=50
        )
        if event.source == "mcp:save_memory"
    ]


class TestSaveMemory:
    def test_stores_both_keys_and_stamps_the_form(
        self, temp_registry: StoreRegistry
    ) -> None:
        assert len(BODY) > 20_000 - 5_000  # a long body, kept whole
        reply = save_memory(BODY, summary=SUMMARY, uri=URI)
        doc = temp_registry.knowledge.document_store.get(_saved_id(reply))
        assert doc is not None
        assert doc["content"] == BODY
        assert doc["metadata"]["summary"] == SUMMARY
        assert doc["metadata"]["uri"] == URI
        assert doc["metadata"]["document_form"] == "reference"

    @pytest.mark.parametrize(
        "metadata",
        [
            {"document_form": "note"},
            # A flat ``content_type`` outside the ContentType vocabulary reads as
            # a form, so it is a named form too.
            {"content_type": "handbook"},
        ],
    )
    def test_a_named_non_reference_form_is_refused_naming_it(
        self, temp_registry: StoreRegistry, metadata: dict[str, Any]
    ) -> None:
        form = next(iter(metadata.values()))
        # The summary parameter means "store a reference", so another form is a
        # disagreement, not something to write past: before this it stamped
        # nothing, embedded the body, never used the summary and took any length.
        with pytest.raises(MCPError, match=f"names the form '{form}'"):
            save_memory(BODY, summary=SUMMARY, metadata=metadata)
        assert _count(temp_registry) == 0

    def test_an_over_long_summary_on_another_form_is_refused_too(
        self, temp_registry: StoreRegistry
    ) -> None:
        with pytest.raises(MCPError, match="names the form 'note'"):
            save_memory(
                BODY,
                summary="s" * (EMBED_INPUT_CHAR_CAP + 1),
                metadata={"document_form": "note"},
            )
        assert _count(temp_registry) == 0

    def test_the_form_reference_in_metadata_is_accepted(
        self, temp_registry: StoreRegistry
    ) -> None:
        reply = save_memory(
            BODY, summary=SUMMARY, metadata={"document_form": "reference"}
        )
        doc = temp_registry.knowledge.document_store.get(_saved_id(reply))
        assert doc is not None
        assert doc["metadata"]["document_form"] == "reference"
        assert doc["metadata"]["summary"] == SUMMARY

    def test_an_in_vocabulary_content_type_is_not_a_form_and_the_stamp_applies(
        self, temp_registry: StoreRegistry
    ) -> None:
        # ``content_type: code`` is in the ContentType vocabulary, so it is
        # a retrieval tag and names no form: the reference form is stamped.
        reply = save_memory(BODY, summary=SUMMARY, metadata={"content_type": "code"})
        doc = temp_registry.knowledge.document_store.get(_saved_id(reply))
        assert doc is not None
        assert doc["metadata"]["document_form"] == "reference"
        assert doc["metadata"]["summary"] == SUMMARY

    def test_a_plain_summary_key_on_another_form_goes_in_metadata(
        self, temp_registry: StoreRegistry
    ) -> None:
        reply = save_memory(
            BODY, metadata={"document_form": "note", "summary": SUMMARY}
        )
        doc = temp_registry.knowledge.document_store.get(_saved_id(reply))
        assert doc is not None
        assert doc["metadata"]["document_form"] == "note"
        assert doc["metadata"]["summary"] == SUMMARY

    def test_a_uri_alone_does_not_make_a_reference(
        self, temp_registry: StoreRegistry
    ) -> None:
        reply = save_memory("a plain note about kilns", uri=URI)
        doc = temp_registry.knowledge.document_store.get(_saved_id(reply))
        assert doc is not None
        assert doc["metadata"]["uri"] == URI
        assert "document_form" not in doc["metadata"]

    def test_a_parameter_that_agrees_with_metadata_is_accepted(
        self, temp_registry: StoreRegistry
    ) -> None:
        reply = save_memory(
            BODY, summary=SUMMARY, uri=URI, metadata={"summary": SUMMARY, "uri": URI}
        )
        assert "Memory saved" in reply

    @pytest.mark.parametrize(
        ("kwargs", "field"),
        [
            (
                {"summary": SUMMARY, "metadata": {"summary": "another summary"}},
                "summary",
            ),
            ({"uri": URI, "metadata": {"uri": "https://other.example.test/"}}, "uri"),
        ],
    )
    def test_a_parameter_and_a_metadata_key_that_disagree_are_refused(
        self, temp_registry: StoreRegistry, kwargs: dict[str, Any], field: str
    ) -> None:
        with pytest.raises(MCPError, match=field):
            save_memory(BODY, **kwargs)
        assert _count(temp_registry) == 0

    @pytest.mark.parametrize(
        ("summary", "match"),
        [
            ("   ", "empty"),
            ("", "empty"),
            ("s" * (EMBED_INPUT_CHAR_CAP + 1), str(EMBED_INPUT_CHAR_CAP)),
        ],
    )
    def test_an_unusable_summary_is_refused_and_nothing_is_written(
        self, temp_registry: StoreRegistry, summary: str, match: str
    ) -> None:
        with pytest.raises(MCPError, match=match):
            save_memory(BODY, summary=summary)
        assert _count(temp_registry) == 0

    def test_a_summary_inside_metadata_is_judged_too(
        self, temp_registry: StoreRegistry
    ) -> None:
        with pytest.raises(MCPError, match="string"):
            save_memory(BODY, metadata={"document_form": "reference", "summary": 7})
        assert _count(temp_registry) == 0

    def test_without_the_new_parameters_nothing_changes(
        self, temp_registry: StoreRegistry
    ) -> None:
        reply = save_memory("a plain note", metadata={"summary": 7, "topic": "kilns"})
        doc = temp_registry.knowledge.document_store.get(_saved_id(reply))
        assert doc is not None
        assert doc["metadata"]["summary"] == 7
        assert "document_form" not in doc["metadata"]


class TestRefusalsAreRecorded:
    """A refused summary or uri is a ``WRITE_REJECTED`` event, as an empty body is."""

    @pytest.mark.parametrize(
        ("kwargs", "loc"),
        [
            ({"summary": "   "}, "summary"),
            ({"summary": "s" * (EMBED_INPUT_CHAR_CAP + 1)}, "summary"),
            (
                {"summary": SUMMARY, "metadata": {"summary": "another summary"}},
                "summary",
            ),
            ({"uri": URI, "metadata": {"uri": "https://other.example.test/"}}, "uri"),
            (
                {"summary": SUMMARY, "metadata": {"document_form": "note"}},
                "metadata.document_form",
            ),
        ],
    )
    def test_each_refusal_leaves_one_event(
        self, temp_registry: StoreRegistry, kwargs: dict[str, Any], loc: str
    ) -> None:
        assert _rejections(temp_registry) == []

        with pytest.raises(MCPError):
            save_memory(BODY, **kwargs)

        (event,) = _rejections(temp_registry)
        assert event.payload["rejections"][0]["loc"] == loc
        assert event.payload["rejections"][0]["kind"] == "value"
        assert _count(temp_registry) == 0

    @pytest.mark.parametrize(
        ("kwargs", "sent"),
        [
            pytest.param(
                {"summary": "   " + "a secret summary " * 600},
                "a secret summary",
                id="summary-over-the-cap",
            ),
            pytest.param(
                {"summary": SUMMARY, "metadata": {"document_form": "private-form"}},
                "private-form",
                id="short-form",
            ),
            pytest.param(
                {
                    "summary": SUMMARY,
                    "metadata": {"document_form": "secret-form-" + "f" * 5000},
                },
                "secret-form",
                id="5000-character-form",
            ),
            pytest.param(
                {"summary": SUMMARY, "metadata": {"summary": "a secret difference"}},
                "a secret difference",
                id="key-disagreement-summary",
            ),
            pytest.param(
                {"uri": URI, "metadata": {"uri": "https://secret.example.test/x"}},
                "secret.example.test",
                id="key-disagreement-uri",
            ),
            pytest.param(
                {"metadata": {"document_form": "reference", "summary": ["secret"]}},
                "secret",
                id="metadata-only-summary-of-the-wrong-type",
            ),
        ],
    )
    def test_the_event_quotes_nothing_the_caller_sent(
        self, temp_registry: StoreRegistry, kwargs: dict[str, Any], sent: str
    ) -> None:
        with pytest.raises(MCPError):
            save_memory(BODY, **kwargs)

        (event,) = _rejections(temp_registry)
        assert sent not in str(event.payload)
        assert len(str(event.payload)) < 1000

    def test_the_caller_is_told_a_short_cut_of_a_long_form(
        self, temp_registry: StoreRegistry
    ) -> None:
        form = "secret-form-" + "f" * 5000
        with pytest.raises(MCPError) as caught:
            save_memory(BODY, summary=SUMMARY, metadata={"document_form": form})

        text = str(caught.value)
        assert "names the form 'secret-form-" in text
        assert form not in text
        assert len(text) < 500

    def test_a_bad_summary_only_in_metadata_is_refused_before_the_duplicate_check(
        self, temp_registry: StoreRegistry
    ) -> None:
        save_memory(BODY)
        assert _count(temp_registry) == 1

        with pytest.raises(MCPError, match="limit is"):
            save_memory(
                BODY,
                metadata={
                    "document_form": "reference",
                    "summary": "s" * (EMBED_INPUT_CHAR_CAP + 1),
                },
            )

        (event,) = _rejections(temp_registry)
        assert event.payload["rejections"][0]["loc"] == "summary"
        assert _count(temp_registry) == 1

    @pytest.mark.parametrize(
        "summary", ["s" * (EMBED_INPUT_CHAR_CAP + 1), "   ", 7], ids=str
    )
    def test_a_bad_summary_only_in_metadata_on_a_new_body_is_recorded(
        self, temp_registry: StoreRegistry, summary: Any
    ) -> None:
        with pytest.raises(MCPError):
            save_memory(
                BODY, metadata={"document_form": "reference", "summary": summary}
            )

        (event,) = _rejections(temp_registry)
        assert event.payload["rejections"][0]["loc"] == "summary"
        assert _count(temp_registry) == 0

    def test_the_same_summary_through_the_parameter_is_recorded_alike(
        self, temp_registry: StoreRegistry
    ) -> None:
        too_long = "s" * (EMBED_INPUT_CHAR_CAP + 1)
        with pytest.raises(MCPError):
            save_memory(BODY, summary=too_long)
        with pytest.raises(MCPError):
            save_memory(
                BODY, metadata={"document_form": "reference", "summary": too_long}
            )

        first, second = _rejections(temp_registry)
        assert first.payload["rejections"] == second.payload["rejections"]

    def test_a_metadata_summary_on_another_form_is_still_accepted(
        self, temp_registry: StoreRegistry
    ) -> None:
        long_summary = "s" * (EMBED_INPUT_CHAR_CAP + 1)
        reply = save_memory(
            BODY, metadata={"document_form": "note", "summary": long_summary}
        )

        doc = temp_registry.knowledge.document_store.get(_saved_id(reply))
        assert doc is not None
        assert doc["metadata"]["summary"] == long_summary
        assert _rejections(temp_registry) == []

    def test_a_good_metadata_summary_on_a_reference_is_accepted(
        self, temp_registry: StoreRegistry
    ) -> None:
        reply = save_memory(
            BODY, metadata={"document_form": "reference", "summary": SUMMARY}
        )

        doc = temp_registry.knowledge.document_store.get(_saved_id(reply))
        assert doc is not None
        assert doc["metadata"]["summary"] == SUMMARY
        assert _rejections(temp_registry) == []

    def test_an_accepted_call_records_no_rejection(
        self, temp_registry: StoreRegistry
    ) -> None:
        save_memory(BODY, summary=SUMMARY, uri=URI)
        assert _rejections(temp_registry) == []


class TestGetItems:
    def test_returns_the_full_body_and_the_link(
        self, temp_registry: StoreRegistry
    ) -> None:
        reply_id = _saved_id(save_memory(BODY, summary=SUMMARY, uri=URI))
        reply = get_items([reply_id], max_tokens=50_000)
        assert BODY.strip()[:200] in reply
        assert "Firing schedules go on at length." in reply
        assert f"Source: `{URI}`" in reply
        assert reply.index("Source:") < reply.index("Firing schedules")

    def test_a_document_that_merely_has_a_uri_renders_as_before(
        self, temp_registry: StoreRegistry
    ) -> None:
        temp_registry.knowledge.document_store.put(
            "doc-uri", BODY, {"uri": URI, "summary": SUMMARY}
        )
        reply = get_items(["doc-uri"], max_tokens=50_000)
        assert "Firing schedules go on at length." in reply
        assert "Source:" not in reply

    def test_a_hostile_uri_does_not_break_the_markdown(
        self, temp_registry: StoreRegistry
    ) -> None:
        temp_registry.knowledge.document_store.put(
            "doc-hostile",
            BODY,
            {
                "document_form": "reference",
                "summary": SUMMARY,
                "uri": "x`\n\n## injected heading\n`y",
            },
        )
        reply = get_items(["doc-hostile"], max_tokens=50_000)
        source_lines = [ln for ln in reply.splitlines() if ln.startswith("Source:")]
        assert len(source_lines) == 1
        assert source_lines[0].count("`") == 2
        assert "\n## injected heading" not in reply
