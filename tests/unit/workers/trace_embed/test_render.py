"""What a trace turns into, and what it deliberately does not."""

from __future__ import annotations

from trellis.schemas.enums import OutcomeStatus
from trellis.schemas.trace import Outcome, TraceStep
from trellis_workers.trace_embed.render import (
    DOC_ID_PREFIX,
    MAX_STEP_ERROR_CHARS,
    MAX_STEP_ERRORS,
    build_trace_metadata,
    render_trace_summary,
    trace_id_from_doc_id,
    trace_summary_doc_id,
)

from .conftest import make_trace


class TestDocumentIdentity:
    def test_id_is_derived_and_reversible(self) -> None:
        assert trace_summary_doc_id("abc") == "trace-summary:abc"
        assert trace_id_from_doc_id("trace-summary:abc") == "abc"
        assert trace_id_from_doc_id("01KWWCBA56TQN2XHFCP51VRXA4") is None

    def test_namespace_does_not_collide_with_the_graph_node_id(self) -> None:
        """Trace extraction mints ``trace:<id>`` as the Activity *node* id. On
        Neo4j and ArcadeDB the vector store is shape #2 — the vector item_id
        **is** the node id — so reusing that namespace would have this worker
        upserting embeddings onto live SCD-2 graph nodes."""
        trace_id = "01M0ZC5EQW0N77HHCKMX8F7MGZ"
        assert trace_summary_doc_id(trace_id) != f"trace:{trace_id}"
        assert not trace_summary_doc_id(trace_id).startswith("trace:")
        assert DOC_ID_PREFIX.endswith(":")


class TestRenderedBody:
    def test_carries_intent_and_outcome_summary(self) -> None:
        trace = make_trace(1, intent="Investigate the flake", summary="It was DNS.")
        body = render_trace_summary(trace)
        assert "# Investigate the flake" in body
        assert "It was DNS." in body
        assert "**Outcome:** success" in body
        assert "domain `platform`" in body

    def test_step_errors_are_rendered(self) -> None:
        trace = make_trace(1, error="ruff: command not found (venv not activated)")
        body = render_trace_summary(trace)
        assert "## Failures recorded" in body
        assert "venv not activated" in body

    def test_step_errors_can_be_switched_off(self) -> None:
        trace = make_trace(1, error="boom")
        assert "boom" not in render_trace_summary(trace, include_step_errors=False)

    def test_tool_payloads_never_reach_the_body(self) -> None:
        """The step *log* is out of scope on purpose: args and results are tool
        traffic and would swamp the summary inside the embedder's window."""
        trace = make_trace(1)
        trace.steps.append(
            TraceStep(
                step_type="tool_call",
                name="Read",
                args={"file_path": "/etc/secret-looking-path"},
                result={"content": "a thousand lines of source"},
            )
        )
        body = render_trace_summary(trace)
        assert "secret-looking-path" not in body
        assert "a thousand lines of source" not in body

    def test_error_count_is_bounded(self) -> None:
        trace = make_trace(1)
        for n in range(MAX_STEP_ERRORS + 5):
            trace.steps.append(
                TraceStep(step_type="tool_call", name=f"s{n}", error=f"failure-{n}")
            )
        body = render_trace_summary(trace)
        assert body.count("- **s") == MAX_STEP_ERRORS
        assert f"failure-{MAX_STEP_ERRORS + 4}" not in body

    def test_a_long_error_is_elided_with_a_marker(self) -> None:
        trace = make_trace(1, error="x" * (MAX_STEP_ERROR_CHARS + 500))
        body = render_trace_summary(trace)
        assert "<elided" in body
        assert 'reason="step_error_cap"' in body

    def test_a_trace_with_no_prose_renders_to_nothing(self) -> None:
        """Only the provenance line would be left, and five words of
        ``source \\`agent\\``` on the semantic axis is the substance-free stub
        the content floor exists to demote."""
        trace = make_trace(1).model_copy(
            update={"intent": "", "outcome": None, "steps": []}
        )
        assert render_trace_summary(trace) == ""

    def test_an_outcome_summary_alone_is_enough(self) -> None:
        trace = make_trace(1).model_copy(
            update={
                "intent": "",
                "outcome": Outcome(status=OutcomeStatus.FAILURE, summary="It broke."),
            }
        )
        body = render_trace_summary(trace)
        assert "It broke." in body
        assert "**Outcome:** failure" in body


class TestMetadata:
    def test_shape(self) -> None:
        trace = make_trace(3, intent="Ship it", summary="Shipped")
        meta = build_trace_metadata(trace)
        assert meta["title"] == "Ship it"
        assert meta["source_system"] == "trellis-trace"
        assert meta["document_form"] == "trace_summary"
        assert meta["trace_id"] == trace.trace_id
        assert meta["outcome_status"] == "success"
        assert meta["domain"] == "platform"
        assert meta["source_path"] == f"trace/{trace.trace_id}"

    def test_absent_context_fields_are_omitted_not_nulled(self) -> None:
        """A ``domain: None`` key is not the same as no key: the store-side
        tag filters are ``json_extract`` predicates against the exact key."""
        trace = make_trace(3, domain="")
        meta = build_trace_metadata(trace)
        assert "domain" not in meta or meta["domain"]

    def test_title_falls_back_to_the_trace_id(self) -> None:
        trace = make_trace(3).model_copy(update={"intent": ""})
        assert build_trace_metadata(trace)["title"] == trace.trace_id
