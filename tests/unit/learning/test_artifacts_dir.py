"""``resolve_learning_artifacts_dir``: one spelling of the learning-artifacts directory.

The writers (``trellis worker curate``, ``trellis analyze learning-candidates``)
and the API's Review queue all call this resolver, so its two rules are the
whole contract between them: ``TRELLIS_LEARNING_ARTIFACTS_DIR`` when set,
else ``<data_dir>/learning`` beside ``stores/``. The end-to-end proof that
both sides reach it is ``tests/unit/api/test_learning_artifacts_dir.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from trellis.learning import (
    LEARNING_ARTIFACTS_DIR_ENV,
    LEARNING_CANDIDATES_FILENAME,
    resolve_learning_artifacts_dir,
    write_learning_review_artifacts,
)


#: Spelled out rather than imported in the tests that set it, so a rename of
#: the documented variable fails here instead of moving with the constant.
_ENV = "TRELLIS_LEARNING_ARTIFACTS_DIR"


@pytest.fixture(autouse=True)
def _no_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(LEARNING_ARTIFACTS_DIR_ENV, raising=False)
    monkeypatch.delenv(_ENV, raising=False)


@pytest.mark.parametrize("data_dir_name", ["data", "srv-trellis"])
def test_default_is_learning_beside_stores(tmp_path: Path, data_dir_name: str) -> None:
    data_dir = tmp_path / data_dir_name
    assert resolve_learning_artifacts_dir(data_dir / "stores") == data_dir / "learning"


def test_override_wins_over_the_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    elsewhere = tmp_path / "elsewhere"
    monkeypatch.setenv(_ENV, f"  {elsewhere}\n")
    assert resolve_learning_artifacts_dir(tmp_path / "data" / "stores") == elsewhere


def test_override_applies_without_a_stores_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_ENV, str(tmp_path / "elsewhere"))
    assert resolve_learning_artifacts_dir(None) == tmp_path / "elsewhere"


def test_blank_override_falls_back_to_the_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_ENV, "   ")
    stores_dir = tmp_path / "data" / "stores"
    assert resolve_learning_artifacts_dir(stores_dir) == tmp_path / "data" / "learning"


def test_nothing_to_resolve_is_none() -> None:
    assert resolve_learning_artifacts_dir(None) is None


def test_writer_writes_the_filename_the_reader_opens(tmp_path: Path) -> None:
    report = {"candidate_count": 0, "candidates": []}
    paths = write_learning_review_artifacts(report=report, output_dir=tmp_path)
    assert Path(paths["candidates_path"]) == tmp_path / LEARNING_CANDIDATES_FILENAME
    assert (tmp_path / LEARNING_CANDIDATES_FILENAME).is_file()
