"""Tests for retrieve CLI commands."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.chunk_corpus import seed_chunk_favouring, seed_chunked
from tests.cli_output import assert_coloured, force_colour, plain
from trellis_cli.exit_codes import EXIT_VALIDATION
from trellis_cli.main import app

runner = CliRunner()


def _plain(text: str) -> str:
    """Rich-rendered output reduced to what the operator's eye reads.

    Two decorations sit between a rendered line and the sentence it
    represents, and an assertion has to survive both.

    **Wrapping.** Rich breaks at the console width, so a message that is
    one logical line arrives with newlines inside it. Collapsing
    whitespace keeps assertions about *content* from becoming assertions
    about terminal width.

    **Colour.** Rich emits SGR escapes when it believes the stream is
    colour-capable, and its highlighter styles *parts* of a token — an
    option name renders as three separately-wrapped runs, so
    ``"--include-chunks" in output`` is ``False`` against output that
    plainly says ``--include-chunks``. This is the same class as the two
    renderer defects #410 fixed (``[document]`` eaten as a style tag, an
    ``item_id`` emojified): output that is decorated being read as if it
    were plain. It reappeared in the test asserting the fix.

    Stripping is :func:`tests.cli_output.plain` — i.e.
    :func:`click.utils.strip_ansi` — rather than a local regex; see that
    module for why click's own spelling is the one to reach for. The
    whitespace collapse is this file's addition, not part of it: a
    surface whose assertions are about wrapped prose needs it, and a
    surface asserting on a copy-pasteable command must not have it.
    """
    return " ".join(plain(text).split())


@pytest.fixture(autouse=True)
def _temp_stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point CLI stores at a temp directory."""
    data_dir = tmp_path / "data"
    (data_dir / "stores").mkdir(parents=True)
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))


def fake_embed(text: str) -> list[float]:
    """A resolvable embedder for ``TRELLIS_EMBEDDING_FN``.

    Deterministic and content-dependent, so the semantic axis it enables
    is a real axis rather than one that would rank everything equally.
    Nothing here asserts on the *values* — what is under test is whether
    the axis exists and whether the CLI says so.
    """
    return [float(len(text) % 7), float(sum(map(ord, text[:8])) % 11), 1.0]


class TestRetrievePack:
    """``retrieve pack`` assembles a real pack (#410).

    Until #410 this command called ``DocumentStore.search`` directly: one
    keyword axis, no graph axis, no semantic axis, no fusion, no token
    budget, and — the part that mattered most — no ``PACK_ASSEMBLED``
    event, so no ``pack_id`` and nothing the learning loop could grade.
    It was named, documented and reached for as a preview of what an agent
    is served while returning a different result set from every surface an
    agent uses.

    The fixture below is deliberately **not uniform**. Documents alone
    would let ``item_type`` be a constant; one axis alone would let
    ``strategy_source`` be one; equal-scoring items would let ``rank`` be
    anything. Each assertion needs a population that can tell a right
    value from a hard-coded one.
    """

    @staticmethod
    def _seed_two_axes() -> tuple[list[str], list[str]]:
        """Seed documents *and* graph entities matching one intent.

        Returns ``(doc_ids, entity_ids)``. The graph axis is a recency feed
        (#371), so the entities need no keyword overlap to be retrieved —
        which is precisely why they are here: they arrive on a different
        axis with a different ``item_type``, and no single constant can
        satisfy an assertion over both halves.
        """
        from trellis_cli.stores import get_document_store, get_graph_store

        docs = get_document_store()
        # Bodies must be genuinely different, not templated: the builder
        # runs MinHash near-duplicate dedup (#259) at 0.85 Jaccard, and an
        # earlier version of this fixture wrote four sentences differing
        # by one digit — the pack correctly collapsed them to one and the
        # rank assertions had nothing left to rank.
        bodies = [
            (
                "canary rollout runbook: shift ten percent of traffic,"
                " watch p99 latency for fifteen minutes, then promote"
            ),
            (
                "canary rollout abort criteria: any five-hundred rate above"
                " baseline, or a saturated connection pool on the replica"
            ),
            (
                "canary rollout owner rota: platform holds the pager during"
                " business hours, the service team overnight"
            ),
            (
                "canary rollout prerequisites: a green migration, a feature"
                " flag defaulted off, and a tested database rollback"
            ),
        ]
        doc_ids = []
        for i, body in enumerate(bodies):
            doc_id = f"corpus:obsidian:doc{i}"
            docs.put(doc_id, body)
            doc_ids.append(doc_id)

        graph = get_graph_store()
        descriptions = [
            "owns the public edge and terminates every inbound request",
            "stores order history and replays it onto the event bus",
            "runs the nightly reconciliation between billing and ledger",
        ]
        entity_ids = []
        for i in range(3):
            entity_id = f"svc-{i}"
            graph.upsert_node(
                entity_id,
                "service",
                {
                    "name": f"service {i}",
                    "description": descriptions[i],
                },
            )
            entity_ids.append(entity_id)
        return doc_ids, entity_ids

    @staticmethod
    def _json(*args: str) -> dict:
        result = runner.invoke(app, [*args, "--format", "json", "--quiet"])
        assert result.exit_code == 0, result.output
        return json.loads(result.stdout.strip())

    def test_pack_request(self) -> None:
        result = runner.invoke(
            app,
            ["retrieve", "pack", "--intent", "deploy checklist"],
        )
        assert result.exit_code == 0

    def test_pack_json(self) -> None:
        result = runner.invoke(
            app,
            [
                "retrieve",
                "pack",
                "--intent",
                "deploy",
                "--domain",
                "platform",
                "--format",
                "json",
            ],
        )
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())
        assert data["intent"] == "deploy"
        assert data["status"] == "ok"

    def test_pack_emits_pack_assembled_carrying_the_returned_pack_id(self) -> None:
        """The defect's load-bearing half, read back out of a real store.

        #410's cost was not "the ranking is different" — it was that the
        command's output could never be graded, because no pack existed to
        cite. So this reads the *event log* the CLI wrote (a real
        ``SQLiteEventLog``, not a mock), not just the printed payload:
        #447 shipped because a gate's filtering half was covered and its
        emission half was not, and a ``pack_id`` minted in memory and never
        persisted would satisfy every assertion about the JSON alone.
        """
        from trellis.stores.base.event_log import EventType
        from trellis_cli.stores import get_event_log

        self._seed_two_axes()
        before = get_event_log().get_events(
            event_type=EventType.PACK_ASSEMBLED, limit=100
        )
        assert before == []

        data = self._json("retrieve", "pack", "--intent", "canary rollout")

        assert data["pack_id"]
        events = get_event_log().get_events(
            event_type=EventType.PACK_ASSEMBLED, limit=100
        )
        assert len(events) == 1, events
        event = events[0]
        assert event.entity_id == data["pack_id"]
        assert event.entity_type == "pack"
        # The join key the learning loop needs on the *pack* side.
        assert event.payload["injected_item_ids"] == [
            item["item_id"] for item in data["items"]
        ]

    def test_pack_runs_every_axis_the_deployment_has(self) -> None:
        """Keyword *and* graph, not the single keyword axis of the bypass.

        Asserted through the served items' ``strategy_source`` as well as
        the report, because the report is a list the builder appends to
        and the items are what an operator reads.
        """
        self._seed_two_axes()
        data = self._json("retrieve", "pack", "--intent", "canary rollout")

        report = data["retrieval_report"]
        assert report["strategies_used"] == ["keyword", "graph"]
        assert report["queries_run"] == 2
        sources = {item["strategy_source"] for item in data["items"]}
        assert sources == {"keyword", "graph"}
        types = {item["item_type"] for item in data["items"]}
        assert {"document", "entity"} <= types, types

    def test_pack_items_carry_the_builder_rank_and_score(self) -> None:
        """Rank and score are the builder's, and they agree with each other.

        A constant would pass "rank is an int"; it cannot pass "ranks are
        1..n in the order printed **and** the scores are non-increasing
        across that order **and** at least two distinct scores exist".
        """
        self._seed_two_axes()
        data = self._json("retrieve", "pack", "--intent", "canary rollout")

        items = data["items"]
        assert len(items) >= 4
        assert [item["rank"] for item in items] == list(range(1, len(items) + 1))
        scores = [item["relevance_score"] for item in items]
        assert scores == sorted(scores, reverse=True)
        assert len(set(scores)) > 1, scores
        # RRF fusion ran: the reranker writes its own breakdown keys, which
        # the pre-#410 raw store search had no way to produce.
        assert any("rrf_total" in item["score_breakdown"] for item in items)

    def test_pack_says_the_semantic_axis_is_absent(self) -> None:
        """A missing axis is reported, not silently absorbed.

        ``build_strategies`` drops the semantic axis with a ``logger.info``
        line, and the CLI pins logging at ``WARNING`` — so on a deployment
        with no embedder the axis vanishes with no observable trace at all.
        Degrading to keyword+graph *quietly* would reproduce the defect
        this command is being fixed for, one layer up.
        """
        self._seed_two_axes()
        data = self._json("retrieve", "pack", "--intent", "canary rollout")

        assert data["axes"]["semantic"] == "not_configured"
        assert "semantic" not in data["axes"]["available"]
        assert data["axes"]["failed"] == []

        text = runner.invoke(app, ["retrieve", "pack", "--intent", "canary rollout"])
        assert text.exit_code == 0
        assert "Semantic axis unavailable" in _plain(text.stdout)

    def test_pack_runs_the_semantic_axis_when_an_embedder_resolves(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The other side of the same fact.

        Without this, ``axes.semantic`` could be the constant
        ``"not_configured"`` and the warning could be printed
        unconditionally, and every other assertion in this class would
        still pass. It is also what proves the note is *conditional* — a
        caveat that always prints is one that always gets skipped (#365).
        """
        monkeypatch.setenv(
            "TRELLIS_EMBEDDING_FN", "tests.unit.cli.test_retrieve.fake_embed"
        )
        self._seed_two_axes()
        data = self._json("retrieve", "pack", "--intent", "canary rollout")

        assert "semantic" in data["axes"]["available"]
        assert data["axes"]["semantic"] == "ran"
        assert "semantic" in data["retrieval_report"]["strategies_used"]

        text = runner.invoke(app, ["retrieve", "pack", "--intent", "canary rollout"])
        assert text.exit_code == 0
        assert "Semantic axis unavailable" not in _plain(text.stdout)

    def test_pack_enforces_a_token_budget(self) -> None:
        """``--max-tokens`` is a real ceiling, and the cut is reported.

        The bypass had no token budget at all — ``--max-items`` capped rows
        and nothing capped tokens — while README documented a
        ``--max-tokens`` flag that did not exist.
        """
        self._seed_two_axes()
        wide = self._json("retrieve", "pack", "--intent", "canary rollout")
        narrow = self._json(
            "retrieve", "pack", "--intent", "canary rollout", "--max-tokens", "40"
        )

        assert narrow["budget"]["max_tokens"] == 40
        assert 0 < narrow["count"] < wide["count"]
        assert narrow["withholding"]["by_reason"].get("token_budget")
        served = sum(item["estimated_tokens"] for item in narrow["items"])
        assert served <= 40

    def test_pack_enforces_max_items_and_reports_what_it_dropped(self) -> None:
        self._seed_two_axes()
        data = self._json(
            "retrieve", "pack", "--intent", "canary rollout", "--max-items", "2"
        )
        assert data["count"] == 2
        assert data["budget"]["max_items"] == 2
        assert data["withholding"]["by_reason"].get("max_items")
        assert data["withholding"]["total"] >= 1
        # The stamped summary goes out verbatim, ids included: #404's
        # counts-and-reasons-only rule scopes the rendered note an agent
        # reads, not this payload, whose reader already holds the stores
        # and needs the ids to go and look. Pinned so the JSON arm and the
        # comment above it cannot drift apart again.
        withheld = data["withholding"]["withheld_item_ids"]
        assert len(withheld) == data["withholding"]["total"]
        served = {item["item_id"] for item in data["items"]}
        assert not served & set(withheld)

    def test_pack_text_output_states_withholding_above_the_items(self) -> None:
        """Header, not footer — the #404 rule, in this renderer too.

        A note appended after the item list is a note the reader of a long
        list never reaches, which is the "honest in JSON alone" failure
        one layer down.
        """
        self._seed_two_axes()
        result = runner.invoke(
            app,
            ["retrieve", "pack", "--intent", "canary rollout", "--max-items", "2"],
        )
        assert result.exit_code == 0
        out = _plain(result.stdout)
        assert "Withheld:" in out
        assert out.index("Withheld:") < out.index("corpus:obsidian:doc")

    def test_pack_text_output_names_each_item_type_and_axis(self) -> None:
        r"""The rendered columns are read off the item, not hard-coded.

        #456's lesson, applied before the fact: a field that is *printed*
        but never asserted is a field a constant satisfies, and eleven of
        twelve such mutants survived the full suite last time. The fixture
        spans two ``item_type``\ s and two axes precisely so neither can
        be folded to a constant here.
        """
        self._seed_two_axes()
        result = runner.invoke(app, ["retrieve", "pack", "--intent", "canary rollout"])
        assert result.exit_code == 0
        out = _plain(result.stdout)
        assert "[document]" in out
        assert "[entity]" in out
        assert "(keyword)" in out
        assert "(graph)" in out

    def test_pack_forwards_domain_and_agent_to_the_builder(self) -> None:
        """``--agent`` was accepted, printed back, and ignored (#410).

        Both scopes are read back off the *pack*, not off the arguments:
        the pack's ``domain`` / ``agent_id`` are what the builder was
        given and what rides the ``PACK_ASSEMBLED`` event, so an echo of
        the flag would not satisfy this.
        """
        from trellis.stores.base.event_log import EventType
        from trellis_cli.stores import get_event_log

        self._seed_two_axes()
        data = self._json(
            "retrieve",
            "pack",
            "--intent",
            "canary rollout",
            "--domain",
            "platform",
            "--agent",
            "operator-1",
        )
        assert data["domain"] == "platform"
        assert data["agent_id"] == "operator-1"

        event = get_event_log().get_events(
            event_type=EventType.PACK_ASSEMBLED, limit=10
        )[0]
        assert event.payload["domain"] == "platform"
        assert event.payload["agent_id"] == "operator-1"

    def test_pack_reports_a_broken_vector_backend_as_misconfigured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The fourth axis state, reached through the CLI rather than asserted.

        ``describe_axes`` distinguishes ``misconfigured`` from
        ``not_configured``, but the CLI supplies the argument that separates
        them (``embedder_configured=registry.embedding_fn is not None``) —
        and hard-coding that argument to ``False`` passed every other test
        in this file. The state was therefore unreachable from the surface
        that renders it, so an operator with a resolved embedder and a
        broken vector backend would have been told to configure an
        embedder: sent to fix something already correct, which is the exact
        failure this state exists to prevent.

        ``build_strategies`` swallows a ``SemanticSearch`` init failure —
        it logs at ``warning`` to *stderr* and carries on, so the pack
        itself says nothing — which makes raising from the constructor the
        real shape of this state rather than a contrived one.
        """
        from trellis.retrieve import strategies as strategies_mod

        msg = "vector backend unavailable"

        def _boom(*args: object, **kwargs: object) -> object:
            raise RuntimeError(msg)

        monkeypatch.setenv(
            "TRELLIS_EMBEDDING_FN", "tests.unit.cli.test_retrieve.fake_embed"
        )
        monkeypatch.setattr(strategies_mod, "SemanticSearch", _boom)
        self._seed_two_axes()
        data = self._json("retrieve", "pack", "--intent", "canary rollout")

        assert data["axes"]["semantic"] == "misconfigured"
        assert "semantic" not in data["axes"]["available"]

        text = runner.invoke(app, ["retrieve", "pack", "--intent", "canary rollout"])
        assert text.exit_code == 0
        out = _plain(text.stdout)
        assert "did not initialise" in out
        # The wrong advice for this state, and the one a merged
        # "is semantic missing?" boolean would have printed.
        assert "no embeddings provider" not in out

    def test_pack_item_ids_are_printed_verbatim_not_emojified(self) -> None:
        r"""``emoji=False`` is load-bearing, and nothing else pins it.

        A real ``dataset:snowflake://…`` id contains the literal
        ``:snowflake:``, which Rich rewrites to ❄️ — an id an operator
        cannot copy and a script cannot match. The PR that added
        ``emoji=False`` called it a live defect fix; flipping it back to
        ``emoji=True`` passed every other test here, which is the #456
        shape: a field that is printed but never asserted.
        """
        from trellis_cli.stores import get_document_store

        item_id = "dataset:snowflake://analytics/canary_rollout_audit"
        get_document_store().put(
            item_id,
            "canary rollout audit table: one row per promotion decision"
            " with the operator, the observed error rate and the verdict",
        )
        result = runner.invoke(app, ["retrieve", "pack", "--intent", "canary rollout"])
        assert result.exit_code == 0
        out = _plain(result.stdout)
        assert item_id in out, out
        assert "\N{SNOWFLAKE}" not in out

    def test_pack_max_items_buys_recall_instead_of_capping_at_the_default(
        self,
    ) -> None:
        """``limit_per_strategy`` tracks ``--max-items``; it is not the default.

        ``PackBuilder``'s per-axis fetch defaults to 20, so a builder given
        ``limit_per_strategy=20`` returns at most 20 keyword candidates no
        matter what ``--max-items`` says — the item budget would then be a
        ceiling the deployment can never reach. Constant-folding that
        argument to ``20`` passed every other test in this file, because
        the shared fixture holds seven candidates.
        """
        from trellis_cli.stores import get_document_store

        docs = get_document_store()
        subjects = [
            "postgres",
            "redis",
            "kafka",
            "envoy",
            "consul",
            "vault",
            "etcd",
            "nginx",
            "haproxy",
            "rabbitmq",
            "clickhouse",
            "cassandra",
            "spark",
            "airflow",
            "grafana",
            "loki",
            "tempo",
            "vector",
            "fluentd",
            "istio",
            "argo",
            "flux",
            "keda",
            "cilium",
            "calico",
            "harbor",
            "trivy",
            "cosign",
            "spire",
            "teleport",
        ]
        for i, subject in enumerate(subjects):
            docs.put(
                f"doc-{i}",
                f"canary rollout for {subject}: promote after"
                f" {i + 3} minutes of clean {subject} telemetry",
            )

        data = self._json(
            "retrieve",
            "pack",
            "--intent",
            "canary rollout",
            "--max-items",
            "30",
            "--max-tokens",
            "100000",
        )
        assert data["count"] > 20, data["count"]

    def test_pack_identifies_itself_to_the_advisory_loader(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The #373 journal discriminator, from the surface that passes it.

        ``build_pack_builder``'s ``surface`` argument answers "which caller
        saw which advisory file". The factory suite proves each label
        reaches the loader; nothing proved *this* command sends its own,
        so passing ``surface="mcp"`` from the CLI was invisible — and a
        mislabelled reader is how #373's two-files question went
        unanswerable in the first place.
        """
        from trellis.retrieve import builder_factory

        seen: list[str] = []
        original = builder_factory.load_advisory_store

        def _spy(stores_dir: object, *, surface: str) -> object:
            seen.append(surface)
            return original(stores_dir, surface=surface)

        monkeypatch.setattr(builder_factory, "load_advisory_store", _spy)
        self._seed_two_axes()
        self._json("retrieve", "pack", "--intent", "canary rollout")
        assert seen == ["cli.retrieve"]

    def test_pack_exit_code_does_not_depend_on_format(self) -> None:
        """The repo-wide rule, checked on this command's own success path."""
        self._seed_two_axes()
        codes = {
            fmt: runner.invoke(
                app, ["retrieve", "pack", "--intent", "canary rollout", "--format", fmt]
            ).exit_code
            for fmt in ("text", "json")
        }
        assert set(codes.values()) == {0}, codes


class TestRetrieveSearch:
    def test_search(self) -> None:
        result = runner.invoke(app, ["retrieve", "search", "kubernetes"])
        assert result.exit_code == 0

    def test_search_json(self) -> None:
        result = runner.invoke(
            app,
            [
                "retrieve",
                "search",
                "kubernetes",
                "--format",
                "json",
            ],
        )
        data = json.loads(result.stdout.strip())
        assert data["query"] == "kubernetes"
        assert data["status"] == "ok"


#: A document id that trips both halves of #492 at once. ``:snowflake:``
#: is a live Rich emoji shortcode and ``[audit]`` is a well-formed style
#: tag, so an unprotected renderer substitutes the first and *deletes* the
#: second. Short enough not to wrap at Rich's default 80-column width,
#: because ``_plain`` collapses whitespace and a wrapped id would pass
#: through it with a space in the middle.
MANGLE_TRAP_ID = "dataset:snowflake://db/[audit]"


class TestOperatorCopyableIds:
    r"""Every id the operator is expected to copy survives the renderer (#492).

    ``retrieve pack``'s item line was fixed by hand in #488 and is covered
    by ``test_pack_item_ids_are_printed_verbatim_not_emojified``. These are
    the siblings that were still live: ``search``'s result line — the one
    #492 was filed against — and ``entity`` / ``trace``, whose whole output
    is an id the operator just typed or is about to type again.

    Each is asserted twice, plain and coloured. That is not belt-and-braces:
    ``force_colour`` swaps the module console, and a console rebuilt without
    ``emoji=False`` renders the ❄ again — so the coloured arm is what pins
    the *factory* (``trellis_cli.output.build_console``) rather than merely
    the keyword's presence in ``retrieve.py``. Reverting
    ``tests.cli_output.force_colour`` to a bare ``Console(force_terminal=
    True)`` turns exactly the three coloured id assertions below red and
    nothing else in the suite — measured by running that mutation, not
    reasoned from the code.
    """

    def _seed_document(self) -> None:
        from trellis_cli.stores import get_document_store

        get_document_store().put(MANGLE_TRAP_ID, "canary rollout audit table")

    def _seed_entity(self) -> None:
        from trellis_cli.stores import get_graph_store

        get_graph_store().upsert_node(MANGLE_TRAP_ID, "dataset", {"name": "audit"})

    @staticmethod
    def _assert_verbatim(output: str) -> None:
        rendered = " ".join(plain(output).split())
        assert MANGLE_TRAP_ID in rendered, rendered
        assert "\N{SNOWFLAKE}" not in rendered, rendered

    def test_search_prints_the_doc_id_verbatim(self) -> None:
        """The filed defect: ``dataset:snowflake://…`` came back as ❄."""
        self._seed_document()
        result = runner.invoke(app, ["retrieve", "search", "canary"])
        assert result.exit_code == 0
        self._assert_verbatim(result.stdout)

    def test_search_prints_the_doc_id_verbatim_under_colour(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import trellis_cli.retrieve as cli_retrieve

        self._seed_document()
        force_colour(monkeypatch, cli_retrieve)
        result = runner.invoke(app, ["retrieve", "search", "canary"])
        assert result.exit_code == 0
        self._assert_verbatim(assert_coloured(result.stdout))

    def test_entity_prints_the_id_verbatim(self) -> None:
        self._seed_entity()
        result = runner.invoke(app, ["retrieve", "entity", MANGLE_TRAP_ID])
        assert result.exit_code == 0
        self._assert_verbatim(result.stdout)

    def test_entity_prints_the_id_verbatim_under_colour(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import trellis_cli.retrieve as cli_retrieve

        self._seed_entity()
        force_colour(monkeypatch, cli_retrieve)
        result = runner.invoke(app, ["retrieve", "entity", MANGLE_TRAP_ID])
        assert result.exit_code == 0
        self._assert_verbatim(assert_coloured(result.stdout))

    def test_not_found_echoes_the_id_the_operator_typed(self) -> None:
        """The arm where a mangled id is worst.

        "Entity not found: dataset❄//db/" is an operator reading that
        *their* id is absent, when the id the store was asked for is not
        the id on screen. Both not-found paths are covered because the two
        commands render them independently.
        """
        for command in ("entity", "trace"):
            result = runner.invoke(app, ["retrieve", command, MANGLE_TRAP_ID])
            assert result.exit_code == 1
            self._assert_verbatim(result.stdout)

    def test_not_found_echoes_the_id_under_colour(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import trellis_cli.retrieve as cli_retrieve

        force_colour(monkeypatch, cli_retrieve)
        for command in ("entity", "trace"):
            result = runner.invoke(app, ["retrieve", command, MANGLE_TRAP_ID])
            assert result.exit_code == 1
            self._assert_verbatim(assert_coloured(result.stdout))

    def test_the_styling_around_the_id_still_renders(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Escaping the value must not have escaped the markup beside it.

        ``escape`` is chosen over ``markup=False`` precisely so the
        ``[green]Entity[/green]`` tag keeps working. Without this, the
        cheapest way to make every assertion above pass is to disable
        markup on the whole line, which silently drops the CLI's colour —
        and every other test here would stay green.
        """
        import trellis_cli.retrieve as cli_retrieve

        self._seed_entity()
        force_colour(monkeypatch, cli_retrieve)
        result = runner.invoke(app, ["retrieve", "entity", MANGLE_TRAP_ID])

        assert result.exit_code == 0
        assert "[green]" not in result.stdout, "the style tag leaked as literal text"
        assert "\x1b[" in result.stdout, "the style tag rendered nothing at all"


class TestRetrieveChunkVisibility:
    """``retrieve search`` hands back whole rows (#396).

    It prints a ``doc_id`` per result, so a ``<parent>#chunk-N`` row is a
    fragment of a document the same output already lists. The REST siblings
    (``GET /api/v1/documents``, ``GET /api/v1/search``) exclude chunks by
    default; this did not, and an operator running ``trellis retrieve
    search`` against the reference deployment (56% chunk rows) saw the same
    noise the REST fix removed.

    ``retrieve pack`` was in this class until #410 routed it through
    ``PackBuilder``. On a pack surface the chunk *is* the retrievable unit
    and the excerpt is what the token budget prices, so the last test below
    pins the opposite rule for it.
    """

    #: The production id shape, with a source system Rich does not render
    #: as an emoji — see :func:`tests.chunk_corpus.seed_chunked` and #403.
    _ID_PREFIX = "corpus:obsidian"

    @staticmethod
    def _seed(parents: int = 3, per_parent: int = 3) -> list[str]:
        from trellis_cli.stores import get_document_store

        return seed_chunked(
            get_document_store(),
            parents=parents,
            per_parent=per_parent,
            id_prefix=TestRetrieveChunkVisibility._ID_PREFIX,
        )

    @staticmethod
    def _seed_chunk_favouring(parents: int = 25) -> list[str]:
        from trellis_cli.stores import get_document_store

        return seed_chunk_favouring(
            get_document_store(),
            parents=parents,
            id_prefix=TestRetrieveChunkVisibility._ID_PREFIX,
        )

    @staticmethod
    def _json(*args: str) -> dict:
        """Invoke the CLI on the machine-safe path and parse the payload.

        ``--quiet`` is not stylistic: without it the payload prints through
        ``console.print``, and Rich both rewrites ``:name:`` emoji
        shortcodes inside string values and line-wraps at the console
        width, putting literal newlines inside JSON strings so a wide
        payload does not parse at all (#403). Only the *printing* differs
        between the two paths — both run the same
        ``store.search(..., include_chunks=...)`` call — so chunk
        visibility is tested faithfully here while #403 owns the channel.
        """
        result = runner.invoke(app, [*args, "--format", "json", "--quiet"])
        assert result.exit_code == 0, result.output
        return json.loads(result.stdout.strip())

    def test_search_excludes_chunks_by_default(self) -> None:
        parent_ids = self._seed()
        data = self._json("retrieve", "search", "distinctive")
        assert {r["doc_id"] for r in data["results"]} == set(parent_ids)

    def test_search_include_chunks_opt_in(self) -> None:
        self._seed()
        data = self._json("retrieve", "search", "distinctive", "--include-chunks")
        assert data["count"] == 12
        assert any("#chunk-" in r["doc_id"] for r in data["results"])

    def test_search_limit_is_not_shortened_by_the_chunk_filter(self) -> None:
        """``--limit N`` returns N documents, not N minus the chunks.

        Pins the store-level pushdown from the CLI side: with chunks
        ranking above parents, a post-hoc filter would print *nothing* for
        ``--limit 20`` and the operator would read that as "no matches".
        """
        self._seed_chunk_favouring()

        # Precondition: the fixture really does rank chunks first, so the
        # assertion below tests the pushdown rather than the seed data.
        unfiltered = self._json(
            "retrieve", "search", "distinctive", "--limit", "20", "--include-chunks"
        )
        assert all("#chunk-" in r["doc_id"] for r in unfiltered["results"])

        data = self._json("retrieve", "search", "distinctive", "--limit", "20")
        assert data["count"] == 20
        assert not [r for r in data["results"] if "#chunk-" in r["doc_id"]]

    def test_search_text_output_omits_chunk_ids(self) -> None:
        """The operator's actual invocation: no flags at all.

        Every other assertion here reads ``--format json --quiet``, which is
        the machine path. The default text path renders one id per line and
        is unaffected by #403, but nothing covered it with results in the
        store — the pre-existing ``test_search`` runs against an empty one
        and so proves only an exit code.
        """
        self._seed()
        result = runner.invoke(app, ["retrieve", "search", "distinctive"])
        assert result.exit_code == 0, result.output
        assert "#chunk-" not in plain(result.stdout)
        assert "corpus:obsidian:doc0" in result.stdout

    def test_pack_serves_chunk_rows_and_has_no_flag_to_suppress_them(self) -> None:
        """``retrieve pack`` stopped being a row surface (#410).

        #396 classified it as one *because* it bypassed ``PackBuilder``,
        and said in as many words that routing it through the builder
        would make chunks the retrievable unit — at which point the flag
        should be removed, not inverted. Both halves are pinned here: the
        chunk rows are served, and the flag is gone rather than defaulting
        the other way (a flag that still parses is a flag scripts keep
        passing and readers keep believing).
        """
        parent_ids = self._seed()
        data = self._json("retrieve", "pack", "--intent", "distinctive")
        served = {item["item_id"] for item in data["items"]}
        assert any("#chunk-" in item_id for item_id in served), served
        # The parents are still there — chunks are additive candidates on
        # the keyword axis, not a replacement for the documents.
        assert served & set(parent_ids)

        rejected = runner.invoke(
            app,
            ["retrieve", "pack", "--intent", "distinctive", "--include-chunks"],
        )
        assert rejected.exit_code != 0
        # Through ``_plain``: Typer renders this error with Rich, whose
        # highlighter styles the option name in three separate runs, so
        # the raw output does not contain the literal flag even though it
        # displays it. Naming the flag is the property under test — an
        # operator with the old flag in a script has to learn *which*
        # flag went away — so the assertion is kept and the decoration
        # removed, never the reverse.
        assert "--include-chunks" in _plain(rejected.output)

    def test_the_removed_flag_is_named_even_when_rich_colourises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The same property under the rendering CI actually produces.

        This suite runs against a pipe, where Rich emits no colour, so
        the assertion above passed locally and failed on all three CI
        Pythons: GitHub's runners are detected as colour-capable and the
        option name arrives as
        ``\x1b[1;36m-\x1b[0m\x1b[1;36m-include\x1b[0m\x1b[1;36m-chunks\x1b[0m``.
        A local suite could not have caught that, so the fix is not only
        to strip the escapes but to *exercise* the branch that emits
        them — otherwise the next such assertion is brittle again and
        discovers it in CI too.

        ``FORCE_COLOR`` is Rich's own opt-in and is read when the console
        is constructed, which for a Typer usage error is at render time —
        unlike a ``trellis_cli`` module's own console, which caches its
        answer at import and needs ``force_colour`` (#495).
        """
        monkeypatch.setenv("FORCE_COLOR", "1")
        self._seed()
        rejected = runner.invoke(
            app,
            ["retrieve", "pack", "--intent", "distinctive", "--include-chunks"],
        )
        assert rejected.exit_code != 0
        # The escapes really are present — without this the test would
        # pass by accident on a build where Rich decided not to colour,
        # and would then be pinning nothing. Through ``assert_coloured``
        # rather than a local ``"\x1b[" in ...``: the shared helper is the
        # one spelling of this check, and it is itself pinned by
        # ``tests/unit/test_cli_output.py``.
        rendered = assert_coloured(rejected.output)
        assert "--include-chunks" not in rejected.output
        assert "--include-chunks" in _plain(rendered)


class TestRetrieveTrace:
    def test_trace_not_found(self) -> None:
        result = runner.invoke(app, ["retrieve", "trace", "nonexistent"])
        assert result.exit_code == 1

    def test_trace_not_found_json(self) -> None:
        result = runner.invoke(
            app,
            [
                "retrieve",
                "trace",
                "nonexistent",
                "--format",
                "json",
            ],
        )
        data = json.loads(result.stdout.strip())
        assert data["status"] == "not_found"


class TestRetrieveEntity:
    def test_entity_not_found(self) -> None:
        result = runner.invoke(app, ["retrieve", "entity", "ent_456"])
        assert result.exit_code == 1

    def test_entity_resolves_via_local_alias(self) -> None:
        from trellis_cli.stores import LOCAL_SOURCE_SYSTEM, get_graph_store

        graph = get_graph_store()
        graph.upsert_node("ulid_for_api", "service", {"name": "user-api"})
        graph.upsert_alias(
            entity_id="ulid_for_api",
            source_system=LOCAL_SOURCE_SYSTEM,
            raw_id="user-api",
            raw_name="user-api",
            is_primary=True,
        )

        result = runner.invoke(app, ["retrieve", "entity", "user-api"])
        assert result.exit_code == 0
        assert "service" in result.stdout

    def test_entity_resolves_via_local_alias_json(self) -> None:
        from trellis_cli.stores import LOCAL_SOURCE_SYSTEM, get_graph_store

        graph = get_graph_store()
        graph.upsert_node("ulid_for_api", "service", {"name": "user-api"})
        graph.upsert_alias(
            entity_id="ulid_for_api",
            source_system=LOCAL_SOURCE_SYSTEM,
            raw_id="user-api",
            raw_name="user-api",
            is_primary=True,
        )

        result = runner.invoke(
            app, ["retrieve", "entity", "user-api", "--format", "json"]
        )
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())
        assert data.get("status") != "not_found"
        assert data["node_type"] == "service"


class TestDocPreview:
    def test_uses_snippet_when_present(self) -> None:
        from trellis_cli.retrieve import _doc_preview

        doc = {"snippet": "short snippet", "content": "long content here"}
        assert _doc_preview(doc, 80) == "short snippet"

    def test_falls_back_to_content_when_snippet_missing(self) -> None:
        from trellis_cli.retrieve import _doc_preview

        assert _doc_preview({"content": "full document"}, 80) == "full document"
        assert _doc_preview({"snippet": "", "content": "fallback"}, 80) == "fallback"
        assert _doc_preview({"snippet": None, "content": "fallback"}, 80) == "fallback"

    def test_returns_empty_when_no_text(self) -> None:
        from trellis_cli.retrieve import _doc_preview

        assert _doc_preview({}, 80) == ""
        assert _doc_preview({"snippet": "", "content": ""}, 80) == ""
        assert _doc_preview({"snippet": None, "content": None}, 80) == ""

    def test_collapses_whitespace_and_newlines(self) -> None:
        from trellis_cli.retrieve import _doc_preview

        doc = {"content": "# Heading\n\nParagraph\twith\ttabs   and   spaces"}
        assert _doc_preview(doc, 80) == "# Heading Paragraph with tabs and spaces"

    def test_truncates_to_width(self) -> None:
        from trellis_cli.retrieve import _doc_preview

        doc = {"content": "abcdefghij" * 20}
        result = _doc_preview(doc, 50)
        assert len(result) == 50
        assert result == "abcdefghij" * 5


class TestRetrievePrecedents:
    def test_precedents(self) -> None:
        result = runner.invoke(app, ["retrieve", "precedents"])
        assert result.exit_code == 0

    def test_precedents_json(self) -> None:
        result = runner.invoke(
            app,
            [
                "retrieve",
                "precedents",
                "--format",
                "json",
            ],
        )
        data = json.loads(result.stdout.strip())
        assert data["status"] == "ok"
        assert data["count"] == 0


class TestRetrieveFileContext:
    @staticmethod
    def _seed(*, extraction_status: str | None = None) -> None:
        from trellis_cli.stores import get_document_store, get_graph_store

        get_document_store().put(
            "corpus:vault:abc",
            "Gotcha: the pack builder truncates before scoring.",
            metadata={"source_path": "src/trellis/retrieve/pack_builder.py"},
        )
        props: dict[str, str] = {"name": "PackBuilder"}
        if extraction_status is not None:
            props["extraction_status"] = extraction_status
        get_graph_store().upsert_node(
            "ent-packbuilder",
            "concept",
            props,
            document_ids=["corpus:vault:abc"],
        )

    def test_absolute_query_finds_stored_relpath(self) -> None:
        self._seed()
        result = runner.invoke(
            app,
            [
                "retrieve",
                "file-context",
                "/home/n/projects/trellis-ai/src/trellis/retrieve/pack_builder.py",
            ],
        )
        assert result.exit_code == 0
        assert "corpus:vault:abc" in result.stdout
        assert "PackBuilder" in result.stdout

    def test_json_output_carries_timestamps_for_the_staleness_gate(self) -> None:
        self._seed()
        result = runner.invoke(
            app,
            [
                "retrieve",
                "file-context",
                "src/trellis/retrieve/pack_builder.py",
                "--format",
                "json",
            ],
        )
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())
        assert data["status"] == "ok"
        assert data["count"] == 1
        (entry,) = data["paths"]
        assert entry["path"] == "src/trellis/retrieve/pack_builder.py"
        assert entry["documents"][0]["doc_id"] == "corpus:vault:abc"
        assert entry["entities"][0]["entity_id"] == "ent-packbuilder"
        assert entry["newest_item_at"] is not None

    def test_unknown_path_is_a_clean_empty_answer(self) -> None:
        result = runner.invoke(
            app,
            ["retrieve", "file-context", "never/ingested.py", "--format", "json"],
        )
        assert result.exit_code == 0
        data = json.loads(result.stdout.strip())
        assert data["paths"] == [
            {
                "path": "never/ingested.py",
                "documents": [],
                "entities": [],
                "newest_item_at": None,
            }
        ]

    def test_unconfirmed_mints_gated_unless_requested(self) -> None:
        self._seed(extraction_status="unconfirmed")
        args = ["retrieve", "file-context", "src/trellis/retrieve/pack_builder.py"]
        assert "PackBuilder" not in plain(runner.invoke(app, args).stdout)
        assert (
            "PackBuilder" in runner.invoke(app, [*args, "--include-unconfirmed"]).stdout
        )

    def test_quiet_leaves_long_paths_intact(self) -> None:
        """Rich wraps at 80 columns when piped, splitting absolute paths.

        A ``PreToolUse`` hook parses this output, so the raw writer is
        the one that matters for the command's actual consumer.
        """
        self._seed()
        long_path = (
            "/home/nronsse/projects/trellis-ai/src/trellis/retrieve/pack_builder.py"
        )
        result = runner.invoke(app, ["retrieve", "file-context", long_path, "-q"])
        assert result.exit_code == 0
        assert long_path in result.stdout.splitlines()

    def test_jsonl_emits_one_object_per_path(self) -> None:
        self._seed()
        result = runner.invoke(
            app,
            [
                "retrieve",
                "file-context",
                "src/trellis/retrieve/pack_builder.py",
                "never/ingested.py",
                "--format",
                "jsonl",
            ],
        )
        assert result.exit_code == 0
        rows = [json.loads(line) for line in result.stdout.strip().splitlines()]
        assert [r["path"] for r in rows] == [
            "src/trellis/retrieve/pack_builder.py",
            "never/ingested.py",
        ]
        assert rows[0]["documents"][0]["doc_id"] == "corpus:vault:abc"

    def test_unsupported_format_is_refused_not_silently_degraded(self) -> None:
        result = runner.invoke(
            app,
            ["retrieve", "file-context", "notes/foo.md", "--format", "tsv"],
        )
        assert result.exit_code == EXIT_VALIDATION
        assert "tsv" in result.stdout


class TestRetrieveHelp:
    def test_help(self) -> None:
        result = runner.invoke(app, ["retrieve", "--help"])
        assert result.exit_code == 0
        for cmd in ["pack", "search", "trace", "entity", "precedents", "file-context"]:
            assert cmd in result.stdout
