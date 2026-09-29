"""CLI configuration management."""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import ConfigDict

from trellis.core.base import TrellisModel
from trellis.stores.registry import StoreRegistry


def get_config_dir() -> Path:
    """Get Trellis config directory."""
    return Path(os.environ.get("TRELLIS_CONFIG_DIR", str(Path.home() / ".trellis")))


def get_default_data_dir() -> Path:
    """``TRELLIS_DATA_DIR``, else ``<config dir>/data``: a new config's data dir.

    ``admin init`` writes this into the ``config.yaml`` it creates, and with
    ``--force`` it does not read the file it replaces, so an unparseable one
    can still be overwritten. Code that opens the stores asks
    :func:`get_data_dir`, which honours the ``data_dir`` key.
    """
    return Path(os.environ.get("TRELLIS_DATA_DIR", str(get_config_dir() / "data")))


def get_data_dir() -> Path:
    """The data directory the stores are in, as the store registry resolves it.

    ``config.yaml``'s ``data_dir`` wins, then ``TRELLIS_DATA_DIR``, then
    ``<config dir>/data``. That is :meth:`StoreRegistry.from_config_dir`'s
    order, used by MCP, the REST API and the capture sweep, so asking it
    keeps the policy file and advisories this CLI writes where those
    surfaces read them. A ``config.yaml`` the registry refuses raises its
    ``ConfigError`` here too.
    """
    stores_dir = StoreRegistry.from_config_dir(config_dir=get_config_dir()).stores_dir
    assert stores_dir is not None  # from_config_dir always sets it
    return stores_dir.parent


class TrellisConfig(TrellisModel):
    """CLI configuration."""

    # Its values come from config.yaml, so a ValidationError names the key
    # and the expected type but never repeats the value (a mapping under
    # ``default_domain`` could be a credentials block pasted one level off).
    model_config = ConfigDict(hide_input_in_errors=True)

    data_dir: str = ""
    default_domain: str | None = None
    default_agent: str | None = None
    format: str = "text"  # text or json

    @classmethod
    def load(cls) -> TrellisConfig:
        """Load config from file or return defaults.

        ``config.yaml`` is shared: the store registry owns its plane
        blocks (``knowledge``, ``operational``, ``embeddings``, ``llm`` …),
        so only the keys this model declares are read and the rest are
        left to their owner. The model itself stays ``extra="forbid"``.

        The result therefore holds only the CLI-owned keys. Never
        round-trip it through :meth:`save`, which rewrites the whole file:
        a ``load()`` → ``save()`` would delete every plane block.
        """
        config_path = get_config_dir() / "config.yaml"
        if config_path.exists():
            data = yaml.safe_load(config_path.read_text()) or {}
            return cls(**{k: v for k, v in data.items() if k in cls.model_fields})
        # No config.yaml, so no key: the registry's dir, without building one.
        return cls(data_dir=str(get_default_data_dir()))

    def save(self, config_dir: Path | None = None) -> None:
        """Write ``config.yaml`` into ``config_dir``, default :func:`get_config_dir`."""
        if config_dir is None:
            config_dir = get_config_dir()
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path = config_dir / "config.yaml"
        config_path.write_text(yaml.dump(self.model_dump(), default_flow_style=False))
