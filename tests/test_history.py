"""Sessions, index, reconstruction and how fast a big log opens."""
from __future__ import annotations

import random
import time
from pathlib import Path

import pytest

from scratchpad.core import checkpoint as checkpoint_mod
from scratchpad.core.eventlog import (
    EventKind,
    EventLogReader,
    EventLogWriter,
    encode_op,
    encode_session_start,
    encode_wall_anchor,
)
from scratchpad.core.history import History
from scratchpad.core.ops import apply_op, delete, insert, replace

SECOND = 1_000_000_000
WALL0 = 1_700_000_000 * SECOND
MONO0 = 1_000 * SECOND


class LogBuilder:
    """Writes a log with a fully deterministic timeline."""

    def __init__(self, path: Path, *, step_ns: int = 100_000_000, buffer_bytes: int = 1 << 20):
        self.writer = EventLogWriter(path, buffer_bytes=buffer_bytes)
        self.step = step_ns
        self.mono = MONO0
        self.prev_mono = MONO0
        self.text = ""
        self.session_id = 0

    def _advance(self, ns: int | None = None) -> int:
        self.mono += self.step if ns is None else ns
        delta = self.mono - self.prev_mono
        self.prev_mono = self.mono
        return delta

    def session_start(self, wall_ns: int, *, session_id: int | None = None, gap_ns: int = 0) -> None:
        self.mono += gap_ns
        self.prev_mono = self.mono
        self.session_id = session_id if session_id is not None else 1000 + self.session_id
        self.writer.append(
            EventKind.SESSION_START, 0,
            encode_session_start(self.session_id, wall_ns, self.mono, "test"),
        )

    def session_stop(self) -> None:
        self.writer.append(EventKind.SESSION_STOP, self._advance(), b"")

    def heartbeat(self, wall_ns: int | None = None) -> None:
        self.writer.append(EventKind.HEARTBEAT, self._advance(), b"")
        if wall_ns is not None:
            self.writer.append(EventKind.WALL_ANCHOR, self._advance(0), encode_wall_anchor(wall_ns))

    def apply(self, op, *, ns: int | None = None) -> None:
        self.text = apply_op(self.text, op)
        kind, payload = encode_op(op)
        self.writer.append(kind, self._advance(ns), payload)

    def type(self, text: str, *, at: int | None = None) -> None:
        position = len(self.text) if at is None else at
        for index, char in enumerate(text):
            self.apply(insert(position + index, char))

    def close(self) -> None:
        self.writer.close()


def make_history(path: Path, *, stride: int = 256, checkpoints: Path | None = None) -> History:
    history = History(path, checkpoints or (path.parent / "checkpoints"), index_stride=stride)
    history.rebuild()
    return history


@pytest.fixture
def simple_log(tmp_path: Path) -> tuple[Path, History]:
    path = tmp_path / "events.log"
    builder = LogBuilder(path)
    builder.session_start(WALL0)
    builder.type("hello")
    builder.apply(delete(0, "h"))
    builder.apply(replace(0, "ell", "ELL"))
    builder.session_stop()
    builder.close()
    return path, make_history(path, stride=2)


def test_states_are_reproduced_step_by_step(simple_log) -> None:
    path, history = simple_log
    reader = EventLogReader(path)
    expected = ""
    for event in reader.iter_events():
        if event.op is not None:
            expected = apply_op(expected, event.op)
        assert history.reconstruct_seq(event.seq) == expected
    assert history.reconstruct_seq(-1) == ""
    assert history.reconstruct_seq(10_000) == expected


def test_reconstruct_at_semantics(simple_log) -> None:
    path, history = simple_log
    events = list(EventLogReader(path).iter_events())

    # Before the first event there is nothing.
    assert history.reconstruct_at(events[0].wall_ns - 1) == ("", -1)
    assert history.reconstruct_at(0) == ("", -1)

    # Exactly at an event: the state after it.
    for event in events:
        text, seq = history.reconstruct_at(event.wall_ns)
        assert seq == event.seq
        assert text == history.reconstruct_seq(event.seq)

    # Between two events: the state after the earlier one.
    midpoint = (events[2].wall_ns + events[3].wall_ns) // 2
    text, seq = history.reconstruct_at(midpoint)
    assert seq == events[2].seq
    assert text == history.reconstruct_seq(events[2].seq)

    # After the end: the final state.
    text, seq = history.reconstruct_at(events[-1].wall_ns + 10**12)
    assert seq == events[-1].seq
    assert text == "ELLo"


def test_time_range_and_event_lookup(simple_log) -> None:
    path, history = simple_log
    events = list(EventLogReader(path).iter_events())
    assert history.time_range() == (events[0].wall_ns, events[-1].wall_ns)
    assert history.count == len(events)
    assert history.last_seq == len(events) - 1

    assert history.event_at_or_before(events[0].wall_ns - 1) is None
    assert history.event_at_or_before(events[3].wall_ns).seq == 3
    assert history.event_at_or_before(events[3].wall_ns + 1).seq == 3
    assert history.event_after(events[3].wall_ns).seq == 4
    assert history.event_after(events[-1].wall_ns) is None
    assert history.event_after(0).seq == 0


def test_events_range_is_half_open(simple_log) -> None:
    _, history = simple_log
    assert [event.seq for event in history.events(2, 5)] == [2, 3, 4]
    assert list(history.events(3, 3)) == []
    assert [event.seq for event in history.events(0, 100)] == list(range(history.count))


def test_sessions_and_gaps(tmp_path: Path) -> None:
    path = tmp_path / "events.log"
    builder = LogBuilder(path)
    builder.session_start(WALL0)
    builder.type("morning note")
    builder.session_stop()
    # The machine was off for four hours.
    gap = 4 * 3600 * SECOND
    builder.session_start(WALL0 + gap, gap_ns=gap)
    builder.type(" afternoon")
    builder.close()                      # crash: no SESSION_STOP

    history = make_history(path)
    sessions = history.sessions()
    assert len(sessions) == 2
    assert sessions[0].clean_stop is True
    assert sessions[1].clean_stop is False
    assert sessions[0].session_id != sessions[1].session_id
    assert sessions[1].start_wall_ns - sessions[0].end_wall_ns > 3 * 3600 * SECOND
    assert sessions[0].last_seq + 1 == sessions[1].first_seq

    # A time inside the gap reconstructs the state the scratchpad was left in.
    inside_gap = sessions[0].end_wall_ns + 3600 * SECOND
    text, seq = history.reconstruct_at(inside_gap)
    assert text == "morning note"
    assert seq == sessions[0].last_seq


def test_crashed_session_ends_at_its_last_record(tmp_path: Path) -> None:
    path = tmp_path / "events.log"
    builder = LogBuilder(path, step_ns=30 * SECOND)
    builder.session_start(WALL0)
    builder.type("x")
    builder.heartbeat()
    builder.heartbeat()
    builder.close()
    history = make_history(path)
    session = history.sessions()[0]
    assert session.clean_stop is False
    # Three records after the start, 30 s apart.
    assert session.end_wall_ns == WALL0 + 3 * 30 * SECOND


def test_activity_counts_only_edits(tmp_path: Path) -> None:
    path = tmp_path / "events.log"
    builder = LogBuilder(path, step_ns=SECOND)
    builder.session_start(WALL0)
    builder.type("abcde")          # one edit per second
    builder.heartbeat()
    builder.type("fg")
    builder.session_stop()
    builder.close()
    history = make_history(path)

    start, end = WALL0, WALL0 + 10 * SECOND
    counts = history.activity(start, end, 10)
    assert sum(counts) == 7
    assert counts[0] == 0            # the SESSION_START second holds no edit
    assert counts[1] == 1
    assert history.activity(start, end, 0) == []
    assert history.activity(end, start, 4) == [0, 0, 0, 0]
    assert sum(history.activity(start, end, 1)) == 7


def test_activity_histogram_path_matches_the_exact_path(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "events.log"
    builder = LogBuilder(path, step_ns=SECOND)
    builder.session_start(WALL0)
    builder.type("a" * 50)
    builder.close()
    history = make_history(path, stride=8)
    window = (WALL0, WALL0 + 60 * SECOND, 12)
    exact = history.activity(*window)
    monkeypatch.setattr("scratchpad.core.history.EXACT_ACTIVITY_EVENTS", 0)
    assert history.activity(*window) == exact


# --- checkpoints ------------------------------------------------------------


def _typing_log(path: Path, count: int, *, checkpoints: Path | None = None, every: int = 50):
    builder = LogBuilder(path, step_ns=10_000_000)
    builder.session_start(WALL0)
    states = [""]
    for index in range(count):
        builder.apply(insert(len(builder.text), chr(ord("a") + index % 26)))
        states.append(builder.text)
        if checkpoints is not None and (index + 1) % every == 0:
            checkpoint_mod.write_checkpoint(checkpoints, index + 1, WALL0, builder.text)
    builder.close()
    return states


def test_checkpoints_are_only_a_cache(tmp_path: Path) -> None:
    path = tmp_path / "events.log"
    checkpoints = tmp_path / "checkpoints"
    states = _typing_log(path, 300, checkpoints=checkpoints)

    with_ckpt = make_history(path, stride=16, checkpoints=checkpoints)
    without = make_history(path, stride=16, checkpoints=tmp_path / "gone")
    for seq in range(1, 301):
        assert with_ckpt.reconstruct_seq(seq) == states[seq]
        assert without.reconstruct_seq(seq) == states[seq]


def test_a_lying_checkpoint_is_detected_and_bypassed(tmp_path: Path, caplog) -> None:
    path = tmp_path / "events.log"
    checkpoints = tmp_path / "checkpoints"
    states = _typing_log(path, 120, checkpoints=checkpoints, every=50)
    # Corrupt a checkpoint's *content* so that it decodes but disagrees with the log.
    checkpoint_mod.write_checkpoint(checkpoints, 100, WALL0, "not what the log says")
    history = make_history(path, stride=16, checkpoints=checkpoints)
    assert history.reconstruct_seq(120) == states[120]


def test_reconstruction_uses_the_index_not_the_whole_log(tmp_path: Path) -> None:
    path = tmp_path / "events.log"
    _typing_log(path, 600)
    history = make_history(path, stride=256)
    entries = history.index_entries()
    assert [entry.seq for entry in entries] == [0, 256, 512]
    for entry in entries:
        assert entry.state.offset == entry.offset
    # Index entries never sit further apart than the stride.
    assert all(b.seq - a.seq <= 256 for a, b in zip(entries, entries[1:]))


def test_random_targets_match_a_naive_replay(tmp_path: Path) -> None:
    path = tmp_path / "events.log"
    checkpoints = tmp_path / "checkpoints"
    states = _typing_log(path, 400, checkpoints=checkpoints, every=64)
    history = make_history(path, stride=32, checkpoints=checkpoints)
    events = list(EventLogReader(path).iter_events())
    rng = random.Random(7)
    for _ in range(80):
        event = rng.choice(events)
        text, seq = history.reconstruct_at(event.wall_ns)
        assert seq == event.seq
        edits = sum(1 for e in events[: event.seq + 1] if e.op is not None)
        assert text == states[edits]


# --- live updates -----------------------------------------------------------


def test_note_event_keeps_the_index_identical_to_a_rescan(tmp_path: Path) -> None:
    """The store feeds events in live; that must equal what a rescan produces."""
    path = tmp_path / "events.log"
    writer = EventLogWriter(path)
    history = History(path, tmp_path / "checkpoints", index_stride=4)
    history.rebuild()

    mono = MONO0
    writer.append(EventKind.SESSION_START, 0, encode_session_start(77, WALL0, mono, "t"))
    from scratchpad.core.eventlog import Event

    history.note_event(
        Event(0, EventKind.SESSION_START, mono, WALL0, 77, None, 12), anchor_wall_ns=WALL0
    )
    text = ""
    for index in range(40):
        op = insert(index, "q")
        kind, payload = encode_op(op)
        mono += 25_000_000
        offset = writer.append(kind, 25_000_000, payload)
        text = apply_op(text, op)
        history.note_event(
            Event(index + 1, EventKind.INSERT, mono, WALL0 + (mono - MONO0), 77, op, offset)
        )
    writer.flush()

    fresh = make_history(path, stride=4)
    assert history.index_entries() == fresh.index_entries()
    assert history.sessions() == fresh.sessions()
    assert history.time_range() == fresh.time_range()
    assert history.count == fresh.count
    assert history.reconstruct_seq(history.last_seq) == text
    assert history.activity(WALL0, WALL0 + 10 * SECOND, 8) == fresh.activity(
        WALL0, WALL0 + 10 * SECOND, 8
    )
    writer.close()


# --- performance ------------------------------------------------------------


def test_large_log_opens_fast_and_reconstructs_fast(tmp_path: Path) -> None:
    """Target: open a 200k event log in under 2 s, reconstruct in under 100 ms."""
    count = 200_000
    checkpoint_every = 2_000
    path = tmp_path / "events.log"
    checkpoints = tmp_path / "checkpoints"
    checkpoints.mkdir()

    started = time.perf_counter()
    builder = LogBuilder(path, step_ns=120_000_000)      # ~8 keystrokes per second
    builder.session_start(WALL0)
    document = ""
    for index in range(count):
        if len(document) > 12_000:
            # Occasionally the user clears the top half of the scratchpad.
            head = document[:6_000]
            builder.apply(delete(0, head))
            document = document[6_000:]
        else:
            builder.apply(insert(len(document), "abcdefghij\n"[index % 11]))
            document += "abcdefghij\n"[index % 11]
        if (index + 1) % checkpoint_every == 0:
            checkpoint_mod.write_checkpoint(checkpoints, index + 1, WALL0, document)
    builder.close()
    generated = time.perf_counter() - started
    size = path.stat().st_size

    started = time.perf_counter()
    history = History(path, checkpoints, index_stride=256)
    scan = history.rebuild()
    open_seconds = time.perf_counter() - started
    assert scan.tail_ok and scan.count == count + 1

    first, last = history.time_range()
    rng = random.Random(99)
    targets = [rng.randrange(first, last) for _ in range(25)]
    timings = []
    for target in targets:
        started = time.perf_counter()
        text, seq = history.reconstruct_at(target)
        timings.append(time.perf_counter() - started)
        assert seq >= 0
    timings.sort()

    started = time.perf_counter()
    without_checkpoints = History(path, tmp_path / "none", index_stride=256)
    without_checkpoints.adopt(scan)
    cold = without_checkpoints.reconstruct_at(targets[0])[0]
    cold_seconds = time.perf_counter() - started
    assert cold == history.reconstruct_at(targets[0])[0]

    print(
        f"\n{count} events, {size/1e6:.2f} MB ({size/count:.1f} bytes/event), "
        f"{len(document)} char document"
        f"\n  generated in       {generated:6.2f} s"
        f"\n  open (scan+index)  {open_seconds*1000:6.1f} ms"
        f"\n  reconstruct_at     min {timings[0]*1000:.1f} ms / "
        f"median {timings[len(timings)//2]*1000:.1f} ms / max {timings[-1]*1000:.1f} ms"
        f"\n  reconstruct_at without any checkpoint: {cold_seconds*1000:.0f} ms"
    )
    assert open_seconds < 2.0, f"opening took {open_seconds:.2f} s"
    assert timings[-1] < 0.1, f"slowest reconstruct took {timings[-1]*1000:.0f} ms"


# --- session segments -------------------------------------------------------


def test_a_suspend_inside_a_session_becomes_two_segments(tmp_path: Path) -> None:
    """Three hours with the lid closed is not three hours of running.

    CLOCK_BOOTTIME keeps counting while the machine sleeps, so the resume shows
    up as a three hour hole between two consecutive records of one session.
    """
    path = tmp_path / "events.log"
    builder = LogBuilder(path, step_ns=SECOND)
    builder.session_start(WALL0)
    builder.type("before")
    suspend = 3 * 3600 * SECOND
    builder.apply(insert(len(builder.text), "!"), ns=suspend)      # resume
    builder.type(" after")
    builder.session_stop()
    builder.close()

    history = make_history(path)
    sessions = history.sessions()
    assert len(sessions) == 2
    first, second = sessions
    assert first.session_id == second.session_id
    assert (first.segment, second.segment) == (0, 1)
    assert second.start_wall_ns - first.end_wall_ns >= suspend
    assert first.clean_stop is False, "only the last segment can end cleanly"
    assert second.clean_stop is True
    assert first.last_seq + 1 == second.first_seq
    assert first.first_seq == 0 and second.last_seq == history.last_seq

    # The segments are only a view of the timeline; the log is untouched.
    assert history.reconstruct_seq(history.last_seq) == "before! after"
    assert history.reconstruct_at(first.end_wall_ns)[0] == "before"


def test_ordinary_idle_time_does_not_split_a_session(tmp_path: Path) -> None:
    path = tmp_path / "events.log"
    builder = LogBuilder(path, step_ns=30 * SECOND)     # heartbeats, 30 s apart
    builder.session_start(WALL0)
    builder.type("a")
    for _ in range(10):
        builder.heartbeat()
    builder.type("b")
    builder.session_stop()
    builder.close()

    assert len(make_history(path).sessions()) == 1
    # A longer heartbeat interval raises the threshold with it.
    slow = History(path, tmp_path / "ckpt", heartbeat_seconds=600)
    slow.rebuild()
    assert slow.gap_threshold_ns == 4 * 600 * SECOND
    assert len(slow.sessions()) == 1


def test_the_gap_threshold_follows_the_configured_heartbeat(tmp_path: Path) -> None:
    path = tmp_path / "events.log"
    builder = LogBuilder(path, step_ns=SECOND)
    builder.session_start(WALL0)
    builder.type("x")
    builder.apply(insert(1, "y"), ns=10 * 60 * SECOND)      # a ten minute pause
    builder.session_stop()
    builder.close()

    default = History(path, tmp_path / "ckpt")
    default.rebuild()
    assert default.gap_threshold_ns == 120 * SECOND
    assert len(default.sessions()) == 2

    patient = History(path, tmp_path / "ckpt", heartbeat_seconds=3600)
    patient.rebuild()
    assert len(patient.sessions()) == 1, "4 h threshold swallows a 10 min pause"

    # The floor holds even for an absurdly small heartbeat interval.
    eager = History(path, tmp_path / "ckpt", heartbeat_seconds=1)
    eager.rebuild()
    assert eager.gap_threshold_ns == 120 * SECOND


def test_live_events_produce_the_same_segments_as_a_rescan(tmp_path: Path) -> None:
    from scratchpad.core.eventlog import Event

    path = tmp_path / "events.log"
    writer = EventLogWriter(path)
    history = History(path, tmp_path / "checkpoints", index_stride=4)
    history.rebuild()

    mono = MONO0
    writer.append(EventKind.SESSION_START, 0, encode_session_start(9, WALL0, mono, "t"))
    history.note_event(Event(0, EventKind.SESSION_START, mono, WALL0, 9, None, 12),
                       anchor_wall_ns=WALL0)
    text = ""
    for index in range(6):
        op = insert(index, "z")
        kind, payload = encode_op(op)
        step = 3 * 3600 * SECOND if index == 3 else SECOND     # one suspend
        mono += step
        offset = writer.append(kind, step, payload)
        text = apply_op(text, op)
        history.note_event(
            Event(index + 1, EventKind.INSERT, mono, WALL0 + (mono - MONO0), 9, op, offset)
        )
    writer.close()

    fresh = make_history(path, stride=4)
    assert history.sessions() == fresh.sessions()
    assert len(history.sessions()) == 2
    assert [session.segment for session in history.sessions()] == [0, 1]


# --- a record that decodes but does not apply -------------------------------


def _break_an_insert_position(path: Path, offset: int, position: int) -> None:
    """Rewrite the INSERT at ``offset`` to an impossible position, checksum and all.

    ``position`` stays below 128 so that it remains a single varint byte: the
    record must still *decode* perfectly -- the damage is only that it cannot be
    applied to the document in front of it.
    """
    import zlib

    from scratchpad.core.varint import decode_uvarint

    blob = bytearray(path.read_bytes())
    body_len, body_start = decode_uvarint(blob, offset)
    body_end = body_start + body_len
    _kind, after_kind = decode_uvarint(blob, body_start)
    _delta, payload_start = decode_uvarint(blob, after_kind)
    blob[payload_start] = position
    blob[body_end] = zlib.crc32(bytes(blob[body_start:body_end])) & 0xFF
    path.write_bytes(blob)


def test_a_record_that_does_not_apply_ends_the_replay_instead_of_raising(
    tmp_path: Path, caplog
) -> None:
    path = tmp_path / "events.log"
    builder = LogBuilder(path)
    builder.session_start(WALL0)
    builder.type("hello")
    builder.close()

    events = list(EventLogReader(path).iter_events())
    _break_an_insert_position(path, events[3].offset, 50)

    history = make_history(path)
    with caplog.at_level("ERROR", logger="scratchpad.core.history"):
        text = history.reconstruct_seq(history.last_seq)
    assert text == "he", "everything up to the unusable record"
    assert history.replay_damage is not None
    assert history.replay_damage.seq == events[3].seq
    assert history.replay_damage.offset == events[3].offset
    assert "does not apply" in " ".join(r.getMessage() for r in caplog.records)

    # Also from a checkpoint that is *older* than the damage: the checkpoint is
    # retreated past, and the full replay still stops instead of raising.
    checkpoints = tmp_path / "checkpoints"
    checkpoint_mod.write_checkpoint(checkpoints, 2, WALL0, "he")
    with_ckpt = make_history(path, checkpoints=checkpoints)
    assert with_ckpt.reconstruct_seq(with_ckpt.last_seq) == "he"
    assert with_ckpt.replay_damage is not None


def test_reconstruct_at_also_survives_an_unapplyable_record(tmp_path: Path) -> None:
    path = tmp_path / "events.log"
    builder = LogBuilder(path)
    builder.session_start(WALL0)
    builder.type("abcdef")
    builder.close()
    events = list(EventLogReader(path).iter_events())
    _break_an_insert_position(path, events[4].offset, 100)

    history = make_history(path)
    text, seq = history.reconstruct_at(events[-1].wall_ns)
    assert text == "abc"
    assert seq == events[-1].seq


# --- checkpoint listing cache ----------------------------------------------


def test_the_checkpoint_listing_is_cached_and_invalidated(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "events.log"
    checkpoints = tmp_path / "checkpoints"
    states = _typing_log(path, 120, checkpoints=checkpoints, every=50)
    history = make_history(path, stride=16, checkpoints=checkpoints)

    calls: list[Path] = []
    real = checkpoint_mod.list_checkpoints

    def counting(directory: Path):
        calls.append(directory)
        return real(directory)

    monkeypatch.setattr(checkpoint_mod, "list_checkpoints", counting)
    for seq in range(1, 120):
        assert history.reconstruct_seq(seq) == states[seq]
    assert len(calls) == 1, "one listing for a hundred reconstructions"

    checkpoint_mod.write_checkpoint(checkpoints, 110, WALL0, states[110])
    history.invalidate_checkpoints()
    assert history.reconstruct_seq(115) == states[115]
    assert len(calls) == 2

    # A stale cache entry (the file was pruned) costs replay time, not data.
    (checkpoints / checkpoint_mod.checkpoint_name(110)).unlink()
    assert history.reconstruct_seq(115) == states[115]
