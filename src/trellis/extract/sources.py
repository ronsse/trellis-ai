"""Source registration via ``sources.yaml``.

A ``sources.yaml`` file is the optional, declarative entrypoint for
multi-source ingestion. Each entry pins one upstream system Trellis
should pull from: the extractor type to use, where to read from (local
path or remote endpoint), and an env-var name resolving to credentials
if needed. The registry is consumed by ``trellis extract refresh`` and
the demo bootstrap path; per-source ad-hoc invocations (``trellis ingest
dbt-manifest <path>``) continue to work without it.

Trellis does NOT execute orchestration on top of this file. The registry
declares *what* to ingest; users wire *when* via their own scheduler
(cron, Airflow, GHA, Dagster). See
``docs/agent-guide/freshness-and-curation.md``.

Example
-------

.. code-block:: yaml

    sources:
      - name: jaffle-dbt
        type: dbt-manifest
        path: ./fixtures/dbt/manifest.json
      - name: lineage-events
        type: openlineage
        path: ./fixtures/openlineage/events.jsonl
        enabled: true
      - name: streaming-events
        type: openlineage
        endpoint: https://lineage.example.com/api/v1/events
        credentials_ref: TRELLIS_LINEAGE_TOKEN
        enabled: false

Hard rules
~~~~~~~~~~

* Each entry MUST have exactly one of ``path`` or ``endpoint``.
* ``name`` MUST be unique across the file (the refresh CLI looks up by name).
* ``credentials_ref`` is an env-var name, never an inline secret. The
  pattern enforces uppercase + underscores so accidental inline secrets
  surface at validation time, not at extraction time.
* Unrecognised keys are rejected (``extra="forbid"`` via
  :class:`~trellis.core.base.TrellisModel`).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, model_validator

from trellis.core.base import TrellisModel
from trellis.extract.base import ExtractorTier

_ENV_VAR_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")
_VALID_NAME_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]*$")


class SourceEntry(TrellisModel):
    """One declared upstream source.

    The combination of ``type`` and ``path``/``endpoint`` tells the
    dispatcher which extractor to run and what raw input to feed it.
    """

    name: str = Field(
        description=(
            "Unique identifier used by `trellis extract refresh --source <name>`. "
            "Letters, digits, underscore, dash; must start with a letter."
        )
    )
    type: str = Field(
        description=(
            "Source type matching an extractor's `supported_sources` entry "
            "(e.g., 'dbt-manifest', 'openlineage')."
        )
    )
    path: str | None = Field(
        default=None,
        description="Local filesystem path. Exactly one of path/endpoint required.",
    )
    endpoint: str | None = Field(
        default=None,
        description="Remote URL. Exactly one of path/endpoint required.",
    )
    credentials_ref: str | None = Field(
        default=None,
        description=(
            "Name of the env var (or secrets-manager key) holding credentials. "
            "Must be uppercase + underscores so accidental inline secrets surface "
            "as a validation error."
        ),
    )
    enabled: bool = Field(
        default=True,
        description="Set false to keep an entry on file but skip it during refresh.",
    )
    tier_override: ExtractorTier | None = Field(
        default=None,
        description=(
            "Force a specific extractor tier for this source. Rarely needed; the "
            "dispatcher's tier-priority routing handles most cases."
        ),
    )

    @model_validator(mode="after")
    def _validate_name(self) -> SourceEntry:
        if not _VALID_NAME_PATTERN.match(self.name):
            msg = (
                f"SourceEntry.name {self.name!r} must match "
                f"[a-zA-Z][a-zA-Z0-9_-]* — letters, digits, underscore, dash; "
                f"first char a letter."
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _validate_xor_path_endpoint(self) -> SourceEntry:
        has_path = self.path is not None
        has_endpoint = self.endpoint is not None
        if has_path == has_endpoint:
            msg = (
                f"SourceEntry {self.name!r} must declare exactly one of "
                f"'path' or 'endpoint' (got "
                f"path={self.path!r}, endpoint={self.endpoint!r})."
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _validate_credentials_ref(self) -> SourceEntry:
        if self.credentials_ref is None:
            return self
        if not _ENV_VAR_PATTERN.match(self.credentials_ref):
            msg = (
                f"SourceEntry {self.name!r}: credentials_ref "
                f"{self.credentials_ref!r} must look like an env-var name "
                f"(uppercase letters, digits, underscore; first char a letter). "
                f"Never inline secret values."
            )
            raise ValueError(msg)
        return self


class SourcesConfig(TrellisModel):
    """Top-level ``sources.yaml`` schema."""

    sources: list[SourceEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_unique_names(self) -> SourcesConfig:
        seen: set[str] = set()
        for entry in self.sources:
            if entry.name in seen:
                msg = (
                    f"Duplicate source name {entry.name!r} in sources.yaml. "
                    f"Each entry must have a unique name."
                )
                raise ValueError(msg)
            seen.add(entry.name)
        return self

    def enabled(self) -> list[SourceEntry]:
        """Return only entries with ``enabled=True``, preserving declared order."""
        return [s for s in self.sources if s.enabled]

    def find(self, name: str) -> SourceEntry | None:
        """Return the entry with the given name or ``None``."""
        for entry in self.sources:
            if entry.name == name:
                return entry
        return None


def load_sources(path: Path | str) -> SourcesConfig:
    """Read and validate a ``sources.yaml`` file.

    Returns an empty :class:`SourcesConfig` when the file is empty or
    contains only the top-level key with no entries — callers don't have
    to special-case an empty registry. Raises :class:`FileNotFoundError`
    when the path does not exist (callers can catch and treat as "no
    registry declared").
    """
    yaml_path = Path(path)
    text = yaml_path.read_text(encoding="utf-8")
    data: Any = yaml.safe_load(text)
    if data is None:
        return SourcesConfig(sources=[])
    if not isinstance(data, dict):
        msg = (
            f"{yaml_path}: top-level YAML must be a mapping with a 'sources' "
            f"key; got {type(data).__name__}."
        )
        raise TypeError(msg)
    return SourcesConfig.model_validate(data)
