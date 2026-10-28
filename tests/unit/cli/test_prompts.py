"""The shipped loop text: package data, ``show-prompt``, the skill and the page.

Three consumers get this text instead of a skill (a model called with an API
key, a framework's system message, a dispatched subagent), so a wrong tool or
field name in it is not a typo: the agent calls the wrong thing, or grades a
pack with feedback the learning join discards. The names are therefore pinned
to the schemas that define them in
``tests/unit/mcp/test_shipped_prompt_claims.py``. This module pins what the
text must *say*, and that every copy of it (the CLI's output, the skill, the
docs page) is the package file and not a second draft.
"""

from __future__ import annotations

import json
import re
from importlib.resources import files
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.cli_output import force_colour, plain
from trellis_cli import admin
from trellis_cli.main import app
from trellis_cli.prompts import (
    DEFAULT_VARIANT,
    PROMPT_VARIANTS,
    agent_rule_text,
    load_prompt,
)
from trellis_cli.skills import SKILL_NAMES

runner = CliRunner()

REPO_ROOT = Path(__file__).resolve().parents[3]
PAGE = REPO_ROOT / "docs" / "getting-started" / "system-prompt.md"

#: Hand-written on purpose: the list says what the text must teach. The
#: sibling module checks that each name is real.
NAMED_BY_EVERY_VARIANT = (
    "get_context",
    "record_feedback",
    "pack_id",
    "helpful_item_ids",
    "unhelpful_item_ids",
)

#: What only the full system prompt must carry: the write step, and the one
#: trap that rejects most traces.
NAMED_BY_SYSTEM = ("save_experience", "`result` is a JSON object")


def missing_from(variant: str, text: str) -> list[str]:
    """Return what ``text`` must say as ``variant`` and does not."""
    required = list(NAMED_BY_EVERY_VARIANT)
    if variant == "system":
        required += NAMED_BY_SYSTEM
    # Whole-name match: ``unhelpful_item_ids`` contains ``helpful_item_ids``,
    # so a plain substring test passes a text that lost the first.
    return [
        name
        for name in required
        if not re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text)
    ]


def _flat(variant: str) -> str:
    """Return a variant's text with line breaks folded to single spaces."""
    return " ".join(load_prompt(variant).split())


class TestPackageData:
    @pytest.mark.parametrize("variant", PROMPT_VARIANTS)
    def test_each_variant_is_readable_as_a_resource(self, variant):
        # The path ``load_prompt`` takes, so a wheel that drops the file fails
        # here and not in front of the adopter.
        text = (files("trellis_cli.prompts") / f"{variant}.md").read_text()
        assert text == load_prompt(variant)
        assert text.endswith("\n")
        assert not text.endswith("\n\n")

    def test_the_variants_are_the_three_files(self):
        shipped = {
            p.name.removesuffix(".md")
            for p in Path(str(files("trellis_cli.prompts"))).glob("*.md")
        }
        assert shipped == set(PROMPT_VARIANTS)

    def test_the_default_is_the_system_prompt(self):
        assert DEFAULT_VARIANT == "system"

    def test_an_unknown_variant_raises(self):
        with pytest.raises(ValueError, match="unknown prompt variant"):
            load_prompt("../system")

    def test_the_agent_rule_stays_a_paragraph(self):
        # It is pasted into every agent definition, so it is paid for on
        # every subagent call.
        assert len(load_prompt("agent-rule").split()) < 80

    def test_the_prompts_are_not_installed_as_a_skill(self):
        # Prompts live beside the skills, not among them, so
        # ``install-skills`` never copies a non-skill into a skills directory.
        assert not set(PROMPT_VARIANTS) & set(SKILL_NAMES)
        skills_dir = Path(str(files("trellis_cli.skills")))
        assert {p.name for p in skills_dir.iterdir() if p.is_dir()} - {
            "__pycache__"
        } == set(SKILL_NAMES)


class TestThePinnedText:
    @pytest.mark.parametrize("variant", PROMPT_VARIANTS)
    def test_every_variant_names_what_it_must(self, variant):
        assert missing_from(variant, load_prompt(variant)) == []

    @pytest.mark.parametrize(
        ("variant", "mutate"),
        [
            ("system", lambda t: t.replace("unhelpful_item_ids", "unhelpful_ids")),
            ("system", lambda t: re.sub(r"(?<!\w)helpful_item_ids", "helpful_ids", t)),
            ("system", lambda t: t.replace("pack_id", "pack")),
            ("system", lambda t: t.replace("save_experience", "save_trace")),
            ("system", lambda t: t.replace("`result` is a JSON object", "result")),
            ("system", lambda t: t.replace("get_context", "get_pack")),
            ("claude-md", lambda t: t.replace("record_feedback", "record_rating")),
            ("agent-rule", lambda t: t.replace("helpful_item_ids=[...],", "")),
        ],
    )
    def test_a_defective_copy_is_caught(self, variant, mutate):
        """The pin is proven against copies that each lose one thing."""
        text = load_prompt(variant)
        defective = mutate(text)
        assert defective != text
        assert missing_from(variant, defective), "the check did not notice"

    def test_the_subagent_wording_is_where_a_subagent_reads_it(self):
        # The rule exists because a subagent reads no skill. The CLAUDE.md
        # variant says it applies to subagents; the rule is the paragraph for
        # an agent definition and says retrieval is part of the task.
        assert "dispatched subagent" in load_prompt("claude-md")
        assert "not scope creep" in load_prompt("agent-rule")

    def test_the_empty_pack_wording_holds_whichever_way_the_server_answers(self):
        # A pack with no items may or may not carry a pack_id, so the text
        # makes grading conditional on one having been shown and never says
        # an empty pack has none.
        # Prose is hard-wrapped, so compare with whitespace folded.
        assert "unless a pack_id was shown" in _flat("system")
        assert "only if a pack_id was shown" in _flat("agent-rule")
        for variant in PROMPT_VARIANTS:
            assert "has no pack_id" not in load_prompt(variant)


class TestShowPrompt:
    def test_prints_the_system_prompt_by_default(self):
        result = runner.invoke(app, ["admin", "show-prompt"])
        assert result.exit_code == 0
        assert result.stdout == load_prompt("system")

    @pytest.mark.parametrize("colour", [False, True])
    @pytest.mark.parametrize("variant", PROMPT_VARIANTS)
    def test_text_is_the_package_file_byte_for_byte(self, monkeypatch, variant, colour):
        """Rich would delete ``[...]`` as markup and fold long lines.

        ``COLUMNS`` is far below the longest line, and the coloured pass
        guarantees a styling console, so equality here means markup,
        highlighting and wrapping are all off.
        """
        if colour:
            force_colour(monkeypatch, admin)
        monkeypatch.setenv("COLUMNS", "30")
        result = runner.invoke(app, ["admin", "show-prompt", "--variant", variant])
        assert result.exit_code == 0
        assert result.stdout == load_prompt(variant)
        assert "\x1b" not in result.stdout

    @pytest.mark.parametrize("variant", PROMPT_VARIANTS)
    def test_json_carries_the_variant_and_the_text(self, variant):
        result = runner.invoke(
            app, ["admin", "show-prompt", "--variant", variant, "--format", "json"]
        )
        assert result.exit_code == 0
        assert json.loads(result.stdout) == {
            "variant": variant,
            "text": load_prompt(variant),
        }

    def test_json_without_a_variant_is_the_system_prompt(self):
        result = runner.invoke(app, ["admin", "show-prompt", "--format", "json"])
        assert json.loads(result.stdout)["variant"] == "system"

    @pytest.mark.parametrize("output_format", ["text", "json"])
    def test_an_unknown_variant_exits_two_in_both_formats(self, output_format):
        result = runner.invoke(
            app,
            ["admin", "show-prompt", "--variant", "[nope]", "--format", output_format],
        )
        assert result.exit_code == 2
        shown = plain(result.stdout)
        assert "system, claude-md, agent-rule" in shown
        # The value is quoted as typed, brackets included.
        assert "[nope]" in shown


class TestTheSkill:
    def _skill(self) -> str:
        return (files("trellis_cli.skills") / "trellis-loop" / "SKILL.md").read_text()

    def test_carries_the_rule_verbatim(self):
        assert agent_rule_text() in self._skill()

    def test_says_where_to_add_it_and_where_the_rest_is(self):
        body = self._skill()
        assert "every agent definition" in body
        assert "CLAUDE.md" in body
        assert "trellis admin show-prompt" in body

    def test_is_version_one(self):
        assert "\nversion: 1.0.0\n" in self._skill()


class TestTheDocsPage:
    def _page(self) -> str:
        return PAGE.read_text()

    @pytest.mark.parametrize("variant", PROMPT_VARIANTS)
    def test_the_page_holds_the_package_text(self, variant):
        """One source of truth: a page that drifted would teach a second loop."""
        assert load_prompt(variant) in self._page()

    def test_the_page_says_what_is_not_known(self):
        page = " ".join(self._page().split())
        assert "forwards the MCP server's `instructions`" in page

    def test_the_page_names_every_host_it_tells_you_to_paste_into(self):
        page = self._page()
        for host in (
            "Messages API",
            "OpenAI",
            "Cursor",
            "Claude Desktop",
            "LangGraph",
        ):
            assert host in page

    def test_the_decision_page_and_the_readme_link_to_it(self):
        for doc in (
            REPO_ROOT / "docs" / "getting-started" / "integrate-your-agent.md",
            REPO_ROOT / "README.md",
        ):
            assert "system-prompt.md" in doc.read_text(), doc.name
