"""The store facade, crash behaviour and the spec's acceptance criteria.

Acceptance criteria from `Design Specification.md` section 39 that are covered
here: 1 (a crash keeps the last keystroke), 2 (every mutation replayable),
3 (`hello` character by character), 4 (one paste is one mutation), 5 (undo
leaves earlier states intact), 6 (arbitrary times reconstruct), 7 (non-running
intervals are identifiable), 18 (deletion preserves history), 21 (clearing does
not destroy history).
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scratchpad import paths
from scratchpad.config import Config
from scratchpad.core import checkpoint as checkpoint_mod
from scratchpad.core import eventlog, recovery
from scratchpad.core.clock import ManualClock
from scratchpad.core.eventlog import EventKind, EventLogReader, EventLogWriter
from scratchpad.core.history import History
from scratchpad.core.ops import OpError, delete, insert, replace
from scratchpad.core.store import ScratchpadStore, StoreLockedError

SECOND = 1_000_000_000
WALL0 = 1_700_000_000 * SECOND
VERSION = "0.1.0-test"


def open_store(data_dir: Path, config: Config | None = None, clock=None) -> ScratchpadStore:
    return ScratchpadStore.open(
        data_dir, config or Config(), app_version=VERSION, **({"clock": clock} if clock else {})
    )


def manual_clock(wall: int = WALL0, mono: int = 1_000 * SECOND) -> ManualClock:
    return ManualClock(wall=wall, mono=mono)


def type_text(store: ScratchpadStore, text: str, clock: ManualClock | None = None) -> None:
    for index, char in enumerate(text):
        if clock is not None:
            clock.advance(120_000_000)
        store.apply(insert(len(store.text), char))


# --- opening, locking, layout ----------------------------------------------


def test_open_creates_the_layout_and_starts_a_session(data_dir: Path) -> None:
    store = open_store(data_dir)
    try:
        assert paths.events_log(data_dir).exists()
        assert paths.checkpoints_dir(data_dir).is_dir()
        assert store.text == ""
        assert store.event_count == 1
        assert store.seq == 0
        first = next(EventLogReader(paths.events_log(data_dir)).iter_events())
        assert first.kind is EventKind.SESSION_START
        assert first.session_id == store.session_id
    finally:
        store.close()


def test_only_one_instance_may_hold_a_data_directory(data_dir: Path) -> None:
    store = open_store(data_dir)
    try:
        with pytest.raises(StoreLockedError):
            open_store(data_dir)
    finally:
        store.close()
    reopened = open_store(data_dir)          # the lock is released on close
    reopened.close()


def test_a_failed_open_does_not_leak_the_lock(data_dir: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "scratchpad.core.store.EventLogWriter",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    with pytest.raises(RuntimeError):
        open_store(data_dir)
    monkeypatch.undo()
    store = open_store(data_dir)
    store.close()


def test_apply_validates_before_it_logs(data_dir: Path) -> None:
    store = open_store(data_dir)
    try:
        store.apply(insert(0, "abc"))
        before = store.event_count
        with pytest.raises(OpError):
            store.apply(delete(0, "zzz"))
        assert store.text == "abc"
        assert store.event_count == before
    finally:
        store.close()


# --- durability and crashes -------------------------------------------------


_CRASH_SCRIPT = """
import os, sys
from pathlib import Path
from scratchpad.config import Config
from scratchpad.core.ops import insert
from scratchpad.core.store import ScratchpadStore

store = ScratchpadStore.open(Path(sys.argv[1]), Config(), app_version="crash-test")
for index, char in enumerate(sys.argv[2]):
    store.apply(insert(index, char))
os._exit(9)
"""


def test_ac1_an_application_crash_loses_no_keystroke(data_dir: Path) -> None:
    """Acceptance criterion 1, with a real process death (no unwinding at all).

    The child never flushes, never closes and never releases the lock; the
    kernel does that for it.  Everything it typed must still be there.
    """
    text = "unsaved keystrokes"
    result = subprocess.run(
        [sys.executable, "-c", _CRASH_SCRIPT, str(data_dir), text],
        cwd=Path(__file__).resolve().parent.parent,
        capture_output=True,
    )
    assert result.returncode == 9, result.stderr.decode()

    store = open_store(data_dir)
    try:
        assert store.text == text
        sessions = store.history.sessions()
        assert len(sessions) == 2
        assert sessions[0].clean_stop is False, "the dead session must look unclean"
        assert sessions[0].end_wall_ns >= sessions[0].start_wall_ns
    finally:
        store.close()


def test_a_torn_tail_is_truncated_on_open(data_dir: Path, caplog) -> None:
    store = open_store(data_dir)
    type_text(store, "hello")
    store.flush()
    store._writer.close()                  # simulate a crash without SESSION_STOP
    os.close(store._lock_fd)
    log_path = paths.events_log(data_dir)
    good = log_path.stat().st_size
    with open(log_path, "ab") as handle:   # a half written record
        handle.write(b"\x0b\x05\x80\x9a\x0c")

    reopened = open_store(data_dir)
    try:
        assert reopened.text == "hello"
        assert log_path.stat().st_size > good      # SESSION_START was appended
        scan = EventLogReader(log_path).scan()
        assert scan.tail_ok is True
    finally:
        reopened.close()


def test_clean_reopen_uses_current_txt_without_replaying(data_dir: Path, monkeypatch) -> None:
    store = open_store(data_dir)
    type_text(store, "fast path please")
    store.close()

    meta = json.loads(paths.current_meta(data_dir).read_text())
    assert meta["seq"] == store.seq
    assert meta["log_length"] == paths.events_log(data_dir).stat().st_size
    assert paths.current_text(data_dir).read_text() == "fast path please"

    calls: list[int] = []
    original = History.reconstruct_seq

    def spy(self, seq):
        calls.append(seq)
        return original(self, seq)

    monkeypatch.setattr(History, "reconstruct_seq", spy)
    reopened = open_store(data_dir)
    try:
        assert reopened.text == "fast path please"
        assert calls == [], "current.txt should have been trusted"
    finally:
        reopened.close()


def test_a_stale_current_txt_is_ignored(data_dir: Path) -> None:
    store = open_store(data_dir)
    type_text(store, "one")
    store.checkpoint()
    type_text(store, " two")               # written after the last current.txt
    store._writer.flush(fsync=True)
    store._writer.close()
    os.close(store._lock_fd)

    reopened = open_store(data_dir)
    try:
        assert reopened.text == "one two"
    finally:
        reopened.close()


def test_a_corrupted_current_txt_is_ignored(data_dir: Path) -> None:
    store = open_store(data_dir)
    type_text(store, "trust but verify")
    store.close()
    paths.current_text(data_dir).write_text("tampered")

    reopened = open_store(data_dir)
    try:
        assert reopened.text == "trust but verify"
    finally:
        reopened.close()


def test_deleting_every_checkpoint_loses_nothing(data_dir: Path) -> None:
    store = open_store(data_dir)
    type_text(store, "the quick brown fox")
    store.close()
    for _, path in checkpoint_mod.list_checkpoints(paths.checkpoints_dir(data_dir)):
        path.unlink()
    paths.current_text(data_dir).unlink()
    paths.current_meta(data_dir).unlink()

    reopened = open_store(data_dir)
    try:
        assert reopened.text == "the quick brown fox"
    finally:
        reopened.close()


# --- timers: flush, heartbeat, checkpoint -----------------------------------


def test_checkpoints_are_deferred_out_of_apply(data_dir: Path) -> None:
    clock = manual_clock()
    config = Config(checkpoint_every_events=10)
    store = open_store(data_dir, config, clock)
    try:
        type_text(store, "abcdefghijkl", clock)
        checkpoints = checkpoint_mod.list_checkpoints(paths.checkpoints_dir(data_dir))
        assert checkpoints == [], "apply() must not compress or fsync"
        assert store._checkpoint_due is True
        store.tick()
        checkpoints = checkpoint_mod.list_checkpoints(paths.checkpoints_dir(data_dir))
        assert len(checkpoints) == 1
        assert checkpoint_mod.read_checkpoint(checkpoints[0][1]).text == store.text
        assert store._checkpoint_due is False
    finally:
        store.close()


def test_maybe_flush_respects_the_configured_interval(data_dir: Path) -> None:
    clock = manual_clock()
    store = open_store(data_dir, Config(flush_interval_ms=500), clock)
    try:
        store.apply(insert(0, "a"))
        assert store.maybe_flush() is False
        clock.advance(499_000_000)
        assert store.maybe_flush() is False
        clock.advance(2_000_000)
        assert store.maybe_flush() is True
        assert store.maybe_flush() is False
    finally:
        store.close()


def test_maybe_heartbeat_respects_the_configured_interval(data_dir: Path) -> None:
    clock = manual_clock()
    store = open_store(data_dir, Config(heartbeat_seconds=30), clock)
    try:
        assert store.maybe_heartbeat() is False
        clock.advance(29 * SECOND)
        assert store.maybe_heartbeat() is False
        clock.advance(2 * SECOND)
        assert store.maybe_heartbeat() is True
        kinds = [event.kind for event in EventLogReader(paths.events_log(data_dir)).iter_events()]
        # The anchor comes first so the heartbeat carries the real wall time.
        assert kinds[-2:] == [EventKind.WALL_ANCHOR, EventKind.HEARTBEAT]
    finally:
        store.close()


def test_a_drifting_wall_clock_re_anchors(data_dir: Path) -> None:
    clock = manual_clock()
    store = open_store(data_dir, Config(), clock)
    try:
        clock.advance(SECOND)
        store.apply(insert(0, "a"))
        clock.step_wall(3600 * SECOND)          # NTP jumped an hour forward
        store.apply(insert(1, "b"))
        kinds = [event.kind for event in EventLogReader(paths.events_log(data_dir)).iter_events()]
        assert EventKind.WALL_ANCHOR in kinds
        events = list(EventLogReader(paths.events_log(data_dir)).iter_events())
        assert events[-1].wall_ns >= WALL0 + 3600 * SECOND
        walls = [event.wall_ns for event in events]
        assert walls == sorted(walls)
    finally:
        store.close()


def test_derived_wall_times_never_go_backwards(data_dir: Path) -> None:
    clock = manual_clock()
    store = open_store(data_dir, Config(), clock)
    try:
        clock.advance(SECOND)
        store.apply(insert(0, "a"))
        clock.step_wall(-3600 * SECOND)         # the clock was set back
        store.apply(insert(1, "b"))
        store.heartbeat()
        walls = [e.wall_ns for e in EventLogReader(paths.events_log(data_dir)).iter_events()]
        assert walls == sorted(walls)
    finally:
        store.close()


def test_close_is_idempotent_and_records_a_clean_stop(data_dir: Path) -> None:
    store = open_store(data_dir)
    type_text(store, "bye")
    store.close()
    store.close()
    history = History(paths.events_log(data_dir), paths.checkpoints_dir(data_dir))
    history.rebuild()
    assert history.sessions()[-1].clean_stop is True
    with pytest.raises(ValueError):
        store.apply(insert(0, "x"))


# --- acceptance criteria ----------------------------------------------------


def test_ac2_every_mutation_is_replayable_in_order(data_dir: Path) -> None:
    store = open_store(data_dir)
    try:
        expected = []
        for op in (insert(0, "alpha"), insert(5, " beta"), delete(0, "alpha "),
                   replace(0, "beta", "gamma")):
            store.apply(op)
            expected.append(store.text)
        replayed = [
            store.history.reconstruct_seq(event.seq)
            for event in store.history.events(0, store.event_count)
            if event.op is not None
        ]
        assert replayed == expected
    finally:
        store.close()


def test_ac3_typing_hello_gives_five_intermediate_states(data_dir: Path) -> None:
    clock = manual_clock()
    store = open_store(data_dir, Config(), clock)
    try:
        seqs = []
        for index, char in enumerate("hello"):
            clock.advance(150_000_000)
            seqs.append(store.apply(insert(index, char)).seq)
        assert [store.history.reconstruct_seq(seq) for seq in seqs] == [
            "h", "he", "hel", "hell", "hello",
        ]
        # ... and by time, not only by sequence number.
        events = list(store.history.events(0, store.event_count))
        by_time = [store.history.reconstruct_at(event.wall_ns)[0] for event in events[1:]]
        assert by_time == ["h", "he", "hel", "hell", "hello"]
    finally:
        store.close()


def test_ac4_a_paste_is_one_mutation(data_dir: Path) -> None:
    store = open_store(data_dir)
    try:
        before = store.event_count
        store.apply(insert(0, "hello"))
        assert store.event_count == before + 1
        edits = [e for e in store.history.events(0, store.event_count) if e.op is not None]
        assert len(edits) == 1
        assert edits[0].op.text == "hello"
    finally:
        store.close()


def test_ac5_undo_is_a_new_mutation_and_earlier_states_survive(data_dir: Path) -> None:
    """Undo does not rewind history; it appends the inverse operation."""
    clock = manual_clock()
    store = open_store(data_dir, Config(), clock)
    try:
        clock.advance(SECOND)
        typed = store.apply(insert(0, "foobar"))
        clock.advance(3 * SECOND)
        undone = store.apply(delete(0, "foobar"))       # the user pressed Ctrl+Z
        assert store.text == ""
        assert undone.seq == typed.seq + 1

        between = (typed.wall_ns + undone.wall_ns) // 2
        assert store.history.reconstruct_at(between) == ("foobar", typed.seq)
        assert store.history.reconstruct_seq(typed.seq) == "foobar"
        assert store.history.reconstruct_seq(undone.seq) == ""

        clock.advance(SECOND)
        redone = store.apply(insert(0, "foobar"))       # and then Ctrl+Shift+Z
        assert redone.seq == undone.seq + 1
        assert store.history.reconstruct_at(between)[0] == "foobar"
    finally:
        store.close()


def test_ac6_arbitrary_times_reconstruct(data_dir: Path) -> None:
    clock = manual_clock()
    store = open_store(data_dir, Config(), clock)
    try:
        marks = []
        for word in ("one ", "two ", "three"):
            clock.advance(5 * SECOND)
            event = store.apply(insert(len(store.text), word))
            marks.append((event.wall_ns, store.text))
        for wall_ns, text in marks:
            assert store.history.reconstruct_at(wall_ns)[0] == text
            assert store.history.reconstruct_at(wall_ns + SECOND)[0] == text
            assert store.history.reconstruct_at(wall_ns + 4 * SECOND)[0] == text
        first, last = store.history.time_range()
        assert store.history.reconstruct_at(first - SECOND) == ("", -1)
        assert store.history.reconstruct_at(last + 10 * SECOND)[0] == "one two three"
    finally:
        store.close()


def test_ac7_non_running_intervals_are_identifiable(data_dir: Path) -> None:
    clock = manual_clock()
    store = open_store(data_dir, Config(), clock)
    clock.advance(SECOND)
    store.apply(insert(0, "before the gap"))
    store.close()

    clock.advance(4 * 3600 * SECOND)             # four hours powered off
    store = open_store(data_dir, Config(), clock)
    try:
        clock.advance(SECOND)
        store.apply(insert(len(store.text), " and after"))
        sessions = store.history.sessions()
        assert len(sessions) == 2
        gap = sessions[1].start_wall_ns - sessions[0].end_wall_ns
        assert gap > 3 * 3600 * SECOND
        assert sessions[0].clean_stop is True
        # A time inside the gap still reconstructs the state left behind.
        inside = sessions[0].end_wall_ns + 3600 * SECOND
        assert store.history.reconstruct_at(inside)[0] == "before the gap"
    finally:
        store.close()


def test_ac18_deletion_preserves_history(data_dir: Path) -> None:
    clock = manual_clock()
    store = open_store(data_dir, Config(), clock)
    try:
        clock.advance(SECOND)
        written = store.apply(insert(0, "sensitive-looking note"))
        clock.advance(SECOND)
        store.apply(delete(0, "sensitive-looking note"))
        assert store.text == ""
        assert store.history.reconstruct_seq(written.seq) == "sensitive-looking note"
        assert store.history.reconstruct_at(written.wall_ns)[0] == "sensitive-looking note"
    finally:
        store.close()


def test_ac21_clearing_the_scratchpad_keeps_history(data_dir: Path) -> None:
    clock = manual_clock()
    store = open_store(data_dir, Config(), clock)
    try:
        clock.advance(SECOND)
        store.apply(insert(0, "line one\nline two\n"))
        clock.advance(SECOND)
        full = store.history.last_seq
        store.apply(delete(0, store.text))          # "Clear scratchpad"
        assert store.text == ""
        assert store.history.reconstruct_seq(full) == "line one\nline two\n"
        # The empty state is itself part of history.
        assert store.history.reconstruct_seq(store.history.last_seq) == ""
    finally:
        store.close()
        reopened = open_store(data_dir)
        assert reopened.text == ""
        assert reopened.history.reconstruct_seq(full) == "line one\nline two\n"
        reopened.close()


# --- history rewrite hand-off ----------------------------------------------


def _write_replacement_log(path: Path, texts: list[str]) -> None:
    writer = EventLogWriter(path)
    writer.append(
        EventKind.SESSION_START, 0, eventlog.encode_session_start(5, WALL0, 1000, "rewrite")
    )
    position = 0
    for text in texts:
        kind, payload = eventlog.encode_op(insert(position, text))
        writer.append(kind, SECOND, payload)
        position += len(text)
    writer.close()


def test_replace_history_swaps_atomically_and_keeps_writing(data_dir: Path) -> None:
    store = open_store(data_dir)
    try:
        type_text(store, "secret")
        assert store.text == "secret"

        replacement = data_dir / "history" / "rewritten.log"
        _write_replacement_log(replacement, ["redacted ", "text"])
        store.replace_history(replacement, None)

        assert store.text == "redacted text"
        assert not replacement.exists()
        assert not recovery.journal_path(data_dir).exists()
        assert checkpoint_mod.list_checkpoints(paths.checkpoints_dir(data_dir))
        assert store.history.sessions()[-1].session_id == store.session_id

        store.apply(insert(len(store.text), "!"))
        assert store.text == "redacted text!"
    finally:
        store.close()
    reopened = open_store(data_dir)
    try:
        assert reopened.text == "redacted text!"
        assert "secret" not in paths.events_log(data_dir).read_bytes().decode("utf-8", "replace")
    finally:
        reopened.close()


def test_recovery_finishes_an_interrupted_swap(data_dir: Path) -> None:
    store = open_store(data_dir)
    type_text(store, "old")
    store.close()

    _write_replacement_log(recovery.pending_log_path(data_dir), ["new text"])
    recovery.write_journal(data_dir, "swap")

    reopened = open_store(data_dir)
    try:
        assert reopened.text == "new text"
        assert not recovery.journal_path(data_dir).exists()
        assert not recovery.pending_log_path(data_dir).exists()
        assert not recovery.old_log_path(data_dir).exists()
    finally:
        reopened.close()


def test_recovery_discards_an_uncommitted_rewrite(data_dir: Path) -> None:
    store = open_store(data_dir)
    type_text(store, "keep me")
    store.close()

    _write_replacement_log(recovery.pending_log_path(data_dir), ["should be discarded"])
    assert recovery.read_journal(data_dir) is None

    reopened = open_store(data_dir)
    try:
        assert reopened.text == "keep me"
        assert not recovery.pending_log_path(data_dir).exists()
    finally:
        reopened.close()


def test_recovery_is_idempotent(data_dir: Path) -> None:
    store = open_store(data_dir)
    type_text(store, "old")
    store.close()
    _write_replacement_log(recovery.pending_log_path(data_dir), ["new"])
    recovery.write_journal(data_dir, "swap")
    for _ in range(3):
        recovery.recover_pending_rewrite(data_dir)
    reopened = open_store(data_dir)
    try:
        assert reopened.text == "new"
    finally:
        reopened.close()


def test_recovery_handles_a_half_finished_rename(data_dir: Path) -> None:
    store = open_store(data_dir)
    type_text(store, "old")
    store.close()
    # Crash state: events.log already moved aside, the replacement not yet in place.
    _write_replacement_log(recovery.pending_log_path(data_dir), ["new"])
    recovery.write_journal(data_dir, "swap")
    os.replace(paths.events_log(data_dir), recovery.old_log_path(data_dir))

    reopened = open_store(data_dir)
    try:
        assert reopened.text == "new"
        assert not recovery.old_log_path(data_dir).exists()
    finally:
        reopened.close()


def test_recovery_does_nothing_when_nothing_happened(data_dir: Path) -> None:
    recovery.recover_pending_rewrite(data_dir)          # no history dir yet
    store = open_store(data_dir)
    type_text(store, "x")
    store.close()
    recovery.recover_pending_rewrite(data_dir)
    reopened = open_store(data_dir)
    assert reopened.text == "x"
    reopened.close()


# --- larger runs ------------------------------------------------------------


def test_a_long_session_with_checkpoints_reconstructs_everywhere(data_dir: Path) -> None:
    clock = manual_clock()
    config = Config(checkpoint_every_events=100, flush_interval_ms=500)
    store = open_store(data_dir, config, clock)
    states: list[tuple[int, str]] = []
    try:
        for index in range(600):
            clock.advance(100_000_000)
            if index % 37 == 36 and store.text:
                store.apply(delete(0, store.text[0]))
            else:
                store.apply(insert(len(store.text), chr(ord("a") + index % 26)))
            store.tick()
            states.append((store.seq, store.text))
        assert len(checkpoint_mod.list_checkpoints(paths.checkpoints_dir(data_dir))) >= 5
        for seq, text in states:
            assert store.history.reconstruct_seq(seq) == text
    finally:
        store.close()

    reopened = open_store(data_dir)
    try:
        assert reopened.text == states[-1][1]
        for seq, text in states[::37]:
            assert reopened.history.reconstruct_seq(seq) == text
    finally:
        reopened.close()


# --- surviving a failing disk ------------------------------------------------


class _FailingWrite:
    """``os.write`` that fails a given number of times on one descriptor."""

    def __init__(self, fd: int, failures: int = 1) -> None:
        self.fd = fd
        self.failures = failures
        self.real = os.write

    def __call__(self, fd: int, data):  # noqa: ANN001 - mirrors os.write
        if fd == self.fd and self.failures > 0:
            self.failures -= 1
            raise OSError(errno.ENOSPC, "No space left on device")
        return self.real(fd, data)


def _file_event_count(data_dir: Path) -> int:
    return EventLogReader(paths.events_log(data_dir)).scan().count


def test_a_failed_append_keeps_store_log_and_history_in_step(data_dir: Path, monkeypatch) -> None:
    """A full disk during one keystroke must not desynchronise anything.

    The record used to stay in the writer's buffer and go out with the *next*
    keystroke -- a log holding an operation the store had never counted, which
    replayed into a document that never existed.
    """
    store = open_store(data_dir)
    try:
        type_text(store, "before")
        counted = store.event_count

        monkeypatch.setattr(os, "write", _FailingWrite(store._writer.fileno()))
        with pytest.raises(OSError):
            store.apply(insert(len(store.text), "!"))
        monkeypatch.undo()

        assert store.text == "before", "a failed keystroke changes nothing"
        assert store.event_count == counted
        type_text(store, " and after")
        store.flush()

        assert _file_event_count(data_dir) == store.event_count
        assert store.history.count == store.event_count
        assert store.event_count == store.seq + 1
        assert store.history.reconstruct_seq(store.seq) == store.text
        assert store.text == "before and after"
    finally:
        store.close()

    # ... and a pure replay, with every checkpoint gone, agrees.
    for _seq, path in checkpoint_mod.list_checkpoints(paths.checkpoints_dir(data_dir)):
        path.unlink()
    paths.current_text(data_dir).unlink()
    paths.current_meta(data_dir).unlink()
    history = History(paths.events_log(data_dir), paths.checkpoints_dir(data_dir))
    history.rebuild()
    assert history.reconstruct_seq(history.last_seq) == "before and after"

    reopened = open_store(data_dir)
    try:
        assert reopened.text == "before and after"
    finally:
        reopened.close()


def test_a_half_written_record_leaves_a_consistent_store(data_dir: Path, monkeypatch) -> None:
    """Even when part of the record reached the disk, the log stays readable."""
    class _ShortThenFail:
        def __init__(self, fd: int) -> None:
            self.fd = fd
            self.real = os.write
            self.state = 0

        def __call__(self, fd: int, data):  # noqa: ANN001
            if fd != self.fd or self.state > 1:
                return self.real(fd, data)
            self.state += 1
            if self.state == 1:
                return self.real(fd, bytes(data)[:2])
            raise OSError(errno.ENOSPC, "No space left on device")

    store = open_store(data_dir)
    try:
        type_text(store, "half")
        counted = store.event_count
        monkeypatch.setattr(os, "write", _ShortThenFail(store._writer.fileno()))
        with pytest.raises(OSError):
            store.apply(insert(len(store.text), "X"))
        monkeypatch.undo()

        assert store.text == "half"
        assert store.event_count == counted
        type_text(store, "way")
        store.flush()
        assert _file_event_count(data_dir) == store.event_count
        assert store.history.reconstruct_seq(store.seq) == store.text == "halfway"
    finally:
        store.close()
    reopened = open_store(data_dir)
    try:
        assert reopened.text == "halfway"
    finally:
        reopened.close()


def test_tick_survives_an_io_error_and_reports_it(data_dir: Path, monkeypatch) -> None:
    """A raising GLib timeout callback would lose the timer for good."""
    clock = manual_clock()
    store = open_store(data_dir, Config(flush_interval_ms=0), clock)
    try:
        assert store.last_io_error is None
        type_text(store, "x", clock)

        def boom(_fd: int) -> None:
            raise OSError(errno.EIO, "I/O error")

        monkeypatch.setattr(os, "fsync", boom)
        clock.advance(SECOND)
        store.tick()                        # must not raise
        monkeypatch.undo()

        assert store.last_io_error is not None
        assert "I/O error" in store.last_io_error.message
        assert store.last_io_error.wall_ns == clock.wall
        assert store.last_io_error.operation == "tick"

        # The store keeps working; the next tick does the deferred work.
        clock.advance(SECOND)
        store.tick()
        type_text(store, "y", clock)
        assert store.text == "xy"
    finally:
        store.close()


def test_the_heartbeat_anchor_carries_the_true_wall_time(data_dir: Path) -> None:
    """WALL_ANCHOR before HEARTBEAT: after a suspend the heartbeat is not stale."""
    clock = manual_clock()
    store = open_store(data_dir, Config(), clock)
    try:
        clock.advance(SECOND)
        store.apply(insert(0, "a"))
        # Three hours of suspend: monotonic (CLOCK_BOOTTIME) and wall both move.
        clock.advance(3 * 3600 * SECOND)
        store.heartbeat()
        events = list(EventLogReader(paths.events_log(data_dir)).iter_events())
        assert [event.kind for event in events[-2:]] == [
            EventKind.WALL_ANCHOR, EventKind.HEARTBEAT,
        ]
        assert events[-1].wall_ns == clock.wall
        assert events[-2].wall_ns == clock.wall
    finally:
        store.close()


# --- record size limit ------------------------------------------------------


def test_a_paste_too_large_for_one_record_is_refused_before_it_is_logged(data_dir: Path) -> None:
    from scratchpad.core.eventlog import MAX_RECORD_BODY, RecordTooLarge

    store = open_store(data_dir)
    try:
        type_text(store, "small")
        counted = store.event_count
        size = paths.events_log(data_dir).stat().st_size
        huge = "x" * (MAX_RECORD_BODY + 8)
        with pytest.raises(RecordTooLarge):
            store.apply(insert(len(store.text), huge))
        assert store.text == "small"
        assert store.event_count == counted
        assert paths.events_log(data_dir).stat().st_size == size
        # A RecordTooLarge is an OpError, so the UI's existing handler catches it.
        with pytest.raises(OpError):
            store.apply(insert(len(store.text), huge))
    finally:
        store.close()


# --- checkpoint housekeeping ------------------------------------------------


def test_close_prunes_all_but_the_newest_checkpoints(data_dir: Path) -> None:
    config = Config(checkpoint_every_events=2, keep_checkpoints=3)
    clock = manual_clock()
    store = open_store(data_dir, config, clock)
    try:
        for index in range(20):
            clock.advance(SECOND)
            store.apply(insert(len(store.text), chr(ord("a") + index)))
            store.tick()
        assert len(checkpoint_mod.list_checkpoints(paths.checkpoints_dir(data_dir))) > 3
    finally:
        store.close()

    kept = checkpoint_mod.list_checkpoints(paths.checkpoints_dir(data_dir))
    assert len(kept) == 3
    assert kept[-1][0] == store.seq, "the newest checkpoint is the one at SESSION_STOP"

    reopened = open_store(data_dir)
    try:
        assert reopened.text == store.text
        # seq 0 is SESSION_START, so seq 3 is the third character.
        assert reopened.history.reconstruct_seq(3) == "abc"
    finally:
        reopened.close()


def test_reconstruction_does_not_list_the_checkpoint_directory_every_time(
    data_dir: Path, monkeypatch
) -> None:
    store = open_store(data_dir, Config(checkpoint_every_events=5))
    try:
        for index in range(40):
            store.apply(insert(len(store.text), "x"))
            store.tick()
        listings: list[Path] = []
        real = checkpoint_mod.list_checkpoints

        def counting(directory: Path):
            listings.append(directory)
            return real(directory)

        monkeypatch.setattr(checkpoint_mod, "list_checkpoints", counting)
        for seq in range(1, 40):
            store.history.reconstruct_seq(seq)
        assert len(listings) <= 1, "the listing is cached between reconstructions"

        # Writing a checkpoint invalidates the cache, so a new one is found.
        store.checkpoint()
        assert store.history.reconstruct_seq(store.seq) == store.text
        assert len(listings) == 2
    finally:
        store.close()


# --- current.meta -----------------------------------------------------------


def test_a_current_meta_from_another_format_version_is_ignored(data_dir: Path) -> None:
    store = open_store(data_dir)
    type_text(store, "version matters")
    store.close()

    # A sidecar that is consistent in every way except its format version: the
    # seq, the log length and even the checksum match, so only the version check
    # can stop the fast path from handing back that text.
    stale = "stale nonsense"
    meta_path = paths.current_meta(data_dir)
    meta = json.loads(meta_path.read_text())
    meta["version"] = 99
    meta["sha256"] = hashlib.sha256(stale.encode("utf-8")).hexdigest()
    meta["chars"] = len(stale)
    meta_path.write_text(json.dumps(meta, sort_keys=True) + "\n")
    paths.current_text(data_dir).write_text(stale)

    reopened = open_store(data_dir)
    try:
        assert reopened.text == "version matters"
    finally:
        reopened.close()


# --- a record that decodes but does not apply -------------------------------


def _repair_edit_checksum(blob: bytearray, offset: int) -> None:
    """Give the damaged edit record at ``offset`` a matching checksum byte."""
    import zlib

    from scratchpad.core.varint import decode_uvarint

    body_len, body_start = decode_uvarint(blob, offset)
    body_end = body_start + body_len
    blob[body_end] = zlib.crc32(bytes(blob[body_start:body_end])) & 0xFF


def test_an_unapplyable_record_is_treated_as_tail_damage(data_dir: Path, caplog) -> None:
    """A corrupt position that passes the one-byte checksum must not brick the app.

    reconstruct_seq() raised OpError out of ScratchpadStore.open() -- for good,
    on every start, with no way for the user to get back in.
    """
    store = open_store(data_dir)
    type_text(store, "hello")
    store.close()
    for _seq, path in checkpoint_mod.list_checkpoints(paths.checkpoints_dir(data_dir)):
        path.unlink()
    paths.current_text(data_dir).unlink()
    paths.current_meta(data_dir).unlink()

    log_path = paths.events_log(data_dir)
    events = list(EventLogReader(log_path).iter_events())
    victim = events[3]                      # the INSERT of the first `l`
    assert victim.op == insert(2, "l")

    blob = bytearray(log_path.read_bytes())
    from scratchpad.core.varint import decode_uvarint

    body_len, body_start = decode_uvarint(blob, victim.offset)
    _kind, after_kind = decode_uvarint(blob, body_start)
    _delta, payload_start = decode_uvarint(blob, after_kind)
    blob[payload_start] = 50                # INSERT at 50 into a 2 character document
    _repair_edit_checksum(blob, victim.offset)
    log_path.write_bytes(blob)

    scan = EventLogReader(log_path).scan()
    assert scan.tail_ok is True, "the damage passes framing and checksum"

    with caplog.at_level("ERROR", logger="scratchpad.core.store"):
        reopened = open_store(data_dir)
    try:
        assert reopened.text == "he", "the state before the unusable record"
        assert "does not apply" in " ".join(r.getMessage() for r in caplog.records)
        after = EventLogReader(log_path).scan()
        assert after.tail_ok is True
        assert after.good_length == log_path.stat().st_size
    finally:
        reopened.close()

    again = open_store(data_dir)
    try:
        assert again.text == "he"
    finally:
        again.close()
