"""Shared pytest fixtures."""
from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fresh data directory; also exported as SCRATCHPAD_DATA_DIR."""
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setenv("SCRATCHPAD_DATA_DIR", str(d))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    (tmp_path / "run").mkdir(exist_ok=True)
    return d
