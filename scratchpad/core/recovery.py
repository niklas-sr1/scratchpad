"""Crash recovery for the history rewrite protocol (ARCHITECTURE.md section 11).

A redaction rewrites the whole log next to the live one and then swaps it in:

1. write ``history/events.log.rewrite`` completely, fsync
2. write ``history/checkpoints.rewrite/`` (may be empty), fsync the directory
3. write the journal ``history/REWRITE_PENDING`` (one JSON line), fsync
4. rename ``events.log`` -> ``events.log.old``, ``events.log.rewrite`` ->
   ``events.log``, ``checkpoints`` -> ``checkpoints.old``, ``checkpoints.rewrite``
   -> ``checkpoints``; delete ``current.txt`` and ``index.cache``
5. remove ``events.log.old``, ``checkpoints.old``, then ``REWRITE_PENDING``

Steps 1 and 2 are Agent E's; this module owns the swap (steps 4 and 5) and the
recovery decision:

* journal present  -> finish steps 4 and 5, idempotently
* journal absent but a ``.rewrite`` left over -> the rewrite never committed;
  delete it and keep the live history untouched

Checkpoints and the journal
---------------------------

Once the journal exists the live ``checkpoints/`` directory describes a log that
is about to disappear.  Its files say "after event N the document read exactly
this" -- with the *unredacted* text, and with sequence numbers that the new log
reuses for entirely different events.  So the swap always removes the live
checkpoint directory, whether or not a replacement was staged; without a
replacement it installs an empty one.  Losing a checkpoint costs replay time,
whereas keeping a stale one hands redacted text back to the next reconstruction
that happens to hit its sequence number exactly.

Everything here is pure filesystem work with no store state, so it can run
before the log is opened -- which is exactly when it must run.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path

from scratchpad import paths

log = logging.getLogger(__name__)

JOURNAL_NAME = "REWRITE_PENDING"
NEW_LOG_NAME = "events.log.rewrite"
OLD_LOG_NAME = "events.log.old"
NEW_CHECKPOINTS_NAME = "checkpoints.rewrite"
OLD_CHECKPOINTS_NAME = "checkpoints.old"


def journal_path(data_dir: Path) -> Path:
    return paths.history_dir(data_dir) / JOURNAL_NAME


def pending_log_path(data_dir: Path) -> Path:
    return paths.history_dir(data_dir) / NEW_LOG_NAME


def pending_checkpoints_path(data_dir: Path) -> Path:
    return paths.history_dir(data_dir) / NEW_CHECKPOINTS_NAME


def old_log_path(data_dir: Path) -> Path:
    return paths.history_dir(data_dir) / OLD_LOG_NAME


def old_checkpoints_path(data_dir: Path) -> Path:
    return paths.history_dir(data_dir) / OLD_CHECKPOINTS_NAME


def write_journal(data_dir: Path, phase: str = "swap", **extra: object) -> Path:
    """Step 3: record that a swap is in progress, durably."""
    path = journal_path(data_dir)
    payload = json.dumps({"phase": phase, **extra}, sort_keys=True) + "\n"
    paths.atomic_write_text(path, payload)
    return path


def read_journal(data_dir: Path) -> dict[str, object] | None:
    """The journal content, or None when no rewrite is pending."""
    path = journal_path(data_dir)
    try:
        raw = path.read_text("utf-8")
    except FileNotFoundError:
        return None
    line = raw.strip()
    if not line:
        return {}
    try:
        value = json.loads(line.splitlines()[0])
    except json.JSONDecodeError:
        log.warning("recovery: unreadable rewrite journal %s; assuming a swap", path)
        return {}
    return value if isinstance(value, dict) else {}


def _swap_path(new: Path, live: Path, old: Path) -> None:
    """Idempotent half of step 4 for one path (file or directory)."""
    if new.exists():
        if live.exists():
            if old.exists():
                # The pre-rewrite copy is already parked in `old`; this `live`
                # is a leftover from an interrupted rename.
                _remove(live)
            else:
                os.replace(live, old)
        os.replace(new, live)


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    else:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _swap_checkpoints(data_dir: Path) -> None:
    """Step 4 for the checkpoint directory: the live one always goes.

    Unlike the log, the checkpoints are a cache, and the cache in place belongs
    to the pre-rewrite log.  It is removed even when no replacement was staged
    (an empty directory takes its place), because a checkpoint that survives a
    rewrite is a checkpoint that answers ``reconstruct_seq`` with text the
    rewrite was supposed to remove.
    """
    live = paths.checkpoints_dir(data_dir)
    old = old_checkpoints_path(data_dir)
    new = pending_checkpoints_path(data_dir)
    if live.exists():
        if old.exists():
            # The pre-rewrite copy is already parked in `old`; this `live` is a
            # leftover from an interrupted rename.
            _remove(live)
        else:
            os.replace(live, old)
    if new.exists():
        os.replace(new, live)
    else:
        paths.ensure_dir(live)


def finish_rewrite(data_dir: Path) -> None:
    """Steps 4 and 5, idempotently.  Safe to call any number of times.

    Calling it again after a completed swap costs the fresh checkpoints (they
    are written again at the next checkpoint or clean shutdown); it never costs
    history.
    """
    history = paths.history_dir(data_dir)
    if not history.is_dir():
        return
    _swap_path(pending_log_path(data_dir), paths.events_log(data_dir), old_log_path(data_dir))
    _swap_checkpoints(data_dir)
    # Derived state that described the pre-rewrite log.
    _remove(paths.current_text(data_dir))
    _remove(paths.current_meta(data_dir))
    _remove(paths.index_cache(data_dir))
    paths.fsync_dir(history)

    _remove(old_log_path(data_dir))
    _remove(old_checkpoints_path(data_dir))
    paths.fsync_dir(history)

    _remove(journal_path(data_dir))
    paths.fsync_dir(history)


def abort_rewrite(data_dir: Path) -> None:
    """Throw away an uncommitted rewrite; the live history is untouched."""
    _remove(pending_log_path(data_dir))
    _remove(pending_checkpoints_path(data_dir))
    history = paths.history_dir(data_dir)
    if history.is_dir():
        paths.fsync_dir(history)


def recover_pending_rewrite(data_dir: Path) -> None:
    """Bring an interrupted history rewrite to a decision.

    Called by :meth:`scratchpad.core.store.ScratchpadStore.open` before the log
    is read.  Does nothing when no rewrite was in flight.
    """
    data_dir = Path(data_dir)
    history = paths.history_dir(data_dir)
    if not history.is_dir():
        return
    journal = read_journal(data_dir)
    if journal is not None:
        log.warning("recovery: finishing an interrupted history rewrite (%s)", journal)
        finish_rewrite(data_dir)
        return
    stale = [p for p in (pending_log_path(data_dir), pending_checkpoints_path(data_dir)) if p.exists()]
    if stale:
        log.warning("recovery: discarding an uncommitted history rewrite (%s)",
                    ", ".join(p.name for p in stale))
        abort_rewrite(data_dir)
    # A leftover `.old` without a journal means step 5 was interrupted after the
    # journal was removed; the swap itself is already committed.
    leftovers = [p for p in (old_log_path(data_dir), old_checkpoints_path(data_dir)) if p.exists()]
    if leftovers:
        log.warning("recovery: removing pre-rewrite leftovers (%s)",
                    ", ".join(p.name for p in leftovers))
        for path in leftovers:
            _remove(path)
        paths.fsync_dir(history)
