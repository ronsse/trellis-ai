"""No config.yaml value reaches operator output raw (gate-642 items 3, 4, 7b).

config.yaml carries DSNs and passwords, so every surface that reads it
describes a bad value without repeating it: ``admin health``'s backend
report, the registry's refusal of an unknown backend or of a key the backend
does not accept, the registry's parse error, and the two CLI commands that
parse the file themselves. A value typed where a key belongs (a DSN inside
YAML flow braces) is a key, so a key that does not look like one is described
by its length. Each case
plants a fake credential where a real one would sit, runs both output arms,
and requires the same exit code from each and no 8-character window of the
credential on any surface, ignoring case (``!!bool`` echoes it lowercased).
"""

from __future__ import annotations

import ast
import inspect
import json
import traceback
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.cli_output import plain
from trellis_cli import main as main_module
from trellis_cli.main import app
from trellis_cli.stores import _reset_registry

runner = CliRunner()

#: A fake credential: mixed case, digits, and a letter first, so ``!!int`` and
#: ``!!float`` cannot parse it and a lowercased echo is still counted.
_SENTINEL = "UHKT4xPE99cO7Z8mZu2QPO2c"
_WINDOWS = {_SENTINEL[i : i + 8].lower() for i in range(len(_SENTINEL) - 7)}

_NEO = (
    "  graph:\n    backend: neo4j\n    uri: bolt://h:7687\n    user: u\n"
    "    password: {S}\n"
)


def _write(path: Path, text: str | bytes) -> None:
    raw = text if isinstance(text, bytes) else text.encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw.replace(b"{S}", _SENTINEL.encode()))


def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config: str | bytes) -> Path:
    """Isolated config and data dirs; config.yaml holds ``config`` verbatim."""
    data_dir = tmp_path / "data"
    (data_dir / "stores").mkdir(parents=True)
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(data_dir))
    _write(tmp_path / "config" / "config.yaml", config)
    return data_dir


def _leaks(result) -> dict[str, int]:
    surfaces = {"stdout": result.stdout, "stderr": result.stderr}
    if result.exception is not None:
        surfaces["exception"] = str(result.exception)
        surfaces["traceback"] = "".join(traceback.format_exception(result.exception))
    return {
        name: sum(window in text.lower() for window in _WINDOWS)
        for name, text in surfaces.items()
    }


def _both(args: list[str], code: int):
    """Run ``args`` on the text arm, then the JSON arm, asserting each alike."""
    results = []
    for extra in ([], ["--format", "json"]):
        _reset_registry()
        result = runner.invoke(app, [*args, *extra])
        arm = "json" if extra else "text"
        assert not any(_leaks(result).values()), f"{arm}: {_leaks(result)}"
        assert result.exit_code == code, f"{arm}: {result.exception!r}"
        results.append(result)
    return results


def _table_rows(output: str) -> dict[str, str]:
    """``{component: status}`` read off the rendered health table."""
    rows: dict[str, str] = {}
    for line in plain(output).splitlines():
        cells = [cell.strip() for cell in line.split("│")[1:-1]]
        if len(cells) == 2:
            rows[cells[0]] = cells[1]
    return rows


# A value under ``backend`` that names no registered backend, and whole store
# blocks that are not a mapping. ``dsn`` is a DSN written as the shorthand.
_UNREGISTERED = {
    "name": "knowledge:\n  document:\n    backend: {S}\n",
    "dsn": "knowledge:\n  document: postgresql://u:{S}@h/db\n",
    "map": "knowledge:\n  document:\n    backend:\n      password: {S}\n",
    "list": "knowledge:\n  document:\n    backend: [{S}]\n",
    "null": "knowledge:\n  document:\n    backend: null\n",
    "typo": "knowledge:\n  document:\n    backend: postgress\n",
    "null_store": "knowledge:\n  document: null\n",
}


@pytest.mark.parametrize("config", _UNREGISTERED.values(), ids=_UNREGISTERED.keys())
def test_health_reports_an_unregistered_backend_without_printing_it(
    tmp_path, monkeypatch, config
):
    _env(tmp_path, monkeypatch, config)

    text, as_json = _both(["admin", "health"], 0)

    payload = json.loads(as_json.stdout)
    assert payload["backends"]["document"] is None
    # Unknown is not missing: no check turns false for it.
    assert "documents.db" not in payload
    assert _table_rows(text.stdout)["document"] == "unknown backend (not checked)"


@pytest.mark.parametrize("shape", ["name", "dsn", "map", "null_store"])
def test_opening_an_unregistered_backend_is_a_config_error_that_omits_it(
    tmp_path, monkeypatch, shape
):
    _env(tmp_path, monkeypatch, _UNREGISTERED[shape])

    _, as_json = _both(["retrieve", "search", "probe"], 5)

    assert json.loads(as_json.stdout)["error_type"] == "ConfigError"


# Each puts the secret on the line PyYAML's ``str(exc)`` quotes.
_SYNTAX = {
    "colon": "knowledge:\n" + _NEO.replace("password: {S}", "password: {S}: x"),
    "quote": "knowledge:\n" + _NEO.replace("password: {S}", 'password: "{S}'),
    "long_line": "knowledge:\n"
    + _NEO.replace("password: {S}", "password: " + "x" * 70 + "{S}: x"),
    "long_dsn": "knowledge:\n  document:\n    backend: postgres\n"
    "    dsn: postgresql://u:" + "x" * 70 + "{S}@h/db: x\n",
    "tab": "knowledge:\n  graph:\n    backend: neo4j\n    auth_blob: {S}\t: [\n",
    "flow": "llm:\n  api_key: [{S}\n",
    "unknown_key": "knowledge:\n  vector:\n    backend: pgvector\n"
    "    conn_str: {S}: x\n",
    # PyYAML quotes the alias and the tag with ``%r``.
    "alias": "knowledge:\n  graph: *{S}\n",
    "tag": "knowledge:\n  graph:\n    b: !!python/name:os.{S}\n",
}


@pytest.mark.parametrize("config", _SYNTAX.values(), ids=_SYNTAX.keys())
def test_a_yaml_error_names_its_line_without_quoting_it(tmp_path, monkeypatch, config):
    _env(tmp_path, monkeypatch, config)

    text, as_json = _both(["admin", "health"], 5)

    assert json.loads(as_json.stdout)["error_type"] == "ConfigError"
    # The JSON arm's sanitizer can suppress the whole message, so the
    # position is read off the text arm.
    assert "(line " in " ".join(plain(text.stdout).split())


_TAGS = {
    "int": "knowledge:\n  graph:\n    backend: neo4j\n    port: !!int {S}\n",
    "float": "knowledge:\n  graph:\n    backend: neo4j\n    port: !!float {S}\n",
    "bool": "knowledge:\n  graph:\n    backend: neo4j\n    tls: !!bool {S}\n",
}


@pytest.mark.parametrize("config", _TAGS.values(), ids=_TAGS.keys())
def test_an_explicit_tag_that_cannot_construct_is_a_config_error(
    tmp_path, monkeypatch, config
):
    _env(tmp_path, monkeypatch, config)

    _, as_json = _both(["admin", "health"], 5)

    assert json.loads(as_json.stdout)["error_type"] == "ConfigError"


_NOT_A_CONFIG = {
    "top_level_list": "- {S}\n",
    "top_level_scalar": "{S}\n",
    # {S} ends at the bad byte, so quoting the bytes near the offset prints it.
    "not_utf8": b"knowledge:\n  graph:\n    backend: neo4j\n  x: {S}\xff\xfe\n",
    "bad_date": "knowledge:\n" + _NEO + "stamp: 2026-13-45\n",
    "timestamp_tag": "knowledge:\n  graph:\n    t: !!timestamp {S}\n",
}


@pytest.mark.parametrize("config", _NOT_A_CONFIG.values(), ids=_NOT_A_CONFIG.keys())
def test_a_file_that_is_not_a_config_mapping_is_a_config_error(
    tmp_path, monkeypatch, config
):
    _env(tmp_path, monkeypatch, config)

    _, as_json = _both(["admin", "health"], 5)

    assert json.loads(as_json.stdout)["error_type"] == "ConfigError"


_WRONG_TYPE = {
    "domain_map": "default_domain:\n  k: {S}\n",
    "agent_list": "default_agent: [{S}]\n",
    "format_map": "format:\n  k: {S}\n",
}


@pytest.mark.parametrize("config", _WRONG_TYPE.values(), ids=_WRONG_TYPE.keys())
def test_a_cli_key_of_the_wrong_type_is_not_repeated(tmp_path, monkeypatch, config):
    # Still a traceback (exit 1): turning it into a ConfigError is a follow-up.
    _env(tmp_path, monkeypatch, config)

    _both(["admin", "health"], 1)


_GRAPH_CONFIGS = {
    "syntax": "graph:\n  backend: neo4j\n  password: " + "x" * 30 + "{S}: x\n",
    "int_tag": "graph:\n  backend: neo4j\n  password: !!int {S}\n",
}


@pytest.mark.parametrize("config", _GRAPH_CONFIGS.values(), ids=_GRAPH_CONFIGS.keys())
def test_migrate_graph_names_a_bad_config_without_quoting_it(
    tmp_path, monkeypatch, config
):
    _env(tmp_path, monkeypatch, "{}\n")
    _write(tmp_path / "from.yaml", config)
    _write(tmp_path / "to.yaml", "graph:\n  backend: sqlite\n")
    args = ["admin", "migrate-graph", "--from-config", str(tmp_path / "from.yaml")]
    args += ["--to-config", str(tmp_path / "to.yaml"), "--dry-run"]

    _both(args, 2)


_AUTO_PROMOTE = "learning:\n  auto_promote:\n    enabled: true\n"
_TUNE_CONFIGS = {
    "syntax": "knowledge:\n  graph:\n    password: " + "x" * 30 + "{S}: x\n",
    "int_tag": "knowledge:\n  graph:\n    password: !!int {S}\n",
    "not_an_int": _AUTO_PROMOTE + "    min_sample_size: {S}\n",
    "not_a_number": _AUTO_PROMOTE + "    min_effect_size: {S}\n",
    "not_a_bool": "learning:\n  auto_promote:\n    enabled: {S}\n",
}


@pytest.mark.parametrize("config", _TUNE_CONFIGS.values(), ids=_TUNE_CONFIGS.keys())
def test_worker_tune_names_a_bad_config_without_quoting_it(
    tmp_path, monkeypatch, config
):
    _env(tmp_path, monkeypatch, config)

    _both(["worker", "tune"], 1)


# Keys the backend's constructor does not take. ``flow_dsn`` is a DSN inside
# YAML flow braces, which parses as a key with a null value.
_UNACCEPTED_KEY = {
    "flow_dsn": "knowledge:\n  document: {postgresql://u:{S}@h/db}\n",
    "bare": "knowledge:\n  document:\n    backend: sqlite\n    {S}: x\n",
}


@pytest.mark.parametrize("config", _UNACCEPTED_KEY.values(), ids=_UNACCEPTED_KEY.keys())
def test_a_key_the_backend_does_not_accept_is_a_config_error_that_omits_it(
    tmp_path, monkeypatch, config
):
    _env(tmp_path, monkeypatch, config)

    text, as_json = _both(["retrieve", "search", "probe"], 5)

    assert json.loads(as_json.stdout)["error_type"] == "ConfigError"
    assert (
        "not shown>, which the sqlite backend does not accept; it accepts: db_path"
        in (plain(text.stdout))
    )


def test_a_key_shaped_like_a_parameter_is_still_named(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch, "knowledge:\n  document:\n    db_pth: x\n")

    text, _ = _both(["retrieve", "search", "probe"], 5)

    assert "stores.document sets db_pth, which the sqlite backend" in plain(text.stdout)


def test_migrate_graph_refuses_a_key_the_backend_does_not_accept(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch, "{}\n")
    _write(
        tmp_path / "from.yaml", "graph: {backend: sqlite, postgresql://u:{S}@h/db}\n"
    )
    _write(tmp_path / "to.yaml", "graph:\n  backend: sqlite\n")
    args = ["admin", "migrate-graph", "--from-config", str(tmp_path / "from.yaml")]
    args += ["--to-config", str(tmp_path / "to.yaml"), "--dry-run"]

    _, as_json = _both(args, 5)

    assert json.loads(as_json.stdout)["error_type"] == "ConfigError"


def test_a_placeholder_under_a_dsn_written_as_a_key_omits_the_key(
    tmp_path, monkeypatch
):
    _env(
        tmp_path,
        monkeypatch,
        "knowledge:\n  document:\n    postgresql://u:{S}@h/db: ${PGPASS}\n",
    )

    text, _ = _both(["admin", "health"], 5)

    assert (
        "knowledge.document.<44-character key, not shown> is the literal text ${PGPASS}"
        in plain(text.stdout)
    )


def test_the_root_app_never_renders_locals():
    """Typer below 0.23 prints every frame's locals, and ``typer>=0.9`` admits it."""
    assert app.pretty_exceptions_show_locals is False
    # The attribute is False under today's Typer whatever main.py says, so
    # the pin is on the argument itself.
    [call] = [
        node.value
        for node in ast.parse(inspect.getsource(main_module)).body
        if isinstance(node, ast.Assign)
        and any(getattr(target, "id", None) == "app" for target in node.targets)
    ]
    keywords = {keyword.arg: keyword.value for keyword in call.keywords}
    assert isinstance(keywords["pretty_exceptions_show_locals"], ast.Constant)
    assert keywords["pretty_exceptions_show_locals"].value is False
