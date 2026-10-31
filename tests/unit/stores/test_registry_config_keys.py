"""config.yaml refuses what nothing reads, and warns about a DSN nothing uses.

A misspelled plane (``knowlege:``), a store name no plane has, a store under
the wrong plane and the removed flat ``stores:`` block each matched nothing
below ``from_config_dir``, so their stores ran on the default sqlite backend
while health, readiness and ``admin stats`` all reported normal. Each is a
``ConfigError`` now, like the nested ``backnd`` and ``postgress`` cases beside
them.

The closed set of top-level keys is pinned here against its readers: a key a
reader later adds, and does not add to ``_CONFIG_DOCUMENT_KEYS``, stops every
surface at startup, which is what these tests exist to catch.

Fixture words are generic; the DSN carries a fake credential asserted absent
from every log line.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from structlog.testing import capture_logs

from trellis.errors import ConfigError
from trellis.stores import registry as registry_module
from trellis.stores.registry import StoreRegistry
from trellis_cli.config import TrellisConfig
from trellis_cli.worker import LEARNING_CONFIG_SECTION

_KNOWLEDGE_DSN = "TRELLIS_KNOWLEDGE_PG_DSN"
_OPERATIONAL_DSN = "TRELLIS_OPERATIONAL_PG_DSN"
_SECRET = "Qx7LmV2pTz9RkWc4"  # noqa: S105
_DSN_VALUE = f"postgresql://user:{_SECRET}@db.invalid:5432/app"


def _load(tmp_path: Path, text: str | None) -> StoreRegistry:
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    if text is not None:
        (config_dir / "config.yaml").write_text(text, encoding="utf-8")
    return StoreRegistry.from_config_dir(
        config_dir=config_dir, data_dir=tmp_path / "data"
    )


@pytest.fixture(autouse=True)
def _clean_dsn_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_KNOWLEDGE_DSN, raising=False)
    monkeypatch.delenv(_OPERATIONAL_DSN, raising=False)
    monkeypatch.setattr(registry_module, "_WARNED_UNUSED_PLANE_DSNS", set())


# -- The allowlist is pinned against its readers --------------------------


def test_the_allowlist_holds_every_key_a_reader_takes() -> None:
    keys = registry_module._CONFIG_DOCUMENT_KEYS
    # ``trellis_cli.config.TrellisConfig`` reads these from the same file.
    assert set(TrellisConfig.model_fields) <= keys
    # ``trellis_cli.worker`` reads ``learning.auto_promote``.
    assert LEARNING_CONFIG_SECTION in keys
    # The registry's own planes and sections.
    assert {
        "knowledge",
        "operational",
        "embeddings",
        "retrieval",
        "llm",
        "classify",
        "data_dir",
    } <= keys


def test_the_allowlist_is_exactly_the_documented_keys() -> None:
    """Widening it is a deliberate edit, so the whole set is spelled out."""
    expected = {
        "knowledge",
        "operational",
        "embeddings",
        "retrieval",
        "llm",
        "classify",
        "data_dir",
        "default_domain",
        "default_agent",
        "format",
        "learning",
    }
    assert expected == registry_module._CONFIG_DOCUMENT_KEYS


# -- Refusals -------------------------------------------------------------


@pytest.mark.parametrize(
    ("config", "setting", "needle"),
    [
        (
            "knowlege:\n  document:\n    backend: postgres\n",
            "knowlege",
            "'knowlege'",
        ),
        (
            "storage:\n  graph:\n    backend: neo4j\n",
            "storage",
            "'storage'",
        ),
        (
            "knowledge:\n  graph:\n    backend: sqlite\nopperational: {}\n",
            "opperational",
            "'opperational'",
        ),
        ("Knowledge: {}\n", "<9-character key, not shown>", "<9-character key"),
        ("5: x\n", "<int key, not shown>", "<int key, not shown>"),
        (
            "knowledge:\n  documents:\n    backend: postgres\n",
            "knowledge.documents",
            "'knowledge:' has no store named 'documents'",
        ),
        (
            "operational:\n  traces:\n    backend: postgres\n",
            "operational.traces",
            "'operational:' has no store named 'traces'",
        ),
        (
            "knowledge:\n  trace:\n    backend: postgres\n",
            "knowledge.trace",
            "'trace' is a store of the operational plane, not the knowledge plane",
        ),
        (
            "operational:\n  graph:\n    backend: neo4j\n",
            "operational.graph",
            "'graph' is a store of the knowledge plane, not the operational plane",
        ),
        (
            "stores:\n  graph:\n    backend: neo4j\n",
            "stores",
            "flat 'stores:' block",
        ),
    ],
    ids=[
        "typo_plane",
        "storage_plane",
        "typo_second_plane",
        "wrong_case",
        "non_string_key",
        "unknown_knowledge_store",
        "unknown_operational_store",
        "trace_under_knowledge",
        "graph_under_operational",
        "flat_stores",
    ],
)
def test_a_config_nothing_reads_is_refused(
    tmp_path: Path, config: str, setting: str, needle: str
) -> None:
    with pytest.raises(ConfigError) as info:
        _load(tmp_path, config)

    assert info.value.setting == setting
    assert needle in str(info.value)
    assert str(tmp_path / "config" / "config.yaml") in str(info.value)


def test_every_stray_top_level_key_is_listed_with_the_keys_read(
    tmp_path: Path,
) -> None:
    with pytest.raises(ConfigError) as info:
        _load(tmp_path, "knowlege: {}\nstorage: {}\nformat: json\n")

    message = str(info.value)
    assert "'knowlege', 'storage'" in message
    assert "format" in message.split("Keys read:")[1]
    for key in sorted(registry_module._CONFIG_DOCUMENT_KEYS):
        assert key in message


# -- What still loads -----------------------------------------------------


def test_every_misplaced_store_in_a_plane_is_listed(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as excinfo:
        _load(tmp_path, "knowledge:\n  trace: {}\n  event_log: {}\n")

    assert "'trace'" in str(excinfo.value)
    assert "'event_log'" in str(excinfo.value)


def test_the_constructor_refuses_a_flat_stores_block_beside_a_plane() -> None:
    """The file-level key check does not run here, only the plane-level one."""
    with pytest.raises(ConfigError, match="flat 'stores:' block"):
        StoreRegistry(config={"knowledge": {}, "stores": {}})


def test_every_allowlisted_key_loads(tmp_path: Path) -> None:
    config = (
        "knowledge:\n  graph:\n    backend: sqlite\n"
        "operational:\n  trace:\n    backend: sqlite\n"
        "embeddings: {}\nretrieval: {}\nllm: {}\nclassify: {}\n"
        "learning:\n  auto_promote:\n    enabled: false\n"
        f"data_dir: {tmp_path / 'elsewhere'}\n"
        "default_domain: demo\ndefault_agent: demo-agent\nformat: json\n"
    )

    registry = _load(tmp_path, config)

    assert registry.configured_backend("graph") == "sqlite"
    assert registry.stores_dir == tmp_path / "elsewhere" / "stores"


@pytest.mark.parametrize("config", [None, "", "{}\n", "# only a comment\n"])
def test_no_config_and_an_empty_config_load(tmp_path: Path, config: str | None) -> None:
    assert _load(tmp_path, config).configured_backend("graph") == "sqlite"


def test_an_extension_key_holding_an_anchor_loads(tmp_path: Path) -> None:
    registry = _load(
        tmp_path,
        "x-pg: &pg\n  backend: postgres\n"
        "knowledge:\n  document: *pg\n"
        "operational:\n  trace: *pg\n",
    )

    assert registry.configured_backend("document") == "postgres"
    assert registry.configured_backend("trace") == "postgres"


def test_a_plane_block_with_a_null_value_loads(tmp_path: Path) -> None:
    assert _load(tmp_path, "knowledge:\noperational:\n")


# -- The unused-DSN warning ------------------------------------------------


def _unused(logs: list[dict[str, object]]) -> list[dict[str, object]]:
    return [e for e in logs if e["event"] == "registry_plane_dsn_unused"]


def test_a_dsn_beside_a_default_config_warns_for_that_plane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_KNOWLEDGE_DSN, _DSN_VALUE)

    with capture_logs() as logs:
        _load(tmp_path, None)

    [warning] = _unused(logs)
    assert warning["log_level"] == "warning"
    assert warning["plane"] == "knowledge"
    assert warning["env_var"] == _KNOWLEDGE_DSN
    assert "backend: postgres" in str(warning["hint"])
    assert _SECRET not in repr(logs)


def test_each_plane_is_judged_on_its_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_KNOWLEDGE_DSN, _DSN_VALUE)
    monkeypatch.setenv(_OPERATIONAL_DSN, _DSN_VALUE)

    with capture_logs() as logs:
        _load(tmp_path, "knowledge:\n  document:\n    backend: postgres\n")

    # The knowledge DSN has a reader; the operational one has none.
    assert [e["plane"] for e in _unused(logs)] == ["operational"]


def test_both_dsns_beside_a_default_config_warn_once_each(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_KNOWLEDGE_DSN, _DSN_VALUE)
    monkeypatch.setenv(_OPERATIONAL_DSN, _DSN_VALUE)

    with capture_logs() as logs:
        _load(tmp_path, "{}\n")

    assert sorted(e["plane"] for e in _unused(logs)) == ["knowledge", "operational"]


@pytest.mark.parametrize(
    "config",
    [
        "knowledge:\n  document:\n    backend: postgres\n",
        "knowledge:\n  vector:\n    backend: pgvector\n",
        (
            "knowledge:\n  graph:\n    backend: arcadedb\n"
            "  document:\n    backend: postgres\n"
        ),
        "knowledge:\n  document:\n    backend: postgres\n    dsn: x://explicit\n",
    ],
    ids=["document", "vector", "graph_elsewhere_document_postgres", "explicit_dsn"],
)
def test_one_postgres_store_in_the_plane_is_a_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config: str
) -> None:
    monkeypatch.setenv(_KNOWLEDGE_DSN, _DSN_VALUE)

    with capture_logs() as logs:
        _load(tmp_path, config)

    assert _unused(logs) == []


def test_a_dsn_for_the_other_plane_is_not_a_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_OPERATIONAL_DSN, _DSN_VALUE)

    with capture_logs() as logs:
        _load(tmp_path, "knowledge:\n  document:\n    backend: postgres\n")

    assert [e["plane"] for e in _unused(logs)] == ["operational"]


@pytest.mark.parametrize("value", [None, ""], ids=["unset", "empty"])
def test_no_dsn_no_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    if value is not None:
        monkeypatch.setenv(_KNOWLEDGE_DSN, value)

    with capture_logs() as logs:
        _load(tmp_path, None)

    assert _unused(logs) == []


def test_a_backend_that_is_not_a_name_is_left_to_the_other_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_KNOWLEDGE_DSN, _DSN_VALUE)

    with capture_logs() as logs:
        _load(tmp_path, f"knowledge:\n  document:\n    backend: {{pw: {_SECRET}}}\n")

    assert _unused(logs) == []


def test_the_warning_fires_once_per_plane_per_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_KNOWLEDGE_DSN, _DSN_VALUE)

    with capture_logs() as logs:
        _load(tmp_path, None)
        _load(tmp_path, None)

    assert len(_unused(logs)) == 1
