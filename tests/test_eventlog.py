"""Binary log framing, torn tails, corruption and the per-record byte cost."""
from __future__ import annotations

import errno
import os
import random
import zlib
from pathlib import Path

import pytest

from scratchpad.core.eventlog import (
    EDIT_KINDS,
    FULL_CRC_KINDS,
    HEADER_SIZE,
    MAGIC,
    Event,
    EventKind,
    EventLogReader,
    EventLogWriter,
    LogFormatError,
    RecordTooLarge,
    build_record,
    encode_op,
    encode_session_start,
    encode_wall_anchor,
    ensure_log,
    file_header,
)
from scratchpad.core.ops import OpError, delete, insert, replace

SESSION_ID = 0x0BADC0DE
WALL0 = 1_700_000_000_000_000_000
MONO0 = 5_000_000_000


def build_log(path: Path, *, typed: str = "hello", delta: int = 100_000_000) -> list[int]:
    """Write a small but representative log; returns the record offsets."""
    offsets = []
    writer = EventLogWriter(path)
    offsets.append(
        writer.append(
            EventKind.SESSION_START, 0,
            encode_session_start(SESSION_ID, WALL0, MONO0, "0.1.0"),
        )
    )
    for index, char in enumerate(typed):
        kind, payload = encode_op(insert(index, char))
        offsets.append(writer.append(kind, delta, payload))
    offsets.append(writer.append(EventKind.HEARTBEAT, delta, b""))
    offsets.append(writer.append(EventKind.WALL_ANCHOR, 1000, encode_wall_anchor(WALL0 + 10**9)))
    kind, payload = encode_op(delete(0, typed[0]))
    offsets.append(writer.append(kind, delta, payload))
    kind, payload = encode_op(replace(0, typed[1:3], "XY"))
    offsets.append(writer.append(kind, delta, payload))
    offsets.append(writer.append(EventKind.SESSION_STOP, delta, b""))
    writer.close()
    return offsets


def test_header_and_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "events.log"
    offsets = build_log(path)
    assert path.read_bytes()[:HEADER_SIZE] == file_header()
    assert path.read_bytes()[:8] == MAGIC

    events = list(EventLogReader(path).iter_events())
    assert [event.offset for event in events] == offsets
    assert [event.seq for event in events] == list(range(len(offsets)))
    assert events[0].kind is EventKind.SESSION_START
    assert events[0].session_id == SESSION_ID
    assert events[1].op == insert(0, "h")
    assert events[-1].kind is EventKind.SESSION_STOP
    assert all(event.session_id == SESSION_ID for event in events)


def test_derived_wall_clock_is_anchored_and_non_decreasing(tmp_path: Path) -> None:
    path = tmp_path / "events.log"
    build_log(path)
    events = list(EventLogReader(path).iter_events())
    assert events[0].wall_ns == WALL0
    assert events[1].wall_ns == WALL0 + 100_000_000       # anchor + mono delta
    walls = [event.wall_ns for event in events]
    assert walls == sorted(walls)


def test_wall_anchor_re_anchors_the_derivation(tmp_path: Path) -> None:
    path = tmp_path / "events.log"
    writer = EventLogWriter(path)
    writer.append(EventKind.SESSION_START, 0, encode_session_start(1, WALL0, MONO0, "t"))
    # The wall clock jumped forward by an hour while only 1 ms of monotonic
    # time passed (an NTP step, or a resume from suspend).
    jumped = WALL0 + 3600 * 10**9
    writer.append(EventKind.WALL_ANCHOR, 1_000_000, encode_wall_anchor(jumped))
    kind, payload = encode_op(insert(0, "a"))
    writer.append(kind, 1_000_000, payload)
    writer.close()

    events = list(EventLogReader(path).iter_events())
    assert events[1].wall_ns == jumped
    assert events[2].wall_ns == jumped + 1_000_000


def test_backwards_wall_clock_is_clamped(tmp_path: Path) -> None:
    path = tmp_path / "events.log"
    writer = EventLogWriter(path)
    writer.append(EventKind.SESSION_START, 0, encode_session_start(1, WALL0, MONO0, "t"))
    writer.append(EventKind.WALL_ANCHOR, 1_000_000, encode_wall_anchor(WALL0 - 60 * 10**9))
    kind, payload = encode_op(insert(0, "a"))
    writer.append(kind, 1_000_000, payload)
    writer.close()

    events = list(EventLogReader(path).iter_events())
    walls = [event.wall_ns for event in events]
    assert walls == sorted(walls), "the derived timeline must never go backwards"
    assert walls[1] == WALL0


def test_scan_reports_sessions_and_index(tmp_path: Path) -> None:
    path = tmp_path / "events.log"
    offsets = build_log(path)
    result = EventLogReader(path).scan(index_stride=4)
    assert result.tail_ok is True
    assert result.count == len(offsets)
    assert result.good_length == path.stat().st_size
    assert result.last_event is not None and result.last_event.kind is EventKind.SESSION_STOP
    assert len(result.sessions) == 1
    session = result.sessions[0]
    assert session.session_id == SESSION_ID
    assert session.clean_stop is True
    assert (session.first_seq, session.last_seq) == (0, len(offsets) - 1)
    assert [entry.seq for entry in result.index] == [0, 4, 8]
    assert [entry.offset for entry in result.index] == [offsets[0], offsets[4], offsets[8]]


def test_index_entries_are_resume_points(tmp_path: Path) -> None:
    path = tmp_path / "events.log"
    build_log(path, typed="abcdefghij")
    reader = EventLogReader(path)
    everything = list(reader.iter_events())
    for entry in reader.scan(index_stride=2).index:
        resumed = list(reader.iter_events(entry.offset, state=entry.state))
        assert resumed == everything[entry.seq:], f"resume at seq {entry.seq}"


def test_unclean_session_ends_at_its_last_record(tmp_path: Path) -> None:
    path = tmp_path / "events.log"
    writer = EventLogWriter(path)
    writer.append(EventKind.SESSION_START, 0, encode_session_start(1, WALL0, MONO0, "t"))
    writer.append(EventKind.HEARTBEAT, 30 * 10**9, b"")
    writer.append(EventKind.SESSION_START, 0, encode_session_start(2, WALL0 + 10**12, MONO0, "t"))
    writer.append(EventKind.SESSION_STOP, 10**9, b"")
    writer.close()

    sessions = EventLogReader(path).scan().sessions
    assert len(sessions) == 2
    assert sessions[0].clean_stop is False
    assert sessions[0].end_wall_ns == WALL0 + 30 * 10**9
    assert sessions[1].clean_stop is True


# --- byte cost --------------------------------------------------------------


def test_ascii_keystroke_byte_cost_matches_the_documented_table() -> None:
    """The per-record cost table in eventlog.py's module docstring."""
    def keystroke(pos: int, delta: int) -> int:
        kind, payload = encode_op(insert(pos, "a"))
        return len(build_record(kind, delta, payload))

    fast = 100_000_000        # 100 ms between keystrokes
    slow = 1_000_000_000      # a one second pause
    assert keystroke(80, fast) == 10           # tiny document
    assert keystroke(5_000, fast) == 11        # the ordinary case
    assert keystroke(5_000, slow) == 12
    assert keystroke(1_000_000, fast) == 12    # a 1 MB document
    assert keystroke(1_000_000, slow) == 13    # the worst case
    # The contract's target: under 12 bytes for a document of scratchpad size.
    for pos in (0, 127, 128, 16_000):
        assert keystroke(pos, fast) < 12
    for pos in (0, 127, 128, 16_383, 16_384, 1_000_000):
        for delta in (fast, slow):
            assert keystroke(pos, delta) <= 13


def test_lifecycle_records_keep_the_full_crc32() -> None:
    for kind in FULL_CRC_KINDS:
        record = build_record(kind, 1000, b"")
        body = record[1:-4]
        assert int.from_bytes(record[-4:], "little") == zlib.crc32(body) & 0xFFFFFFFF
    for kind in EDIT_KINDS:
        record = build_record(kind, 1000, b"\x00\x01a")
        body = record[1:-1]
        assert record[-1] == zlib.crc32(body) & 0xFF


def test_average_cost_of_typing_a_realistic_line(tmp_path: Path) -> None:
    path = tmp_path / "events.log"
    line = "investigate CAN timeout on the rear gateway\n"
    writer = EventLogWriter(path)
    writer.append(EventKind.SESSION_START, 0, encode_session_start(1, WALL0, MONO0, "0.1.0"))
    for index, char in enumerate(line):
        kind, payload = encode_op(insert(index, char))
        writer.append(kind, 140_000_000, payload)       # ~7 characters per second
    writer.close()
    typing_bytes = path.stat().st_size - HEADER_SIZE - 29
    per_keystroke = typing_bytes / len(line)
    print(f"\ntyping cost: {per_keystroke:.2f} bytes per ASCII keystroke")
    assert per_keystroke <= 11.0


# --- torn tails and corruption ---------------------------------------------


def _record_boundaries(path: Path) -> list[int]:
    """Offsets just past each record, i.e. every valid truncation point."""
    reader = EventLogReader(path)
    boundaries = [HEADER_SIZE]
    events = list(reader.iter_events())
    for event in events[1:]:
        boundaries.append(event.offset)
    boundaries.append(path.stat().st_size)
    return boundaries


def test_truncation_at_every_byte_offset_of_the_last_records(tmp_path: Path) -> None:
    source = tmp_path / "events.log"
    build_log(source, typed="hello world")
    blob = source.read_bytes()
    boundaries = set(_record_boundaries(source))
    complete = list(EventLogReader(source).iter_events())
    start = sorted(boundaries)[-5]        # the last four records

    target = tmp_path / "torn.log"
    for cut in range(start, len(blob) + 1):
        target.write_bytes(blob[:cut])
        result = EventLogReader(target).scan()
        good = max(b for b in boundaries if b <= cut)
        assert result.good_length == good, f"cut at {cut}"
        assert result.tail_ok is (cut in boundaries), f"cut at {cut}"
        assert result.count == sum(1 for b in boundaries if b <= cut) - 1
        events = list(EventLogReader(target).iter_events())
        assert events == complete[: result.count], f"cut at {cut}"


def test_truncation_inside_the_header(tmp_path: Path) -> None:
    source = tmp_path / "events.log"
    build_log(source)
    blob = source.read_bytes()
    target = tmp_path / "torn.log"
    for cut in range(0, HEADER_SIZE):
        target.write_bytes(blob[:cut])
        result = EventLogReader(target).scan()
        if cut == 0:
            assert result.tail_ok and result.count == 0
        else:
            assert result.tail_ok is False
            assert result.count == 0


def test_every_single_bit_flip_in_the_tail_is_handled(tmp_path: Path) -> None:
    source = tmp_path / "events.log"
    build_log(source, typed="hello world")
    blob = bytearray(source.read_bytes())
    boundaries = _record_boundaries(source)
    complete = list(EventLogReader(source).iter_events())
    start = boundaries[-5]

    target = tmp_path / "flipped.log"
    flips = 0
    detected = 0
    for position in range(start, len(blob)):
        first_record = max(b for b in boundaries if b <= position)
        intact = sum(1 for b in boundaries if b <= first_record) - 1
        for bit in range(8):
            flips += 1
            damaged = bytearray(blob)
            damaged[position] ^= 1 << bit
            target.write_bytes(damaged)
            result = EventLogReader(target).scan()            # must never raise
            events = list(EventLogReader(target).iter_events())
            # Everything before the damaged record survives unchanged.
            assert events[:intact] == complete[:intact], f"byte {position} bit {bit}"
            if not result.tail_ok:
                detected += 1
                assert result.good_length <= first_record
    rate = detected / flips
    print(f"\nsingle-bit flips in the last records: {detected}/{flips} detected ({rate:.1%})")
    assert rate >= 0.95


def test_flips_in_a_lifecycle_record_are_always_detected(tmp_path: Path) -> None:
    source = tmp_path / "events.log"
    build_log(source)
    blob = bytearray(source.read_bytes())
    first_edit = list(EventLogReader(source).iter_events())[1].offset
    target = tmp_path / "flipped.log"
    for position in range(HEADER_SIZE, first_edit):
        for bit in range(8):
            damaged = bytearray(blob)
            damaged[position] ^= 1 << bit
            target.write_bytes(damaged)
            result = EventLogReader(target).scan()
            assert result.tail_ok is False, f"SESSION_START byte {position} bit {bit}"
            assert result.count == 0


def test_random_multi_byte_corruption_never_crashes(tmp_path: Path) -> None:
    source = tmp_path / "events.log"
    build_log(source, typed="the quick brown fox")
    blob = bytearray(source.read_bytes())
    rng = random.Random(20240914)
    target = tmp_path / "noise.log"
    for _ in range(300):
        damaged = bytearray(blob)
        for _ in range(rng.randint(1, 6)):
            damaged[rng.randrange(HEADER_SIZE, len(damaged))] = rng.randrange(256)
        target.write_bytes(damaged)
        result = EventLogReader(target).scan(index_stride=4)
        assert HEADER_SIZE <= result.good_length <= len(damaged)
        list(EventLogReader(target).iter_events())


def test_writer_truncates_a_torn_tail_and_keeps_appending(tmp_path: Path) -> None:
    path = tmp_path / "events.log"
    build_log(path)
    blob = path.read_bytes()
    path.write_bytes(blob + b"\x0c\x05\x80")       # a half written record
    result = EventLogReader(path).scan()
    assert result.tail_ok is False
    assert result.good_length == len(blob)

    writer = EventLogWriter(path)
    writer.truncate(result.good_length)
    kind, payload = encode_op(insert(0, "z"))
    writer.append(kind, 500, payload)
    writer.close()

    after = EventLogReader(path).scan()
    assert after.tail_ok is True
    assert after.count == result.count + 1
    assert after.last_event is not None and after.last_event.op == insert(0, "z")


def test_bad_magic_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "events.log"
    path.write_bytes(b"NOTALOG!" + b"\x01\x00\x00\x00" + b"junk")
    with pytest.raises(LogFormatError):
        EventLogReader(path).scan()
    with pytest.raises(LogFormatError):
        ensure_log(path)


def test_empty_and_torn_header_files_are_recreated(tmp_path: Path) -> None:
    path = tmp_path / "events.log"
    assert ensure_log(path) == HEADER_SIZE
    assert path.read_bytes() == file_header()
    path.write_bytes(MAGIC[:4])
    assert ensure_log(path) == HEADER_SIZE
    assert path.read_bytes() == file_header()


def test_writer_hands_every_append_to_the_os(tmp_path: Path) -> None:
    """An application crash must not lose an appended record."""
    path = tmp_path / "events.log"
    writer = EventLogWriter(path)
    writer.append(EventKind.SESSION_START, 0, encode_session_start(1, WALL0, MONO0, "t"))
    kind, payload = encode_op(insert(0, "x"))
    writer.append(kind, 1000, payload)
    # No flush, no close: the bytes must already be visible to another reader.
    assert os.path.getsize(path) == writer.size
    assert EventLogReader(path).scan().count == 2
    writer.close()


def test_buffered_writer_is_opt_in(tmp_path: Path) -> None:
    path = tmp_path / "events.log"
    writer = EventLogWriter(path, buffer_bytes=1 << 16)
    writer.append(EventKind.SESSION_START, 0, encode_session_start(1, WALL0, MONO0, "t"))
    assert os.path.getsize(path) == HEADER_SIZE
    writer.flush()
    assert os.path.getsize(path) == writer.size
    writer.close()


def test_reader_sees_appends_without_reopening(tmp_path: Path) -> None:
    path = tmp_path / "events.log"
    writer = EventLogWriter(path)
    writer.append(EventKind.SESSION_START, 0, encode_session_start(1, WALL0, MONO0, "t"))
    reader = EventLogReader(path)
    assert reader.scan().count == 1
    kind, payload = encode_op(insert(0, "x"))
    writer.append(kind, 1000, payload)
    assert reader.scan().count == 2
    writer.close()


def test_unicode_payloads_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "events.log"
    writer = EventLogWriter(path)
    writer.append(EventKind.SESSION_START, 0, encode_session_start(1, WALL0, MONO0, "t"))
    text = "äöü \U0001f600 你好"
    kind, payload = encode_op(insert(0, text))
    writer.append(kind, 1000, payload)
    kind, payload = encode_op(replace(0, text, "x"))
    writer.append(kind, 1000, payload)
    writer.close()
    events = list(EventLogReader(path).iter_events())
    assert events[1].op == insert(0, text)
    assert events[2].op == replace(0, text, "x")


def test_lifecycle_record_costs_match_the_documented_numbers() -> None:
    session_id = (1 << 62) + 12345
    wall = 1_789_000_000_000_000_000
    assert len(build_record(
        EventKind.SESSION_START, 0, encode_session_start(session_id, wall, 10**13, "0.1.0")
    )) == 38
    assert len(build_record(EventKind.SESSION_STOP, 10**9, b"")) == 11
    assert len(build_record(EventKind.HEARTBEAT, 30 * 10**9, b"")) == 11
    assert len(build_record(EventKind.WALL_ANCHOR, 1000, encode_wall_anchor(wall))) == 17


# --- corrupt-but-plausible records ------------------------------------------


def _body_bounds(blob: bytearray, offset: int) -> tuple[int, int, int]:
    """``(body_start, body_end, checksum_width)`` of the record at ``offset``."""
    from scratchpad.core.varint import decode_uvarint

    body_len, body_start = decode_uvarint(blob, offset)
    kind, _ = decode_uvarint(blob, body_start)
    return body_start, body_start + body_len, 4 if kind in FULL_CRC_KINDS else 1


def _repair_checksum(blob: bytearray, offset: int) -> int:
    """Make the damaged record at ``offset`` pass its checksum again.

    On an edit record the checksum is one byte, so a random corruption has a
    1/256 chance of doing this by itself; brute forcing the byte is the cheapest
    way to reproduce that collision deterministically.
    """
    body_start, body_end, width = _body_bounds(blob, offset)
    crc = zlib.crc32(bytes(blob[body_start:body_end])) & 0xFFFFFFFF
    if width == 4:
        blob[body_end:body_end + 4] = crc.to_bytes(4, "little")
        return crc
    for candidate in range(256):
        if candidate == crc & 0xFF:
            blob[body_end] = candidate
            return candidate
    raise AssertionError("no checksum byte matches")   # pragma: no cover


def _payload_bounds(blob: bytearray, offset: int) -> tuple[int, int]:
    """``(payload_start, body_end)`` of the record at ``offset``."""
    from scratchpad.core.varint import decode_uvarint

    body_start, body_end, _ = _body_bounds(blob, offset)
    _kind, after_kind = decode_uvarint(blob, body_start)
    _delta, payload_start = decode_uvarint(blob, after_kind)
    return payload_start, body_end


def test_a_corrupt_string_length_mid_log_is_a_torn_tail(tmp_path: Path) -> None:
    """A checksum collision must not turn into silent data loss.

    The reviewer's scenario: SESSION_START, `hello` typed, SESSION_STOP, and one
    middle record whose string length says "longer than the record".  scan()
    used to call that log intact (it never looked at the payload) while
    iter_events() quietly stopped there -- the document lost every character
    behind the damage and nobody was told.
    """
    path = tmp_path / "events.log"
    offsets = build_log(path, typed="hello")
    intact = list(EventLogReader(path).iter_events())
    damaged_offset = offsets[3]                  # the second `l` of "hello"

    blob = bytearray(path.read_bytes())
    payload_start, _body_end = _payload_bounds(blob, damaged_offset)
    blob[payload_start + 1] = 0x7F                # the text is suddenly 127 bytes long
    _repair_checksum(blob, damaged_offset)
    path.write_bytes(blob)

    result = EventLogReader(path).scan()
    assert result.tail_ok is False
    assert result.good_length == damaged_offset
    assert result.count == 3                      # SESSION_START, `h`, `e`
    assert "runs past the record" in result.error
    # ... and the reader agrees with the scanner about where the log ends.
    assert list(EventLogReader(path).iter_events()) == intact[:3]


def test_a_corrupt_string_length_in_the_last_record_never_raises(tmp_path: Path) -> None:
    """scan() used to decode the last record's payload outside every guard."""
    from scratchpad.core.varint import encode_uvarint

    path = tmp_path / "events.log"
    offsets = build_log(path, typed="hi")
    last = offsets[-1]

    # Replace the last record (SESSION_STOP) with an INSERT that claims a 90
    # byte string it does not carry -- the reviewer's "string at 38 runs past
    # the record".
    blob = bytearray(path.read_bytes())
    del blob[last:]
    body = bytearray()
    body += encode_uvarint(int(EventKind.INSERT))
    body += encode_uvarint(1000)        # mono delta
    body += encode_uvarint(0)           # pos
    body += encode_uvarint(90)          # string length
    blob += encode_uvarint(len(body))
    blob += body
    blob.append(0)                      # placeholder checksum
    _repair_checksum(blob, last)
    path.write_bytes(blob)

    result = EventLogReader(path).scan()          # must not raise VarintError
    assert result.tail_ok is False
    assert result.good_length == last
    assert result.count == len(offsets) - 1
    assert result.last_event is not None and result.last_event.seq == result.count - 1
    assert "runs past the record" in result.error
    list(EventLogReader(path).iter_events())      # nor here


def test_an_undecodable_payload_is_reported_by_scan_for_every_edit_kind(tmp_path: Path) -> None:
    for op in (insert(0, "abc"), delete(0, "abc"), replace(0, "abc", "xyz")):
        target = tmp_path / "one.log"
        target.unlink(missing_ok=True)
        writer = EventLogWriter(target)
        writer.append(EventKind.SESSION_START, 0, encode_session_start(1, WALL0, MONO0, "t"))
        kind, payload = encode_op(insert(0, "abc"))
        writer.append(kind, 1000, payload)
        kind, payload = encode_op(op)
        offset = writer.append(kind, 1000, payload)
        writer.close()

        blob = bytearray(target.read_bytes())
        payload_start, _ = _payload_bounds(blob, offset)
        blob[payload_start + 1] = 0x7F
        _repair_checksum(blob, offset)
        target.write_bytes(blob)

        result = EventLogReader(target).scan()
        assert result.tail_ok is False, op
        assert result.good_length == offset, op


# --- record size limit ------------------------------------------------------


def test_build_record_refuses_a_body_past_the_reader_s_limit() -> None:
    from scratchpad.core.eventlog import MAX_PAYLOAD, MAX_RECORD_BODY, check_payload_size

    payload = b"\x00" * (MAX_RECORD_BODY + 1)
    with pytest.raises(RecordTooLarge):
        build_record(EventKind.INSERT, 1000, payload)
    with pytest.raises(RecordTooLarge):
        check_payload_size(EventKind.INSERT, payload)
    # A RecordTooLarge is an OpError, so callers that already handle "this
    # operation cannot be applied" handle it too.
    assert issubclass(RecordTooLarge, OpError)
    # The documented limit is honoured exactly, both ways.
    ok = b"\x00" * MAX_PAYLOAD
    check_payload_size(EventKind.INSERT, ok)
    assert len(build_record(EventKind.INSERT, 1000, ok)) <= MAX_RECORD_BODY + 6
    with pytest.raises(RecordTooLarge):
        check_payload_size(EventKind.INSERT, ok + b"\x00")


def test_a_too_large_record_is_refused_before_anything_is_written(tmp_path: Path) -> None:
    from scratchpad.core.eventlog import MAX_RECORD_BODY

    path = tmp_path / "events.log"
    writer = EventLogWriter(path)
    writer.append(EventKind.SESSION_START, 0, encode_session_start(1, WALL0, MONO0, "t"))
    size = writer.size
    with pytest.raises(RecordTooLarge):
        writer.append(EventKind.INSERT, 1000, b"\x00" * (MAX_RECORD_BODY + 1))
    assert writer.size == size
    writer.close()
    assert path.stat().st_size == size
    assert EventLogReader(path).scan().tail_ok is True


# --- failing writes ---------------------------------------------------------


class _FlakyWrite:
    """``os.write`` that fails once on one file descriptor."""

    def __init__(self, fd: int, *, short: int | None = None) -> None:
        self.fd = fd
        self.short = short
        self.armed = True
        self.real = os.write
        self.short_done = False

    def __call__(self, fd: int, data):  # noqa: ANN001 - mirrors os.write
        if fd != self.fd or not self.armed:
            return self.real(fd, data)
        if self.short is not None and not self.short_done:
            self.short_done = True
            return self.real(fd, bytes(data)[: self.short])
        self.armed = False
        raise OSError(errno.ENOSPC, "No space left on device")


def test_a_failed_append_leaves_no_trace_of_the_record(tmp_path: Path, monkeypatch) -> None:
    """append() is all or nothing: a failed record is not written *later*."""
    path = tmp_path / "events.log"
    writer = EventLogWriter(path)
    writer.append(EventKind.SESSION_START, 0, encode_session_start(1, WALL0, MONO0, "t"))
    before_size = writer.size
    before_count = EventLogReader(path).scan().count

    kind, payload = encode_op(insert(0, "ouch"))
    monkeypatch.setattr(os, "write", _FlakyWrite(writer.fileno()))
    with pytest.raises(OSError):
        writer.append(kind, 1000, payload)
    monkeypatch.undo()

    assert writer.size == before_size
    assert path.stat().st_size == before_size
    assert writer.needs_truncate is False

    kind, payload = encode_op(insert(0, "ok"))
    writer.append(kind, 1000, payload)
    writer.close()

    events = list(EventLogReader(path).iter_events())
    assert [event.op for event in events[1:]] == [insert(0, "ok")]
    assert EventLogReader(path).scan().count == before_count + 1


def test_a_half_written_record_is_truncated_away_not_written_twice(tmp_path: Path, monkeypatch) -> None:
    """A short write followed by an error must not duplicate the record bytes."""
    path = tmp_path / "events.log"
    writer = EventLogWriter(path)
    writer.append(EventKind.SESSION_START, 0, encode_session_start(1, WALL0, MONO0, "t"))
    good_length = writer.size

    kind, payload = encode_op(insert(0, "torn"))
    monkeypatch.setattr(os, "write", _FlakyWrite(writer.fileno(), short=3))
    with pytest.raises(OSError):
        writer.append(kind, 1000, payload)
    monkeypatch.undo()

    assert writer.size == good_length
    assert path.stat().st_size == good_length, "the torn bytes must be gone"
    assert writer.needs_truncate is False

    kind, payload = encode_op(insert(0, "again"))
    writer.append(kind, 1000, payload)
    writer.close()

    result = EventLogReader(path).scan()
    assert result.tail_ok is True
    assert result.count == 2
    assert result.last_event is not None and result.last_event.op == insert(0, "again")


def test_a_partial_flush_writes_the_rest_exactly_once(tmp_path: Path, monkeypatch) -> None:
    """The buffered writer retries only what the kernel did not take."""
    path = tmp_path / "events.log"
    writer = EventLogWriter(path, buffer_bytes=1 << 16)
    writer.append(EventKind.SESSION_START, 0, encode_session_start(1, WALL0, MONO0, "t"))
    for index in range(5):
        kind, payload = encode_op(insert(index, "x"))
        writer.append(kind, 1000, payload)
    expected = writer.size

    flaky = _FlakyWrite(writer.fileno(), short=4)
    monkeypatch.setattr(os, "write", flaky)
    with pytest.raises(OSError):
        writer.flush()
    monkeypatch.undo()
    assert path.stat().st_size == HEADER_SIZE + 4

    writer.flush(fsync=True)
    assert path.stat().st_size == expected
    writer.close()

    result = EventLogReader(path).scan()
    assert result.tail_ok is True
    assert result.count == 6
