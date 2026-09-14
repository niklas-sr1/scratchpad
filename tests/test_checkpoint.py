"""Checkpoint files."""
from __future__ import annotations

import zlib
from pathlib import Path

import pytest

from scratchpad.core.checkpoint import (
    CKPT_MAGIC,
    HEADER_SIZE,
    Checkpoint,
    CheckpointError,
    checkpoint_name,
    decode_checkpoint,
    encode_checkpoint,
    latest_at_or_before,
    list_checkpoints,
    prune_checkpoints,
    read_checkpoint,
    write_checkpoint,
)

TEXT = "- investigate CAN timeout\n  Ab3Fmx7QK9v2Rt8Nc4WpLd\n" * 40


def test_name_is_sortable() -> None:
    assert checkpoint_name(0) == "0000000000000000.ckpt"
    assert checkpoint_name(12345) == "0000000000012345.ckpt"
    assert sorted([checkpoint_name(9), checkpoint_name(10)]) == [
        checkpoint_name(9), checkpoint_name(10)
    ]
    with pytest.raises(ValueError):
        checkpoint_name(-1)


def test_round_trip(tmp_path: Path) -> None:
    path = write_checkpoint(tmp_path, 42, 1700, TEXT)
    assert path.name == checkpoint_name(42)
    loaded = read_checkpoint(path)
    assert (loaded.seq, loaded.wall_ns, loaded.text) == (42, 1700, TEXT)
    assert path.read_bytes()[:8] == CKPT_MAGIC


def test_empty_and_unicode_text(tmp_path: Path) -> None:
    for text in ("", "\U0001f600 你好\n", "x" * 100_000):
        blob = encode_checkpoint(1, 2, text)
        assert decode_checkpoint(blob).text == text


def test_compression_actually_compresses() -> None:
    blob = encode_checkpoint(1, 2, TEXT)
    assert len(blob) < len(TEXT.encode()) // 2


def test_truncated_file_is_reported(tmp_path: Path) -> None:
    path = write_checkpoint(tmp_path, 1, 2, TEXT)
    blob = path.read_bytes()
    for cut in (0, 10, HEADER_SIZE, HEADER_SIZE + 5, len(blob) - 1):
        path.write_bytes(blob[:cut])
        with pytest.raises(CheckpointError):
            read_checkpoint(path)


def test_corrupt_body_and_checksum_are_reported(tmp_path: Path) -> None:
    blob = bytearray(encode_checkpoint(1, 2, TEXT))
    blob[HEADER_SIZE + 5] ^= 0xFF
    with pytest.raises(CheckpointError):
        decode_checkpoint(bytes(blob))

    # A body that still inflates but no longer matches the stored text CRC.
    good = encode_checkpoint(1, 2, TEXT)
    forged = good[:HEADER_SIZE] + zlib.compress(b"different text")
    with pytest.raises(CheckpointError):
        decode_checkpoint(forged)

    bad_magic = bytearray(good)
    bad_magic[:8] = b"XXXXXXXX"
    with pytest.raises(CheckpointError):
        decode_checkpoint(bytes(bad_magic))


def test_listing_and_lookup(tmp_path: Path) -> None:
    for seq in (0, 5, 2000, 4000):
        write_checkpoint(tmp_path, seq, seq * 10, f"state {seq}")
    (tmp_path / "not-a-checkpoint.txt").write_text("ignore me")
    assert [seq for seq, _ in list_checkpoints(tmp_path)] == [0, 5, 2000, 4000]

    assert latest_at_or_before(tmp_path, -1) is None
    assert latest_at_or_before(tmp_path, 4).seq == 0
    assert latest_at_or_before(tmp_path, 2000).text == "state 2000"
    assert latest_at_or_before(tmp_path, 99999).seq == 4000
    assert list_checkpoints(tmp_path / "missing") == []


def test_a_corrupt_checkpoint_falls_back_to_an_older_one(tmp_path: Path) -> None:
    write_checkpoint(tmp_path, 10, 1, "old but good")
    broken = write_checkpoint(tmp_path, 20, 2, "newer")
    broken.write_bytes(broken.read_bytes()[:-3])
    found = latest_at_or_before(tmp_path, 100)
    assert found is not None and found.text == "old but good"


def test_writes_are_atomic(tmp_path: Path) -> None:
    write_checkpoint(tmp_path, 7, 1, TEXT)
    assert [p.name for p in tmp_path.iterdir()] == [checkpoint_name(7)]


def test_prune_keeps_the_newest(tmp_path: Path) -> None:
    for seq in range(6):
        write_checkpoint(tmp_path, seq, seq, f"state {seq}")
    removed = prune_checkpoints(tmp_path, keep=2)
    assert len(removed) == 4
    assert [seq for seq, _ in list_checkpoints(tmp_path)] == [4, 5]


def test_lookup_can_reuse_a_cached_listing(tmp_path: Path) -> None:
    """History keeps the listing; a lookup must be able to work from it."""
    directory = tmp_path / "ckpt"
    for seq, text in ((10, "ten"), (20, "twenty")):
        write_checkpoint(directory, seq, 1, text)
    listing = list_checkpoints(directory)

    assert latest_at_or_before(directory, 25, listing=listing).text == "twenty"
    assert latest_at_or_before(directory, 15, listing=listing).text == "ten"
    assert latest_at_or_before(directory, 5, listing=listing) is None

    # A listing that has gone stale (pruned away) costs replay time, not data.
    (directory / checkpoint_name(20)).unlink()
    assert latest_at_or_before(directory, 25, listing=listing).text == "ten"
    for _seq, path in listing:
        path.unlink(missing_ok=True)
    assert latest_at_or_before(directory, 25, listing=listing) is None
