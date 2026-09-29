"""The registry describes a config key or URI without repeating it.

A value typed where a key belongs (a DSN inside YAML flow braces, a password)
reaches the registry as a key, and a DSN missing its ``//`` parses its user
name as the URL scheme. Each case counts every 8-character window of a fake
credential, ignoring case, in the message or the log events.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pydantic
import pytest
from structlog.testing import capture_logs

from trellis.errors import ConfigError
from trellis.stores import registry as registry_module
from trellis.stores.registry import StoreRegistry, _reset_backend_cache
from trellis.stores.sqlite.document import SQLiteDocumentStore

#: A fake credential: mixed case, digits, and a letter first.
_SENTINEL = "Qm7RtVx2Lp9KzWc4NbH8sDfJ"
_WINDOWS = {_SENTINEL[i : i + 8].lower() for i in range(len(_SENTINEL) - 7)}


def _hits(text: str) -> int:
    return sum(window in text.lower() for window in _WINDOWS)


def _load(tmp_path: Path, text: str) -> StoreRegistry:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(text, encoding="utf-8")
    return StoreRegistry.from_config_dir(
        config_dir=config_dir, data_dir=tmp_path / "data"
    )


@pytest.mark.parametrize(
    "key",
    ["db_path", "db_pth", "grpah", "x-graph", "max_transaction_retry_time", "a" * 30],
    ids=["db_path", "db_pth", "grpah", "anchor", "longest_documented", "30_characters"],
)
def test_a_key_shaped_like_a_parameter_is_named(key: str) -> None:
    assert registry_module._describe_key(key) == key


@pytest.mark.parametrize(
    ("key", "described"),
    [
        (_SENTINEL, "<24-character key, not shown>"),
        (f"postgresql://u:{_SENTINEL}@h/db", "<44-character key, not shown>"),
        ("DB_PATH", "<7-character key, not shown>"),
        ("a" * 31, "<31-character key, not shown>"),
        ("deadbeef" * 4, "<32-character key, not shown>"),
        (5432, "<int key, not shown>"),
    ],
    ids=["password", "dsn", "uppercase", "31_characters", "hex_token", "int"],
)
def test_any_other_key_is_described_by_its_length_or_type(
    key: object, described: str
) -> None:
    assert registry_module._describe_key(key) == described


def test_accepted_params_reads_the_constructor() -> None:
    class Mixed:
        def __init__(self, a: int, /, b: int, *, c: int = 0) -> None: ...

    accepted = registry_module._accepted_params
    assert accepted(SQLiteDocumentStore) == frozenset({"db_path"})
    assert accepted(Mixed) == frozenset({"b", "c"})


def test_a_constructor_taking_kwargs_or_without_a_signature_decides_itself() -> None:
    class TakesAnything:
        def __init__(self, db_path: str, **kwargs: object) -> None: ...

    assert registry_module._accepted_params(TakesAnything) is None
    assert registry_module._accepted_params(dict) is None  # no signature: ValueError


def _takes_retries(init: Callable[..., None]) -> Callable[..., None]:
    @functools.wraps(init)
    def wrapper(self: object, *args: object, retries: int = 3, **kw: object) -> None:
        init(self, *args, **kw)

    return wrapper


class _WrappedInit:
    @_takes_retries
    def __init__(self, db_path: str) -> None: ...


class _Model(pydantic.BaseModel):
    db_path: str = ""


def test_a_constructor_declaring_a_narrower_signature_decides_itself() -> None:
    # inspect.signature reports what these declare, not what the call binds.
    _WrappedInit(db_path="x", retries=1)
    _Model(db_path="x", other="x")  # pydantic ignores a field it does not declare

    assert registry_module._accepted_params(_WrappedInit) is None
    assert registry_module._accepted_params(_Model) is None


def test_a_key_the_constructor_does_not_accept_is_refused_before_the_call(
    tmp_path: Path,
) -> None:
    config = {"document": {"backend": "sqlite", _SENTINEL: "x", "db_pth": "x"}}
    registry = StoreRegistry(config=config, stores_dir=tmp_path)

    with pytest.raises(ConfigError) as info:
        registry._instantiate("document")

    assert info.value.setting == "stores.document"
    assert str(info.value) == (
        "stores.document sets <24-character key, not shown>, db_pth, which the"
        " sqlite backend does not accept; it accepts: db_path"
    )
    assert list(tmp_path.iterdir()) == []  # the constructor never ran


def test_a_backend_that_takes_no_keys_says_so(tmp_path: Path) -> None:
    config = {"event_log": {"backend": "null", "db_path": "x"}}
    registry = StoreRegistry(config=config, stores_dir=tmp_path)

    with pytest.raises(ConfigError) as info:
        registry._instantiate("event_log")

    assert str(info.value) == (
        "stores.event_log sets db_path, which the null backend does not accept;"
        " it accepts: no keys"
    )


class _TakesAnything:
    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs


def test_a_plugin_constructor_taking_kwargs_receives_every_key(tmp_path: Path) -> None:
    def entry_points(*, group: str) -> list[SimpleNamespace]:
        if group != "trellis.stores.document":
            return []
        return [SimpleNamespace(name="custom", value=f"{__name__}:_TakesAnything")]

    config = {"document": {"backend": "custom", "Any-Key": "x"}}
    registry = StoreRegistry(config=config, stores_dir=tmp_path)
    with patch("trellis.plugins.loader.entry_points", side_effect=entry_points):
        _reset_backend_cache()
        try:
            store = registry._instantiate("document")
        finally:
            _reset_backend_cache()

    assert store.kwargs == {"Any-Key": "x"}


@pytest.mark.parametrize(
    "config",
    [
        f"knowledge: {{postgresql://u:{_SENTINEL}@h/db}}\n",
        f"knowledge:\n  {_SENTINEL}:\n    backend: sqlite\n",
    ],
    ids=["flow_dsn", "bare"],
)
def test_an_unknown_store_type_is_logged_without_repeating_it(
    tmp_path: Path, config: str
) -> None:
    with capture_logs() as logs:
        _load(tmp_path, config)

    [warning] = [e for e in logs if e["event"] == "registry_config_unknown_store_type"]
    assert warning["store_type"].endswith("-character key, not shown>")
    assert _hits(repr(logs)) == 0


def test_an_unknown_store_type_shaped_like_a_name_is_still_named(
    tmp_path: Path,
) -> None:
    with capture_logs() as logs:
        _load(tmp_path, "knowledge:\n  grpah:\n    backend: sqlite\n")

    [warning] = [e for e in logs if e["event"] == "registry_config_unknown_store_type"]
    assert warning["store_type"] == "grpah"


# ``urlparse`` needs ``//`` to find a host: without it the user name is the
# scheme, or the scheme is right and the credential sits in the path.
_BAD_URIS = {
    "user_as_scheme": (
        "document",
        {"backend": "postgres", "dsn": f"{_SENTINEL}:x@h/db"},
        "dsn: unexpected URL scheme for postgres backend",
    ),
    "one_slash": (
        "document",
        {"backend": "postgres", "dsn": f"postgresql:/u:{_SENTINEL}@h/db"},
        "dsn: empty network location (host:port required)",
    ),
    "no_slashes": (
        "graph",
        {"backend": "neo4j", "uri": f"neo4j:u:{_SENTINEL}@h:7687"},
        "uri: empty network location (host:port required)",
    ),
    "a_list": (
        "graph",
        {"backend": "neo4j", "uri": [_SENTINEL]},
        "uri: expected a string, got list",
    ),
}


@pytest.mark.parametrize(
    ("store_type", "store_cfg", "expected"), _BAD_URIS.values(), ids=_BAD_URIS.keys()
)
def test_a_bad_uri_is_described_without_repeating_it(
    tmp_path: Path, store_type: str, store_cfg: dict[str, object], expected: str
) -> None:
    registry = StoreRegistry(config={store_type: store_cfg}, stores_dir=tmp_path)

    [(failed, exc)] = registry._check_uri_formats([store_type])

    assert failed == store_type
    assert isinstance(exc, ConfigError)
    assert exc.setting == ("dsn" if store_type == "document" else "uri")
    assert expected in str(exc)
    assert _hits(str(exc)) == 0


@pytest.mark.parametrize(
    "backend", [{"password": _SENTINEL}, [_SENTINEL]], ids=["map", "list"]
)
def test_a_backend_that_is_not_a_string_is_left_to_instantiate(
    tmp_path: Path, backend: object
) -> None:
    registry = StoreRegistry(
        config={"document": {"backend": backend}}, stores_dir=tmp_path
    )

    assert registry._check_uri_formats(["document"]) == []
