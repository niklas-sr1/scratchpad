"""config.toml loading."""
from __future__ import annotations

import logging
from pathlib import Path

from scratchpad.config import Config


def test_defaults_match_the_architecture_document() -> None:
    config = Config()
    assert config.flush_interval_ms == 500
    assert config.checkpoint_every_events == 2000
    assert config.heartbeat_seconds == 30
    assert config.font == "monospace 11"
    assert config.large_paste_threshold_lines == 2000
    assert config.large_paste_threshold_chars == 200000
    assert config.continue_bullets is True
    assert config.width == 900
    assert config.height == 600
    assert config.path is None


def test_missing_file_gives_defaults(tmp_path: Path) -> None:
    config = Config.load(tmp_path / "nope.toml")
    assert config == Config()


def test_full_file_is_read(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[storage]
flush_interval_ms = 250
checkpoint_every_events = 500
heartbeat_seconds = 10

[editor]
font = "Fira Code 12"
large_paste_threshold_lines = 100
large_paste_threshold_chars = 5000
continue_bullets = false

[ui]
width = 1280
height = 720
"""
    )
    config = Config.load(path)
    assert config.flush_interval_ms == 250
    assert config.checkpoint_every_events == 500
    assert config.heartbeat_seconds == 10
    assert config.font == "Fira Code 12"
    assert config.large_paste_threshold_lines == 100
    assert config.large_paste_threshold_chars == 5000
    assert config.continue_bullets is False
    assert config.width == 1280
    assert config.height == 720
    assert config.path == path


def test_partial_file_keeps_other_defaults(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[storage]\nheartbeat_seconds = 5\n")
    config = Config.load(path)
    assert config.heartbeat_seconds == 5
    assert config.flush_interval_ms == 500
    assert config.width == 900


def test_unknown_keys_and_sections_warn_and_are_ignored(tmp_path: Path, caplog) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        "[storage]\nflush_interval_ms = 100\nwibble = 3\n\n[nonsense]\nx = 1\n"
    )
    with caplog.at_level(logging.WARNING, logger="scratchpad.config"):
        config = Config.load(path)
    assert config.flush_interval_ms == 100
    messages = " ".join(record.getMessage() for record in caplog.records)
    assert "wibble" in messages
    assert "nonsense" in messages


def test_wrong_types_warn_and_fall_back(tmp_path: Path, caplog) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[storage]\nflush_interval_ms = "soon"\n[editor]\ncontinue_bullets = 1\nfont = 7\n'
    )
    with caplog.at_level(logging.WARNING, logger="scratchpad.config"):
        config = Config.load(path)
    assert config.flush_interval_ms == 500
    assert config.continue_bullets is True
    assert config.font == "monospace 11"
    assert len(caplog.records) == 3


def test_broken_toml_warns_and_falls_back(tmp_path: Path, caplog) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[storage\nflush_interval_ms = ")
    with caplog.at_level(logging.WARNING, logger="scratchpad.config"):
        config = Config.load(path)
    assert config.flush_interval_ms == 500
    assert caplog.records


def test_round_trip_through_to_toml(tmp_path: Path) -> None:
    original = Config(flush_interval_ms=123, font='a "quoted" font', continue_bullets=False)
    path = tmp_path / "config.toml"
    path.write_text(original.to_toml())
    loaded = Config.load(path)
    assert loaded.flush_interval_ms == 123
    assert loaded.font == 'a "quoted" font'
    assert loaded.continue_bullets is False


def test_derived_nanosecond_helpers() -> None:
    config = Config(flush_interval_ms=500, heartbeat_seconds=30)
    assert config.flush_interval_ns == 500_000_000
    assert config.heartbeat_ns == 30_000_000_000


# --- ranges -----------------------------------------------------------------


def test_the_keep_checkpoints_setting_is_read(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[storage]\nkeep_checkpoints = 5\n")
    assert Config.load(path).keep_checkpoints == 5
    assert Config().keep_checkpoints == 20
    assert "keep_checkpoints = 20" in Config().to_toml()


def test_nonsense_numbers_are_clamped_with_a_warning(tmp_path: Path, caplog) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[storage]
flush_interval_ms = -1
checkpoint_every_events = 0
heartbeat_seconds = -30
keep_checkpoints = 0

[editor]
large_paste_threshold_lines = 0
large_paste_threshold_chars = -5

[ui]
width = 10
height = 0
"""
    )
    with caplog.at_level(logging.WARNING, logger="scratchpad.config"):
        config = Config.load(path)
    assert config.flush_interval_ms == 0            # 0 is legal: fsync every tick
    assert config.checkpoint_every_events == 1
    assert config.heartbeat_seconds == 1
    assert config.keep_checkpoints == 1
    assert config.large_paste_threshold_lines == 1
    assert config.large_paste_threshold_chars == 1
    assert config.width == 200
    assert config.height == 200
    clamped = " ".join(record.getMessage() for record in caplog.records)
    for name in ("checkpoint_every_events", "heartbeat_seconds", "width", "height"):
        assert name in clamped
    assert "flush_interval_ms" in clamped


def test_clamping_also_applies_to_values_set_in_code(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="scratchpad.config"):
        config = Config(checkpoint_every_events=0, heartbeat_seconds=0)
    assert (config.checkpoint_every_events, config.heartbeat_seconds) == (1, 1)
    assert config.heartbeat_ns == 1_000_000_000
    assert caplog.records
    # ... and to dataclasses.replace(), which is how the UI edits settings.
    from dataclasses import replace

    assert replace(config, width=1).width == 200


def test_values_in_range_are_left_alone_and_warn_about_nothing(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="scratchpad.config"):
        config = Config(flush_interval_ms=0, width=200, height=200, keep_checkpoints=1)
    assert (config.flush_interval_ms, config.width, config.height) == (0, 200, 200)
    assert not caplog.records
