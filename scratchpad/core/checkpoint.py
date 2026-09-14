"""Checkpoint files: a full document text at a given sequence number.

::

    magic "SCRPCKP1"   8 bytes
    version            u16 little endian
    seq                u64 little endian
    wall_ns            u64 little endian
    crc32              u32 little endian, over the *uncompressed* UTF-8 text
    zlib(text utf-8)   rest of the file                    (header is 30 bytes)

A checkpoint is a cache in front of the event log: it says "after event ``seq``
the document read exactly this".  Deleting every checkpoint must lose nothing,
so every read is defensive -- a corrupt or truncated checkpoint is reported and
skipped, and the caller falls back to an older one (or to replaying from the
start of the log).

Writes go to a temp file, are fsynced, renamed and the directory fsynced, so a
checkpoint file is either absent or complete.
"""

from __future__ import annotations

import logging
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

from scratchpad import paths

log = logging.getLogger(__name__)

CKPT_MAGIC = b"SCRPCKP1"
CKPT_VERSION = 1
_HEADER = struct.Struct("<8sHQQI")
HEADER_SIZE = _HEADER.size  # 30
SUFFIX = ".ckpt"
#: zlib level: 1 is roughly three times faster than 6 and within a few percent
#: on text of this size, and checkpointing must not stall the UI.
COMPRESS_LEVEL = 1


class CheckpointError(Exception):
    """A checkpoint file is unreadable, truncated or corrupt."""


@dataclass(frozen=True, slots=True)
class Checkpoint:
    """A decoded checkpoint."""

    seq: int
    wall_ns: int
    text: str
    path: Path | None = None


def checkpoint_name(seq: int) -> str:
    """File name for a checkpoint at ``seq``."""
    if seq < 0:
        raise ValueError(f"negative checkpoint seq {seq}")
    return f"{seq:016d}{SUFFIX}"


def encode_checkpoint(seq: int, wall_ns: int, text: str) -> bytes:
    """Serialise a checkpoint."""
    raw = text.encode("utf-8")
    header = _HEADER.pack(CKPT_MAGIC, CKPT_VERSION, seq, wall_ns, zlib.crc32(raw) & 0xFFFFFFFF)
    return header + zlib.compress(raw, COMPRESS_LEVEL)


def decode_checkpoint(blob: bytes, *, path: Path | None = None) -> Checkpoint:
    """Parse a checkpoint, raising :class:`CheckpointError` on any damage."""
    if len(blob) < HEADER_SIZE:
        raise CheckpointError(f"{path or 'checkpoint'}: shorter than the header")
    magic, version, seq, wall_ns, crc = _HEADER.unpack_from(blob, 0)
    if magic != CKPT_MAGIC:
        raise CheckpointError(f"{path or 'checkpoint'}: bad magic {magic!r}")
    if version != CKPT_VERSION:
        raise CheckpointError(f"{path or 'checkpoint'}: unsupported version {version}")
    try:
        raw = zlib.decompress(blob[HEADER_SIZE:])
    except zlib.error as exc:
        raise CheckpointError(f"{path or 'checkpoint'}: corrupt compressed body ({exc})") from exc
    if (zlib.crc32(raw) & 0xFFFFFFFF) != crc:
        raise CheckpointError(f"{path or 'checkpoint'}: text checksum mismatch")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CheckpointError(f"{path or 'checkpoint'}: text is not valid UTF-8 ({exc})") from exc
    return Checkpoint(seq, wall_ns, text, path)


def write_checkpoint(directory: Path, seq: int, wall_ns: int, text: str) -> Path:
    """Write ``text`` as the checkpoint for ``seq``.  Returns the file path."""
    paths.ensure_dir(directory)
    target = directory / checkpoint_name(seq)
    paths.atomic_write_bytes(target, encode_checkpoint(seq, wall_ns, text))
    return target


def read_checkpoint(path: Path) -> Checkpoint:
    """Read one checkpoint file."""
    try:
        blob = path.read_bytes()
    except OSError as exc:
        raise CheckpointError(f"{path}: {exc}") from exc
    return decode_checkpoint(blob, path=path)


def list_checkpoints(directory: Path) -> list[tuple[int, Path]]:
    """All checkpoints in ``directory`` as ``(seq, path)``, ascending by seq."""
    try:
        entries = list(directory.iterdir())
    except FileNotFoundError:
        return []
    found: list[tuple[int, Path]] = []
    for entry in entries:
        if entry.suffix != SUFFIX or not entry.is_file():
            continue
        try:
            seq = int(entry.stem)
        except ValueError:
            log.warning("checkpoints: ignoring unexpected file %s", entry)
            continue
        found.append((seq, entry))
    found.sort()
    return found


def latest_at_or_before(
    directory: Path,
    seq: int,
    *,
    listing: list[tuple[int, Path]] | None = None,
) -> Checkpoint | None:
    """Newest readable checkpoint with ``ckpt.seq <= seq``, or None.

    Corrupt candidates are logged and skipped so that one damaged file only
    costs replay time, never data -- and so is a candidate that has meanwhile
    been pruned away, which is why ``listing`` (a previously obtained, possibly
    stale :func:`list_checkpoints` result, used to avoid one directory listing
    per reconstruction) is safe to pass.
    """
    if seq < 0:
        return None
    for candidate_seq, path in reversed(list_checkpoints(directory) if listing is None else listing):
        if candidate_seq > seq:
            continue
        try:
            return read_checkpoint(path)
        except CheckpointError as exc:
            log.warning("checkpoints: skipping unusable checkpoint: %s", exc)
    return None


def prune_checkpoints(directory: Path, keep: int) -> list[Path]:
    """Delete all but the newest ``keep`` checkpoints.  Returns what was removed.

    Called by :meth:`ScratchpadStore.close` with ``config.keep_checkpoints``:
    every run of the application adds at least one checkpoint, so without
    pruning the directory grows for as long as the scratchpad is used, and every
    listing of it gets slower.  Losing an old checkpoint costs replay time only.
    """
    if keep < 0:
        raise ValueError("keep must not be negative")
    existing = list_checkpoints(directory)
    removed: list[Path] = []
    for _, path in existing[: max(0, len(existing) - keep)]:
        try:
            path.unlink()
            removed.append(path)
        except OSError as exc:
            log.warning("checkpoints: cannot remove %s: %s", path, exc)
    if removed:
        paths.fsync_dir(directory)
    return removed
