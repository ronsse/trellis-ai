"""Where the learning-candidate review artifacts live.

``trellis worker curate`` and ``trellis analyze learning-candidates`` write
the scored candidates; the API's Review queue (``GET
/api/v1/learning/candidates``, ``POST /api/v1/learning/promotions``) reads
them. Each side resolves the directory here, so a writer run with no
``--output-dir`` and an API sharing its data directory meet in one place.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = [
    "LEARNING_ARTIFACTS_DIR_ENV",
    "LEARNING_CANDIDATES_FILENAME",
    "resolve_learning_artifacts_dir",
]

#: Overrides the default directory, on the writers and the API alike. A
#: deployment that sets it must set it on both sides.
LEARNING_ARTIFACTS_DIR_ENV = "TRELLIS_LEARNING_ARTIFACTS_DIR"

#: The default directory's name, a sibling of ``stores/`` under the data dir.
_DEFAULT_SUBDIR = "learning"

#: The scored report the writers produce and the Review queue serves.
LEARNING_CANDIDATES_FILENAME = "intent_learning_candidates.json"


def resolve_learning_artifacts_dir(stores_dir: Path | str | None) -> Path | None:
    """Return the learning-artifacts directory, or ``None`` if nothing names one.

    ``TRELLIS_LEARNING_ARTIFACTS_DIR`` wins when set to a non-blank value;
    otherwise the directory is ``<data_dir>/learning``, beside ``stores_dir``.
    Pass ``StoreRegistry.stores_dir``: it honours ``data_dir:`` in
    ``config.yaml``, which re-deriving from the environment does not.
    ``None`` comes back only when there is no override and no
    ``stores_dir``. The directory is not created or checked here.
    """
    override = os.environ.get(LEARNING_ARTIFACTS_DIR_ENV, "").strip()
    if override:
        return Path(override)
    if stores_dir is None:
        return None
    return Path(stores_dir).parent / _DEFAULT_SUBDIR
