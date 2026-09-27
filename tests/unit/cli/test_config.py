"""``TrellisConfig.load`` reads its own keys out of a shared config.yaml.

config.yaml is one file with several readers: ``StoreRegistry`` owns the
plane blocks (``knowledge``, ``operational``, ``embeddings``, ``llm`` …)
and :class:`~trellis_cli.config.TrellisConfig` owns four CLI keys. The
model is ``extra="forbid"``, so ``load()`` handing it the whole file raised
on every documented plane layout. The filter belongs in ``load()``; the
model stays strict, so a typo'd *own* key is still an error elsewhere.
"""

from __future__ import annotations

import pytest
import yaml
from pydantic import ValidationError

from trellis_cli.config import TrellisConfig

#: Blocks another reader owns, shaped like the documented layouts.
_FOREIGN_BLOCKS = {
    "knowledge": {"document": {"backend": "postgres"}},
    "operational": {"event_log": {"backend": "sqlite"}},
    "embeddings": {"provider": "openai"},
    "llm": {"provider": "openai"},
    "retrieval": {"budgets": {"default": {"max_tokens": 4000}}},
    "classify": {"domain_keywords": {"payments": ["invoice"]}},
}


def _write_config(tmp_path, monkeypatch, document: dict) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(yaml.safe_dump(document))
    monkeypatch.setenv("TRELLIS_CONFIG_DIR", str(config_dir))


def test_foreign_sections_are_ignored(tmp_path, monkeypatch):
    _write_config(tmp_path, monkeypatch, {"data_dir": "/srv/t", **_FOREIGN_BLOCKS})

    config = TrellisConfig.load()

    assert config.model_dump() == {
        "data_dir": "/srv/t",
        "default_domain": None,
        "default_agent": None,
        "format": "text",
    }


def test_own_fields_still_load_beside_foreign_sections(tmp_path, monkeypatch):
    own = {
        "data_dir": "/srv/t",
        "default_domain": "platform",
        "default_agent": "reviewer",
        "format": "json",
    }
    _write_config(tmp_path, monkeypatch, {**_FOREIGN_BLOCKS, **own})

    assert TrellisConfig.load().model_dump() == own


def test_an_own_field_of_the_wrong_type_still_raises(tmp_path, monkeypatch):
    document = {"data_dir": ["not", "a", "path"], **_FOREIGN_BLOCKS}
    _write_config(tmp_path, monkeypatch, document)

    with pytest.raises(ValidationError) as excinfo:
        TrellisConfig.load()

    # Exactly the own field: the filter drops foreign keys, not validation.
    assert [error["loc"] for error in excinfo.value.errors()] == [("data_dir",)]
