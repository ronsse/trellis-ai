"""CLI configuration management."""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from trellis.core.base import TrellisModel


def get_config_dir() -> Path:
    """Get Trellis config directory."""
    return Path(os.environ.get("TRELLIS_CONFIG_DIR", str(Path.home() / ".trellis")))


def get_data_dir() -> Path:
    """Get Trellis data directory."""
    return Path(os.environ.get("TRELLIS_DATA_DIR", str(get_config_dir() / "data")))


class TrellisConfig(TrellisModel):
    """CLI configuration."""

    data_dir: str = ""
    default_domain: str | None = None
    default_agent: str | None = None
    format: str = "text"  # text or json

    @classmethod
    def load(cls) -> TrellisConfig:
        """Load config from file or return defaults."""
        config_path = get_config_dir() / "config.yaml"
        if config_path.exists():
            data = yaml.safe_load(config_path.read_text()) or {}
            return cls(**data)
        return cls(data_dir=str(get_data_dir()))

    def save(self) -> None:
        """Save config to file."""
        config_dir = get_config_dir()
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path = config_dir / "config.yaml"
        config_path.write_text(yaml.dump(self.model_dump(), default_flow_style=False))
