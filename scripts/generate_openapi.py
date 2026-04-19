"""Generate the static OpenAPI spec from the FastAPI app.

Writes ``docs/api/v1.yaml`` so breaking changes to the API surface
become a reviewable diff in PRs.  Run locally with ``make openapi``;
CI regenerates and fails when the committed spec differs from the
live app output (see ``.github/workflows/openapi.yml``).

This script is deliberately standalone and not a typer/click command —
it's invoked by Make and CI, not by end users.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

# Ensure the project's src/ is importable when run directly.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from trellis_api.app import create_app  # noqa: E402


def main() -> int:
    """Render the OpenAPI spec and write it to ``docs/api/v1.yaml``.

    Exit 0 on success.  Exits non-zero only on hard failures
    (import error, app won't build) — the ``--check`` flag for
    "did this diverge?" is handled by the CI workflow, not by this
    script, so local regeneration is always a one-way write.
    """
    out_path = _REPO_ROOT / "docs" / "api" / "v1.yaml"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    app = create_app()
    schema = app.openapi()

    rendered = yaml.safe_dump(
        schema,
        sort_keys=True,
        default_flow_style=False,
        allow_unicode=True,
        width=120,
    )
    out_path.write_text(rendered, encoding="utf-8")
    print(f"wrote {out_path.relative_to(_REPO_ROOT)}  ({len(rendered)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
