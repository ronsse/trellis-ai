"""CLI configuration management."""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import ConfigDict

from trellis.core.base import TrellisModel


def get_config_dir() -> Path:
    """Get Trellis config directory."""
    return Path(os.environ.get("TRELLIS_CONFIG_DIR", str(Path.home() / ".trellis")))


def get_data_dir() -> Path:
    """Get Trellis data directory."""
    return Path(os.environ.get("TRELLIS_DATA_DIR", str(get_config_dir() / "data")))


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
        return cls(data_dir=str(get_data_dir()))

    def save(self, config_dir: Path | None = None) -> None:
        """Write ``config.yaml`` into ``config_dir``, default :func:`get_config_dir`."""
        if config_dir is None:
            config_dir = get_config_dir()
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path = config_dir / "config.yaml"
        config_path.write_text(yaml.dump(self.model_dump(), default_flow_style=False))
