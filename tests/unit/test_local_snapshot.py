"""T10: the local snapshot adapter reads files under its root, never touches the
network (§V1), and refuses any path that escapes the root (§V2 path safety).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from arknights_mcp.sources.base import SourceAdapter, SourceAdapterError
from arknights_mcp.sources.local_snapshot import LocalSnapshotAdapter


def _make_snapshot(tmp_path: Path) -> Path:
    root = tmp_path / "snapshot" / "en"
    (root / "gamedata" / "excel").mkdir(parents=True)
    (root / "gamedata" / "excel" / "stage_table.json").write_text(
        json.dumps({"stages": {"main_04-04": {"code": "4-4"}}}), encoding="utf-8"
    )
    # A file OUTSIDE the snapshot root, used for traversal tests.
    (tmp_path / "secret.txt").write_text("top secret", encoding="utf-8")
    return root


def test_reads_json_and_bytes(tmp_path: Path) -> None:
    adapter = LocalSnapshotAdapter(_make_snapshot(tmp_path), server="en")
    data = adapter.read_json("gamedata/excel/stage_table.json")
    assert data["stages"]["main_04-04"]["code"] == "4-4"
    assert adapter.read_bytes("gamedata/excel/stage_table.json")


def test_exists_and_iter_files(tmp_path: Path) -> None:
    adapter = LocalSnapshotAdapter(_make_snapshot(tmp_path), server="en")
    assert adapter.exists("gamedata/excel/stage_table.json")
    assert not adapter.exists("gamedata/excel/missing.json")
    assert list(adapter.iter_files()) == ["gamedata/excel/stage_table.json"]


def test_satisfies_protocol_and_is_offline(tmp_path: Path) -> None:
    adapter = LocalSnapshotAdapter(_make_snapshot(tmp_path), server="en")
    assert isinstance(adapter, SourceAdapter)
    assert adapter.touches_network is False  # §V1: local adapter never networks
    assert adapter.source_id == "local_snapshot"
    assert adapter.server == "en"


def test_rejects_parent_traversal(tmp_path: Path) -> None:
    adapter = LocalSnapshotAdapter(_make_snapshot(tmp_path), server="en")
    with pytest.raises(SourceAdapterError, match="escapes snapshot root"):
        adapter.read_bytes("../../secret.txt")
    # exists() swallows the error and returns False rather than leaking.
    assert adapter.exists("../../secret.txt") is False


def test_rejects_absolute_path(tmp_path: Path) -> None:
    adapter = LocalSnapshotAdapter(_make_snapshot(tmp_path), server="en")
    with pytest.raises(SourceAdapterError, match="absolute"):
        adapter.read_bytes(str(tmp_path / "secret.txt"))


def test_rejects_symlink_escape(tmp_path: Path) -> None:
    root = _make_snapshot(tmp_path)
    link = root / "gamedata" / "escape.json"
    link.symlink_to(tmp_path / "secret.txt")
    adapter = LocalSnapshotAdapter(root, server="en")
    with pytest.raises(SourceAdapterError, match="escapes snapshot root"):
        adapter.read_bytes("gamedata/escape.json")


def test_missing_file_raises(tmp_path: Path) -> None:
    adapter = LocalSnapshotAdapter(_make_snapshot(tmp_path), server="en")
    with pytest.raises(SourceAdapterError, match="file not found"):
        adapter.read_bytes("gamedata/excel/missing.json")


def test_invalid_json_raises(tmp_path: Path) -> None:
    root = _make_snapshot(tmp_path)
    (root / "bad.json").write_text("{not valid", encoding="utf-8")
    adapter = LocalSnapshotAdapter(root, server="en")
    with pytest.raises(SourceAdapterError, match="invalid JSON"):
        adapter.read_json("bad.json")


def test_nonexistent_root_raises(tmp_path: Path) -> None:
    with pytest.raises(SourceAdapterError, match="not a directory"):
        LocalSnapshotAdapter(tmp_path / "nope", server="en")
