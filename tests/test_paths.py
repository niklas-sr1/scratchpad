"""Directory resolution and the durable-write helpers."""
from __future__ import annotations

import os

import pytest

from scratchpad import paths


def test_data_dir_prefers_the_explicit_override(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SCRATCHPAD_DATA_DIR", str(tmp_path / "explicit"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert paths.data_dir() == tmp_path / "explicit"


def test_data_dir_falls_back_to_xdg_then_home(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("SCRATCHPAD_DATA_DIR", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert paths.data_dir() == tmp_path / "xdg" / "scratchpad"
    monkeypatch.delenv("XDG_DATA_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert paths.data_dir() == tmp_path / "home" / ".local" / "share" / "scratchpad"


def test_config_and_runtime_locations(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    assert paths.config_file() == tmp_path / "cfg" / "scratchpad" / "config.toml"
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    assert paths.runtime_dir() == tmp_path / "run" / "scratchpad"
    assert paths.socket_path() == tmp_path / "run" / "scratchpad" / "ipc.sock"
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    assert paths.runtime_dir().name == f"scratchpad-{os.getuid()}"


def test_layout(tmp_path) -> None:
    data = tmp_path / "data"
    paths.ensure_data_layout(data)
    assert paths.events_log(data) == data / "history" / "events.log"
    assert paths.checkpoints_dir(data).is_dir()
    assert paths.blobs_dir(data).is_dir()
    assert paths.current_text(data) == data / "current.txt"
    assert paths.lock_file(data) == data / "lock"
    assert oct(data.stat().st_mode)[-3:] == "700"
    paths.ensure_data_layout(data)      # idempotent


def test_atomic_write_leaves_no_temp_files(tmp_path) -> None:
    target = tmp_path / "current.txt"
    paths.atomic_write_text(target, "hello")
    assert target.read_text() == "hello"
    paths.atomic_write_text(target, "replaced")
    assert target.read_text() == "replaced"
    assert [p.name for p in tmp_path.iterdir()] == ["current.txt"]
    assert oct(target.stat().st_mode)[-3:] == "600"


def test_atomic_write_creates_parents(tmp_path) -> None:
    target = tmp_path / "a" / "b" / "file"
    paths.atomic_write_bytes(target, b"x")
    assert target.read_bytes() == b"x"


def test_a_failed_write_leaves_no_temp_file_behind(tmp_path, monkeypatch) -> None:
    """A full disk must not litter the data directory with .tmp files."""
    target = tmp_path / "current.txt"
    paths.atomic_write_text(target, "first")

    def boom(_fd: int) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "fsync", boom)
    with pytest.raises(OSError):
        paths.atomic_write_text(target, "second")
    monkeypatch.undo()

    assert [p.name for p in tmp_path.iterdir()] == ["current.txt"]
    assert target.read_text() == "first", "the old content survives"


def test_a_failed_rename_leaves_no_temp_file_behind(tmp_path, monkeypatch) -> None:
    target = tmp_path / "sub" / "file"

    def boom(_src, _dst) -> None:
        raise OSError(18, "Invalid cross-device link")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        paths.atomic_write_bytes(target, b"x")
    monkeypatch.undo()
    assert list((tmp_path / "sub").iterdir()) == []
