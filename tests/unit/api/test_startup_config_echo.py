"""API startup refuses a bad config.yaml store block without repeating it.

The lifespan runs the real ``StoreRegistry.validate``, which aggregates every
store's failure into one ``RegistryValidationError`` and logs each one. A DSN
inside YAML flow braces is a key the backend does not accept, and a backend
written as a mapping used to crash the URI check before that aggregation.
"""

from __future__ import annotations

import traceback
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

import trellis_api.app as app_module
from trellis.stores.registry import RegistryValidationError
from trellis_api.app import create_app

#: A fake credential: mixed case, digits, and a letter first.
_SENTINEL = "Jw5NcR8tHq2VzLm6KpX3dYbF"
_WINDOWS = {_SENTINEL[i : i + 8].lower() for i in range(len(_SENTINEL) - 7)}

_CONFIGS = {
    "dsn_as_key": f"knowledge:\n  document: {{postgresql://u:{_SENTINEL}@h/db}}\n",
    "backend_map": f"knowledge:\n  document:\n    backend: {{password: {_SENTINEL}}}\n",
}


def _hits(text: str) -> int:
    return sum(window in text.lower() for window in _WINDOWS)


@pytest.mark.parametrize("config", _CONFIGS.values(), ids=_CONFIGS.keys())
def test_startup_names_the_store_without_repeating_its_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    config: str,
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(config, encoding="utf-8")
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("TRELLIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(app_module, "_registry", None)

    with (
        capture_logs() as logs,
        pytest.raises(RegistryValidationError) as info,
        TestClient(create_app()),
    ):
        pass

    [(store_type, exc)] = info.value.errors
    assert store_type == "document"
    assert type(exc).__name__ == "ConfigError"
    assert any(e["event"] == "store_registry_validation_failed" for e in logs)
    assert _hits(str(info.value)) == 0
    assert _hits("".join(traceback.format_exception(info.value))) == 0
    assert _hits(repr(logs)) == 0
    captured = capsys.readouterr()
    assert _hits(captured.out + captured.err) == 0
