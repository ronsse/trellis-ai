"""Every messages-based LLM generation site uses the shared JSON parser.

The rule checks an actual parser call, not a bare import. It still cannot prove
that the parsed result belongs to the nearby generation response; #264's
per-site recipe tests remain the stronger behavioral check.
"""

from __future__ import annotations

from pathlib import Path

from tests.ast_rules import (
    CallSite,
    assert_hand_read_floor,
    assert_scan_is_not_vacuous,
    calls_to_any,
    construction_names,
    generate_call_sites,
    iter_modules,
)

ROOT = Path(__file__).parents[3]
SRC = ROOT / "src"
HAND_READ_GENERATE_SITES = 5
NOT_JSON_SITES: dict[str, str] = {}


def _violations(root: Path) -> list[str]:
    violations: list[str] = []
    sites_by_path: dict[Path, list[CallSite]] = {}
    for site in generate_call_sites(root):
        sites_by_path.setdefault(site.path, []).append(site)
    for path, tree in iter_modules(root):
        sites = sites_by_path.get(path, [])
        if not sites:
            continue
        relative = str(path.relative_to(root))
        if relative in NOT_JSON_SITES:
            continue
        parser_names = construction_names("parse_json_response", tree)
        parser_calls = calls_to_any(parser_names, tree)
        if not parser_calls:
            violations.append(f"{relative}: no parse_json_response call")
        violations.extend(
            f"{relative}:{site.lineno}: generate(**kwargs) cannot be classified"
            for site in sites
            if any(keyword.arg is None for keyword in site.node.keywords)
        )
    return violations


def _site_lines(root: Path) -> set[int]:
    return {site.lineno for site in generate_call_sites(root)}


def test_all_messages_generate_sites_use_shared_parser() -> None:
    sites = generate_call_sites(SRC)
    assert_hand_read_floor(
        len(sites),
        HAND_READ_GENERATE_SITES,
        subject="messages-based LLM generate",
    )
    assert _violations(SRC) == []


def test_advisory_generate_decoys_are_not_collected() -> None:
    sites = generate_call_sites(SRC)
    descriptions = [site.describe(SRC) for site in sites]
    assert all("days=days" not in description for description in descriptions)
    assert all(
        "advisory_generator.py" not in description for description in descriptions
    )


def test_generate_site_scan_is_not_vacuous(tmp_path: Path) -> None:
    assert_scan_is_not_vacuous(
        _site_lines,
        subject="generate",
        tmp_path=tmp_path,
        live_population=len(generate_call_sites(SRC)),
        floor=HAND_READ_GENERATE_SITES,
        args="messages=_messages",
        kwarg="messages",
        exempt={
            "partial_binding": (
                "A partial-bound method has no statically resolvable target name."
            ),
            "cross_module_subclass": (
                "Per-module name resolution cannot follow a binding defined elsewhere."
            ),
        },
    )


def test_rule_rejects_site_without_parser_and_opaque_kwargs(tmp_path: Path) -> None:
    package = tmp_path / "src"
    package.mkdir()
    (package / "missing.py").write_text(
        "async def run(client, messages):\n"
        "    return await client.generate(messages=messages)\n",
        encoding="utf-8",
    )
    (package / "opaque.py").write_text(
        "from trellis.llm.json_response import parse_json_response\n"
        "async def run(client, kwargs):\n"
        "    response = await client.generate(**kwargs)\n"
        "    return parse_json_response(response.content)\n",
        encoding="utf-8",
    )
    (package / "bound.py").write_text(
        "async def run(client, messages):\n"
        "    gen = client.generate\n"
        "    return await gen(messages=messages)\n",
        encoding="utf-8",
    )
    (package / "decoy.py").write_text(
        "def run(generator):\n    return generator.generate(days=3)\n",
        encoding="utf-8",
    )

    sites = generate_call_sites(package)
    descriptions = [site.describe(package) for site in sites]
    assert len(sites) == 3
    assert any("bound.py" in description for description in descriptions)
    assert all("decoy.py" not in description for description in descriptions)
    violations = _violations(package)
    assert any("missing.py: no parse_json_response call" in item for item in violations)
    assert any("bound.py: no parse_json_response call" in item for item in violations)
    assert any("cannot be classified" in item for item in violations)
