"""The store facade: one open scratchpad, its log, its history, its lock.

How the UI drives the store
---------------------------

The store owns no threads and starts no timers.  Every durable write happens on
the caller's thread, and the caller decides when.  From the GTK main loop::

    store = ScratchpadStore.open(data_dir, config, app_version=__version__)

    # one timer for the cheap periodic work (fsync batching + checkpoints)
    GLib.timeout_add(250, lambda: (store.tick(), True)[1])

    # ... and one for the liveness record that bounds how much time a crash
    # can leave unaccounted for on the timeline
    GLib.timeout_add_seconds(config.heartbeat_seconds, lambda: (store.heartbeat(), True)[1])

    # on shutdown
    store.close()

``tick()`` is ``maybe_flush()`` plus ``maybe_heartbeat()``; driving a single
250 ms timer with ``tick()`` is enough, the two-timer form above just makes the
intent obvious.  Nothing bad happens if the UI never calls them -- the data is
already in the page cache after every :meth:`apply` -- but then ``fsync`` only
happens at :meth:`close`, so a power failure could cost the whole session.

What each call costs on the UI thread:

* :meth:`apply` -- one ``os.write`` of ~10 bytes plus one string splice.  No
  fsync, no compression, no directory work.  When a checkpoint comes due it is
  only *flagged*; the work happens in the next :meth:`tick`.
* :meth:`maybe_flush` -- an ``fsync`` at most every ``config.flush_interval_ms``,
  and a checkpoint (zlib + two atomic writes) every
  ``config.checkpoint_every_events`` events.
* :meth:`heartbeat` -- two tiny records.

Durability contract
-------------------

Every :meth:`apply` hands its record to the kernel immediately, so an
application crash loses nothing that was typed.  A power failure can only lose
what was written since the last fsync, i.e. at most ``flush_interval_ms``.
A torn tail from such a failure is detected at the next open, truncated away
(with a warning), and the log continues from there.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import logging
import os
import secrets
import shutil
from dataclasses import dataclass
from pathlib import Path

from scratchpad import paths
from scratchpad.config import Config
from scratchpad.core import checkpoint as checkpoint_mod
from scratchpad.core import eventlog, recovery
from scratchpad.core.clock import Clock, SYSTEM_CLOCK, SessionClock
from scratchpad.core.eventlog import (
    Event,
    EventKind,
    EventLogWriter,
    RecordTooLarge,
    ScanResult,
)
from scratchpad.core.history import DEFAULT_INDEX_STRIDE, History
from scratchpad.core.ops import Op, apply_op

log = logging.getLogger(__name__)

CURRENT_META_VERSION = 1

__all__ = ["ScratchpadStore", "StoreLockedError", "IoFailure", "RecordTooLarge"]


@dataclass(frozen=True, slots=True)
class IoFailure:
    """An I/O error the store survived, for the UI to show in a banner."""

    wall_ns: int
    """When it happened (Unix nanoseconds)."""
    message: str
    """What the operating system said."""
    operation: str = ""
    """Which piece of periodic work failed (``tick``, ``close``)."""


class StoreLockedError(RuntimeError):
    """Another process already has this data directory open."""


class ScratchpadStore:
    """The single writable view of a scratchpad data directory."""

    def __init__(
        self,
        data_dir: Path,
        config: Config,
        *,
        app_version: str,
        lock_fd: int,
        writer: EventLogWriter,
        history: History,
        text: str,
        scan: ScanResult,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.config = config
        self.app_version = app_version
        self.history = history
        self._lock_fd = lock_fd
        self._writer = writer
        self._clock = clock
        self._session_clock = SessionClock(clock=clock)
        self._text = text
        self._seq = scan.count - 1
        self._prev_mono_ns = scan.end_state.prev_mono_ns
        self._last_wall_ns = scan.end_state.last_wall_ns
        self._session_id = 0
        self._closed = False
        self._events_since_checkpoint = 0
        self._checkpoint_due = False
        self._next_fsync_ns = 0
        self._last_heartbeat_ns = 0
        self.last_io_error: IoFailure | None = None
        """The last I/O error :meth:`tick` or :meth:`close` swallowed, if any."""

    # -- opening -------------------------------------------------------------

    @classmethod
    def open(
        cls,
        data_dir: Path,
        config: Config,
        *,
        app_version: str,
        clock: Clock = SYSTEM_CLOCK,
        index_stride: int = DEFAULT_INDEX_STRIDE,
    ) -> "ScratchpadStore":
        """Open (or create) a data directory and start a session.

        Creates the layout, takes the instance lock, finishes or discards an
        interrupted history rewrite, truncates a torn tail -- including a record
        that decodes but cannot be applied -- restores the current text and
        appends SESSION_START.  Opening never fails because of a damaged log:
        whatever is readable is kept and the rest is truncated away with a loud
        message, because an editor nobody can start is worse than an editor that
        lost the last few bytes of a crash.

        ``clock`` is injectable so that tests can fabricate timelines; it is not
        part of the contract.
        """
        data_dir = Path(data_dir)
        paths.ensure_data_layout(data_dir)
        lock_fd = _acquire_lock(paths.lock_file(data_dir))
        try:
            recovery.recover_pending_rewrite(data_dir)

            log_path = paths.events_log(data_dir)
            eventlog.ensure_log(log_path)
            history = History(
                log_path,
                paths.checkpoints_dir(data_dir),
                index_stride=index_stride,
                heartbeat_seconds=config.heartbeat_seconds,
            )
            scan = history.rebuild()
            writer = EventLogWriter(log_path)
            try:
                if not scan.tail_ok:
                    log.warning(
                        "event log %s: %s; truncating %d torn byte(s) and continuing",
                        log_path, scan.error or "damaged tail",
                        writer.size - scan.good_length,
                    )
                    writer.truncate(scan.good_length)
                    history.notify_truncated(scan.good_length)
                text = _load_current_text(data_dir, history, scan)
                text, scan = _truncate_unreplayable_tail(log_path, writer, history, text, scan)
                store = cls(
                    data_dir, config, app_version=app_version, lock_fd=lock_fd,
                    writer=writer, history=history, text=text, scan=scan, clock=clock,
                )
                store._start_session()
            except BaseException:
                writer.close()
                raise
            return store
        except BaseException:
            _release_lock(lock_fd)
            raise

    # -- state ---------------------------------------------------------------

    @property
    def text(self) -> str:
        """The current document."""
        return self._text

    @property
    def seq(self) -> int:
        """Sequence number of the last appended event (-1 when empty)."""
        return self._seq

    @property
    def event_count(self) -> int:
        """Number of events in the log."""
        return self._seq + 1

    @property
    def session_id(self) -> int:
        """Identifier of the running session."""
        return self._session_id

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def log_size(self) -> int:
        """Current size of the event log in bytes."""
        return self._writer.size

    # -- writing -------------------------------------------------------------

    def apply(self, op: Op) -> Event:
        """Validate ``op`` against the current text, log it and apply it.

        Raises :class:`~scratchpad.core.ops.OpError` when the operation does not
        fit the document and :class:`~scratchpad.core.eventlog.RecordTooLarge`
        (a subclass of it) when it would not fit a log record -- both before
        anything has been written, so the caller can show a message and carry
        on with an unchanged document.
        """
        self._check_open()
        new_text = apply_op(self._text, op)   # raises OpError before anything is logged
        kind, payload = eventlog.encode_op(op)
        eventlog.check_payload_size(kind, payload)   # ... and so does RecordTooLarge
        self._anchor_if_drifted()
        event = self._append(kind, payload, op=op)
        self._text = new_text
        return event

    def heartbeat(self) -> None:
        """Append HEARTBEAT + WALL_ANCHOR.  The UI calls this on a timer.

        This is what bounds the timeline error of a crashed session: a session
        without SESSION_STOP ends at its last record, which is at most
        ``config.heartbeat_seconds`` old.

        The WALL_ANCHOR goes first.  Its payload is the true wall clock, while
        everything before it is timed by deriving from the previous anchor -- so
        anchoring first is what gives the HEARTBEAT (and therefore the end of a
        crashed session) the real time instead of a wall time that a suspend or
        an NTP step has left behind.
        """
        self._check_open()
        self._append_wall_anchor()
        self._append(EventKind.HEARTBEAT, b"")
        self._last_heartbeat_ns = self._clock.mono_ns()

    def flush(self, fsync: bool = True) -> None:
        """Push pending bytes to the OS and, by default, make them durable."""
        if self._closed:
            return
        self._writer.flush(fsync=fsync)
        if fsync:
            self._next_fsync_ns = self._clock.mono_ns() + self.config.flush_interval_ns

    def maybe_flush(self) -> bool:
        """Do the periodic durability work if it is due.  Call every ~250 ms.

        Returns True when something was actually done.
        """
        if self._closed:
            return False
        if self._checkpoint_due:
            self.checkpoint()
            return True
        if self._clock.mono_ns() >= self._next_fsync_ns:
            self.flush(fsync=True)
            return True
        return False

    def maybe_heartbeat(self) -> bool:
        """Append a heartbeat if ``config.heartbeat_seconds`` have passed."""
        if self._closed:
            return False
        if self._clock.mono_ns() - self._last_heartbeat_ns < self.config.heartbeat_ns:
            return False
        self.heartbeat()
        return True

    def tick(self) -> None:
        """All periodic work in one call; drive it from a 250 ms GLib timeout.

        Never raises.  A GLib timeout callback that raises loses its timer, and
        losing this one means no more fsyncs, no more checkpoints and no more
        heartbeats for the rest of the session -- a far worse outcome than the
        full disk that caused it.  The error is logged with its traceback and
        left in :attr:`last_io_error` for the UI to show; the next tick tries
        again.
        """
        try:
            self.maybe_flush()
            self.maybe_heartbeat()
        except OSError as exc:
            self._note_io_error(exc, "tick")

    def checkpoint(self) -> None:
        """Write a checkpoint plus ``current.txt`` now."""
        self._check_open()
        self.flush(fsync=True)
        if self._seq >= 0:
            checkpoint_mod.write_checkpoint(
                paths.checkpoints_dir(self.data_dir), self._seq, self._last_wall_ns, self._text
            )
            self.history.invalidate_checkpoints()
        self._write_current()
        self._events_since_checkpoint = 0
        self._checkpoint_due = False

    def close(self) -> None:
        """SESSION_STOP, checkpoint, prune, fsync, release the lock.  Idempotent.

        Shutting down must always end with the lock released, so an I/O error on
        the way out is logged and left in :attr:`last_io_error` rather than
        raised.
        """
        if self._closed:
            return
        try:
            self._append(EventKind.SESSION_STOP, b"")
            self.checkpoint()
            self._prune_checkpoints()
        except OSError as exc:
            self._note_io_error(exc, "close")
        finally:
            self._closed = True
            try:
                self._writer.close()
            except OSError as exc:
                self._note_io_error(exc, "close")
            finally:
                _release_lock(self._lock_fd)

    def _prune_checkpoints(self) -> None:
        """Keep the newest ``config.keep_checkpoints`` checkpoints, drop the rest."""
        removed = checkpoint_mod.prune_checkpoints(
            paths.checkpoints_dir(self.data_dir), self.config.keep_checkpoints
        )
        if removed:
            log.debug("checkpoints: pruned %d old checkpoint(s)", len(removed))
            self.history.invalidate_checkpoints()

    def _note_io_error(self, exc: OSError, operation: str) -> None:
        """Log an I/O error the store survived and remember it for the UI."""
        log.exception("store: %s failed: %s", operation, exc)
        self.last_io_error = IoFailure(self._clock.wall_ns(), str(exc), operation)

    def __enter__(self) -> "ScratchpadStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- history rewrite -----------------------------------------------------

    def replace_history(self, new_events_path: Path, new_checkpoints_dir: Path | None) -> None:
        """Swap in a rewritten history (redaction), crash-safely.

        The caller has already written a complete replacement log.  This closes
        the writer, stages the replacement under the names the recovery protocol
        expects, journals the swap, performs it, and reopens on the new files.
        The current text is re-derived from the new log and a fresh session is
        started, so appends continue normally.
        """
        self._check_open()
        self.flush(fsync=True)
        self._writer.close()

        history_dir = paths.history_dir(self.data_dir)
        staged_log = recovery.pending_log_path(self.data_dir)
        _stage(Path(new_events_path), staged_log)
        staged_ckpts = recovery.pending_checkpoints_path(self.data_dir)
        if new_checkpoints_dir is None:
            if staged_ckpts.exists():
                shutil.rmtree(staged_ckpts)
            paths.ensure_dir(staged_ckpts)
        else:
            _stage(Path(new_checkpoints_dir), staged_ckpts)
        paths.fsync_dir(history_dir)

        recovery.write_journal(self.data_dir, "swap")
        recovery.finish_rewrite(self.data_dir)

        log_path = paths.events_log(self.data_dir)
        eventlog.ensure_log(log_path)
        self.history.reader.reset()
        scan = self.history.rebuild()
        self._writer = EventLogWriter(log_path)
        if not scan.tail_ok:
            log.warning("event log %s after rewrite: %s; truncating", log_path, scan.error)
            self._writer.truncate(scan.good_length)
            self.history.notify_truncated(scan.good_length)
        self._seq = scan.count - 1
        self._prev_mono_ns = scan.end_state.prev_mono_ns
        self._last_wall_ns = scan.end_state.last_wall_ns
        self._text = self.history.reconstruct_seq(self._seq)
        self._events_since_checkpoint = 0
        self._checkpoint_due = False
        self._start_session()
        self.checkpoint()

    # -- internals -----------------------------------------------------------

    def _check_open(self) -> None:
        if self._closed:
            raise ValueError("store is closed")

    def _start_session(self) -> None:
        anchor = self._session_clock.start()
        self._session_id = secrets.randbits(63)
        payload = eventlog.encode_session_start(
            self._session_id, anchor.wall_ns, anchor.mono_ns, self.app_version
        )
        offset = self._writer.append(EventKind.SESSION_START, 0, payload)
        self._seq += 1
        wall = max(anchor.wall_ns, self._last_wall_ns)
        event = Event(
            self._seq, EventKind.SESSION_START, anchor.mono_ns, wall, self._session_id, None, offset
        )
        self._prev_mono_ns = anchor.mono_ns
        self._last_wall_ns = wall
        self.history.note_event(event, anchor_wall_ns=anchor.wall_ns)
        self._last_heartbeat_ns = anchor.mono_ns
        self._next_fsync_ns = anchor.mono_ns + self.config.flush_interval_ns
        self._events_since_checkpoint += 1

    def _append(self, kind: EventKind, payload: bytes, *, op: Op | None = None) -> Event:
        mono = self._clock.mono_ns()
        if mono < self._prev_mono_ns:
            mono = self._prev_mono_ns
        delta = mono - self._prev_mono_ns
        wall = max(self._session_clock.derive(mono), self._last_wall_ns)
        offset = self._writer.append(kind, delta, payload)
        self._seq += 1
        event = Event(self._seq, kind, mono, wall, self._session_id, op, offset)
        self._prev_mono_ns = mono
        self._last_wall_ns = wall
        self.history.note_event(event)
        self._note_checkpoint_progress()
        return event

    def _append_wall_anchor(self, wall_ns: int | None = None, mono_ns: int | None = None) -> Event:
        mono = self._clock.mono_ns() if mono_ns is None else mono_ns
        if mono < self._prev_mono_ns:
            mono = self._prev_mono_ns
        raw_wall = self._clock.wall_ns() if wall_ns is None else wall_ns
        self._session_clock.reanchor(mono, raw_wall)
        delta = mono - self._prev_mono_ns
        offset = self._writer.append(
            EventKind.WALL_ANCHOR, delta, eventlog.encode_wall_anchor(raw_wall)
        )
        self._seq += 1
        wall = max(raw_wall, self._last_wall_ns)
        event = Event(self._seq, EventKind.WALL_ANCHOR, mono, wall, self._session_id, None, offset)
        self._prev_mono_ns = mono
        self._last_wall_ns = wall
        self.history.note_event(event, anchor_wall_ns=raw_wall)
        self._note_checkpoint_progress()
        return event

    def _anchor_if_drifted(self) -> None:
        mono = self._clock.mono_ns()
        wall = self._clock.wall_ns()
        if self._session_clock.needs_anchor(mono, wall):
            log.debug("wall clock drifted by %d ns; re-anchoring", self._session_clock.drift_ns(mono, wall))
            self._append_wall_anchor(wall, mono)

    def _note_checkpoint_progress(self) -> None:
        self._events_since_checkpoint += 1
        if self._events_since_checkpoint >= self.config.checkpoint_every_events:
            # Deferred on purpose: compressing and fsyncing must not happen
            # inside a keystroke.  The next tick() picks it up.
            self._checkpoint_due = True

    def _write_current(self) -> None:
        text = self._text
        raw = text.encode("utf-8")
        meta = {
            "version": CURRENT_META_VERSION,
            "seq": self._seq,
            "log_length": self._writer.size,
            "wall_ns": self._last_wall_ns,
            "chars": len(text),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
        paths.atomic_write_bytes(paths.current_text(self.data_dir), raw)
        paths.atomic_write_bytes(
            paths.current_meta(self.data_dir),
            (json.dumps(meta, sort_keys=True) + "\n").encode("utf-8"),
        )


# --- helpers ----------------------------------------------------------------


def _acquire_lock(path: Path) -> int:
    """flock the data directory exclusively, or raise :class:`StoreLockedError`."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
            raise StoreLockedError(
                f"another scratchpad instance is using {path.parent}"
            ) from exc
        raise
    try:
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode("ascii"))
    except OSError:  # pragma: no cover - diagnostics only
        pass
    return fd


def _release_lock(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _stage(source: Path, target: Path) -> None:
    """Move ``source`` to ``target`` (same filesystem if possible)."""
    if source == target:
        return
    if not source.exists():
        raise FileNotFoundError(source)
    if target.exists():
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink()
    try:
        os.replace(source, target)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        if source.is_dir():
            shutil.copytree(source, target)
            shutil.rmtree(source)
        else:
            shutil.copy2(source, target)
            source.unlink()


def _truncate_unreplayable_tail(
    log_path: Path,
    writer: EventLogWriter,
    history: History,
    text: str,
    scan: ScanResult,
) -> tuple[str, ScanResult]:
    """Cut the log at a record that decodes but does not apply.

    A corrupted edit record can pass its one-byte checksum and still be
    nonsense -- an INSERT at a position the document does not have.  The format
    contract knows exactly one answer to a record that cannot be believed: it is
    a torn tail, so it and everything behind it goes.  Doing that here (loudly)
    is what keeps the application startable; the alternative is an exception out
    of every single :meth:`ScratchpadStore.open` from now on.
    """
    damage = history.replay_damage
    if damage is None:
        return text, scan
    log.error(
        "event log %s: event %d at byte offset %d does not apply to the document "
        "before it (%s); truncating %d byte(s) from there and continuing",
        log_path, damage.seq, damage.offset, damage.message, writer.size - damage.offset,
    )
    writer.truncate(damage.offset)
    history.notify_truncated(damage.offset)
    scan = history.rebuild()
    return history.reconstruct_seq(scan.count - 1), scan


def _load_current_text(data_dir: Path, history: History, scan: ScanResult) -> str:
    """Restore the document, preferring ``current.txt`` when it matches the log.

    The sidecar records which log position the text belongs to; if anything
    disagrees (a crash after the last checkpoint, a truncated tail, an edited
    file) the text is rebuilt from checkpoint + replay instead.
    """
    last_seq = scan.count - 1
    meta_path = paths.current_meta(data_dir)
    text_path = paths.current_text(data_dir)
    try:
        meta = json.loads(meta_path.read_text("utf-8"))
    except FileNotFoundError:
        meta = None
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("current.meta unreadable (%s); rebuilding from the log", exc)
        meta = None

    if isinstance(meta, dict) and meta.get("version") != CURRENT_META_VERSION:
        log.info(
            "current.meta was written by another format version (%r); rebuilding from the log",
            meta.get("version"),
        )
        meta = None

    if isinstance(meta, dict) and meta.get("seq") == last_seq and meta.get("log_length") == scan.good_length:
        try:
            raw = text_path.read_bytes()
        except OSError as exc:
            log.warning("current.txt unreadable (%s); rebuilding from the log", exc)
        else:
            if hashlib.sha256(raw).hexdigest() == meta.get("sha256"):
                try:
                    return raw.decode("utf-8")
                except UnicodeDecodeError as exc:
                    log.warning("current.txt is not valid UTF-8 (%s); rebuilding", exc)
            else:
                log.warning("current.txt does not match current.meta; rebuilding from the log")
    elif meta is not None:
        log.info("current.txt is stale (log moved on); rebuilding from the log")

    return history.reconstruct_seq(last_seq)
