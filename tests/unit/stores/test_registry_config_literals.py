"""config.yaml is plain YAML: a literal ``${VAR}`` refuses, a flat ``stores:`` warns.

``StoreRegistry.from_config_dir`` expands nothing, so a value written as
``${TRELLIS_NEO4J_PASSWORD}`` used to reach the neo4j driver as those very
characters, and the failure that followed named neither the key nor the
cause. It now raises a ``ConfigError`` naming every such key, before any
store is built.

The flat ``stores:`` block, removed in 0.6.0, failed more quietly still:
``_extract_store_config`` never looked at it, so a store configured only
there ran on its default backend with no signal at all. It now logs
``registry_config_flat_stores_removed``.

Fixture values are obvious fakes. A placeholder's default part is the
sentinel ``SENTINEL123``, asserted *absent* from the error, because the
message renders only the variable name.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs

from trellis.errors import ConfigError
from trellis.stores.registry import StoreRegistry

_NEO4J_CONFIG = """\
knowledge:
  graph:
    backend: neo4j
    uri: bolt://example.invalid:7687
    password: ${TRELLIS_NEO4J_PASSWORD}
"""


def _load(tmp_path: Path, text: str) -> StoreRegistry:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(text, encoding="utf-8")
    return StoreRegistry.from_config_dir(
        config_dir=config_dir, data_dir=tmp_path / "data"
    )


def _refusal(tmp_path: Path, text: str) -> ConfigError:
    with pytest.raises(ConfigError) as info:
        _load(tmp_path, text)
    return info.value


# -- A value that is only a placeholder refuses --------------------------


def test_a_literal_placeholder_refuses_and_names_the_key(tmp_path: Path) -> None:
    exc = _refusal(tmp_path, _NEO4J_CONFIG)

    assert exc.setting == "knowledge.graph.password"
    message = str(exc)
    assert (
        "knowledge.graph.password is the literal text ${TRELLIS_NEO4J_PASSWORD}"
        in message
    )
    # Which file, for an operator with more than one TRELLIS_CONFIG_DIR.
    assert str(tmp_path / "config" / "config.yaml") in message


def test_an_llm_api_key_placeholder_refuses(tmp_path: Path) -> None:
    exc = _refusal(tmp_path, "llm:\n  provider: openai\n  api_key: ${OPENAI_API_KEY}\n")

    assert exc.setting == "llm.api_key"
    assert "llm.api_key is the literal text ${OPENAI_API_KEY}" in str(exc)


@pytest.mark.parametrize("operator", [":-", "-", ":=", "=", ":?", "?", ":+", "+"])
def test_a_placeholder_with_a_default_refuses_without_echoing_it(
    tmp_path: Path, operator: str
) -> None:
    exc = _refusal(
        tmp_path,
        "knowledge:\n  graph:\n"
        f"    password: ${{TRELLIS_NEO4J_PASSWORD{operator}SENTINEL123}}\n",
    )

    assert exc.setting == "knowledge.graph.password"
    assert "${TRELLIS_NEO4J_PASSWORD}" in str(exc)
    assert "SENTINEL123" not in str(exc)


def test_a_block_scalar_placeholder_refuses(tmp_path: Path) -> None:
    """``|`` keeps a trailing newline; the value is still only a placeholder."""
    exc = _refusal(
        tmp_path, "knowledge:\n  graph:\n    password: |\n      ${SENTINEL_BLOCK}\n"
    )

    assert exc.setting == "knowledge.graph.password"


def test_every_placeholder_is_named_not_just_the_first(tmp_path: Path) -> None:
    exc = _refusal(tmp_path, _NEO4J_CONFIG + "llm:\n  api_key: ${OPENAI_API_KEY}\n")

    assert exc.setting == "knowledge.graph.password"
    message = str(exc)
    assert (
        "knowledge.graph.password is the literal text ${TRELLIS_NEO4J_PASSWORD}"
        in message
    )
    assert "llm.api_key is the literal text ${OPENAI_API_KEY}" in message


def test_a_placeholder_in_a_list_is_named_by_its_index(tmp_path: Path) -> None:
    exc = _refusal(
        tmp_path,
        "classify:\n  domain_keywords:\n    infra:\n"
        "      - kubernetes\n      - ${SENTINEL_KEYWORD}\n",
    )

    assert exc.setting == "classify.domain_keywords.infra[1]"
    assert (
        "classify.domain_keywords.infra[1] is the literal text ${SENTINEL_KEYWORD}"
        in str(exc)
    )


def test_a_self_referential_anchor_does_not_stop_the_walk(tmp_path: Path) -> None:
    """``yaml.safe_load`` builds a genuinely recursive dict from this anchor."""
    exc = _refusal(
        tmp_path, "retrieval: &loop\n  again: *loop\n  token: ${SENTINEL_TOKEN}\n"
    )

    assert exc.setting == "retrieval.token"


def test_an_aliased_block_is_named_once_at_its_anchor(tmp_path: Path) -> None:
    """The anchor is the one place to edit, and one visit bounds the walk."""
    exc = _refusal(
        tmp_path,
        "x-graph: &graph\n  backend: neo4j\n  password: ${SENTINEL_SHARED}\n"
        "knowledge:\n  graph: *graph\n  vector: *graph\n",
    )

    assert exc.setting == "x-graph.password"
    assert str(exc).count("${SENTINEL_SHARED}") == 1


# -- Anything else still loads -------------------------------------------


@pytest.mark.parametrize(
    "value", ["a${SENTINEL_B}c", "${SENTINEL_DIR}/kuzu", "bolt://${SENTINEL_HOST}"]
)
def test_a_placeholder_inside_a_longer_value_is_not_refused(
    tmp_path: Path, value: str
) -> None:
    registry = _load(
        tmp_path, f"knowledge:\n  graph:\n    backend: sqlite\n    password: {value}\n"
    )

    assert registry._config["graph"]["password"] == value


def test_a_config_without_placeholders_loads(tmp_path: Path) -> None:
    registry = _load(
        tmp_path,
        "knowledge:\n  graph:\n    backend: sqlite\n"
        "llm:\n  provider: openai\n  api_key_env: OPENAI_API_KEY\n",
    )

    assert registry._resolve_backend("graph")[0] == "sqlite"
    assert registry._llm_config == {
        "provider": "openai",
        "api_key_env": "OPENAI_API_KEY",
    }


# -- The removed flat ``stores:`` block warns ----------------------------

_FLAT_STORES = """\
stores:
  graph:
    backend: neo4j
  trace:
    backend: postgres
"""


def _load_capturing(
    tmp_path: Path, text: str
) -> tuple[StoreRegistry, list[dict[str, Any]]]:
    with capture_logs() as logs:
        registry = _load(tmp_path, text)
    return registry, [
        entry
        for entry in logs
        if entry["event"] == "registry_config_flat_stores_removed"
    ]


def test_a_flat_stores_block_warns_once_naming_the_file(tmp_path: Path) -> None:
    registry, warnings = _load_capturing(tmp_path, _FLAT_STORES)

    assert len(warnings) == 1
    assert warnings[0]["log_level"] == "warning"
    assert warnings[0]["source"] == str(tmp_path / "config" / "config.yaml")
    assert "'knowledge:'" in warnings[0]["hint"]
    assert "'operational:'" in warnings[0]["hint"]
    # Still ignored, which is what the warning says.
    assert registry._resolve_backend("graph")[0] == "sqlite"
    assert registry._resolve_backend("trace")[0] == "sqlite"


def test_a_flat_stores_block_beside_plane_blocks_still_warns(tmp_path: Path) -> None:
    registry, warnings = _load_capturing(
        tmp_path, _FLAT_STORES + "knowledge:\n  vector:\n    backend: pgvector\n"
    )

    assert len(warnings) == 1
    assert registry._resolve_backend("vector")[0] == "pgvector"
    assert registry._resolve_backend("graph")[0] == "sqlite"


def test_plane_blocks_alone_do_not_warn(tmp_path: Path) -> None:
    registry, warnings = _load_capturing(
        tmp_path,
        "knowledge:\n  graph:\n    backend: neo4j\n"
        "operational:\n  trace:\n    backend: postgres\n",
    )

    assert warnings == []
    assert registry._resolve_backend("graph")[0] == "neo4j"
    assert registry._resolve_backend("trace")[0] == "postgres"
