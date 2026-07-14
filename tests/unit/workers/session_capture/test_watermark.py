"""Watermark cursor: skip unchanged files, re-process changed ones."""

from __future__ import annotations

from pathlib import Path

from trellis_workers.session_capture.watermark import WatermarkStore


def _touch(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def test_unrecorded_file_is_not_unchanged(tmp_path: Path) -> None:
    f = tmp_path / "s.jsonl"
    _touch(f, "a")
    wm = WatermarkStore(tmp_path / "wm.json")
    assert not wm.is_unchanged(f)


def test_recorded_file_is_unchanged(tmp_path: Path) -> None:
    f = tmp_path / "s.jsonl"
    _touch(f, "a")
    wm = WatermarkStore(tmp_path / "wm.json")
    wm.record(f)
    assert wm.is_unchanged(f)


def test_changed_size_reprocessed(tmp_path: Path) -> None:
    f = tmp_path / "s.jsonl"
    _touch(f, "a")
    wm = WatermarkStore(tmp_path / "wm.json")
    wm.record(f)
    _touch(f, "a longer body now")
    assert not wm.is_unchanged(f)


def test_persist_and_reload(tmp_path: Path) -> None:
    f = tmp_path / "s.jsonl"
    _touch(f, "a")
    wm_path = tmp_path / "wm.json"
    wm = WatermarkStore(wm_path)
    wm.record(f)
    wm.save()

    reloaded = WatermarkStore(wm_path)
    assert reloaded.is_unchanged(f)


def test_corrupt_watermark_resets_to_full_scan(tmp_path: Path) -> None:
    f = tmp_path / "s.jsonl"
    _touch(f, "a")
    wm_path = tmp_path / "wm.json"
    wm_path.write_text("{ not valid json", encoding="utf-8")
    wm = WatermarkStore(wm_path)
    # A corrupt cursor degrades to re-scanning everything, never crashes.
    assert not wm.is_unchanged(f)


def test_save_noop_when_nothing_recorded(tmp_path: Path) -> None:
    wm_path = tmp_path / "wm.json"
    WatermarkStore(wm_path).save()
    assert not wm_path.exists()
