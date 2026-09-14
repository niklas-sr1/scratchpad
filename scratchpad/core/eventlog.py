"""The append-only binary event log.

File layout
-----------

::

    magic "SCRPLOG1"  8 bytes
    format version    u16 little endian
    reserved          u16 little endian
    record*                                       (header is 12 bytes)

Record framing (every integer is an unsigned LEB128 varint unless stated)::

    [len]        length of the remainder of this record (kind..payload)
    [kind]       EventKind
    [mono_delta] nanoseconds since the previous record in the file
                 (0 for the first record and for SESSION_START, whose payload
                  carries absolute times)
    [payload]    kind specific
    [crc]        checksum of (kind..payload), little endian

Payloads::

    SESSION_START  session_id, wall_ns, mono_ns, app_version (len-prefixed utf8)
    SESSION_STOP   -
    HEARTBEAT      -
    WALL_ANCHOR    wall_ns
    INSERT         pos, text (len-prefixed utf8)
    DELETE         pos, old_text
    REPLACE        pos, old_text, text

Checksum width, and what a keystroke costs
------------------------------------------

The contract asks for a single ASCII keystroke to stay under 12 bytes on disk
and allows trading checksum width for it.  A 4-byte CRC32 on every record makes
that impossible (see the table below), so:

* SESSION_START, SESSION_STOP, HEARTBEAT and WALL_ANCHOR keep the full 4-byte
  CRC32.  They are rare, they anchor every timestamp in the file, and they are
  the records whose corruption would misplace whole sessions on the timeline.
  Checkpoint files keep their CRC32 as well.
* INSERT, DELETE and REPLACE carry a 1-byte checksum: the low byte of the same
  CRC32 (``zlib.crc32(body) & 0xFF``).  One algorithm for the whole format, and
  a C implementation instead of a Python CRC8 table, which matters when opening
  a log means checksumming a few hundred thousand records.

A corrupt edit record therefore has a 1/256 chance of passing its checksum --
but it still has to pass the length framing, the varint canonicality checks, the
UTF-8 decode and the replay validation in :func:`scratchpad.core.ops.apply_op`,
which is what actually catches a damaged log in practice.

Final per-record cost, one ASCII character typed.  The record is
``len`` 1 + ``kind`` 1 + ``mono_delta`` D + ``pos`` P + ``textlen`` 1 +
``text`` 1 + ``crc`` 1, so **5 + D + P bytes**, where D is 4 for a keystroke
within 268 ms of the previous record and 5 up to 34 s, and P is 1 below
offset 128, 2 below 16 KiB and 3 below 2 MiB:

======================== =============== =========== =========
document size            keystroke pause D + P       bytes
======================== =============== =========== =========
< 128 chars              < 268 ms        4 + 1       **10**
< 16 KiB                 < 268 ms        4 + 2       **11**
< 16 KiB                 268 ms .. 34 s  5 + 2       **12**
< 2 MiB                  < 268 ms        4 + 3       **12**
< 2 MiB                  268 ms .. 34 s  5 + 3       **13**
======================== =============== =========== =========

So 10 to 13 bytes, 11 for the ordinary case of typing into a document of a few
kilobytes, against 13 to 16 with a CRC32 on every record.  The contract's "under
12 bytes" target is met for documents below 16 KiB, which is what a scratchpad
normally holds; beyond that the ``pos`` varint grows and costs one more byte.

The remaining cost driver is not the checksum but the nanosecond resolution of
``mono_delta``: a 268 ms gap already needs four varint bytes and a 1.07 s gap
needs five, so a typist's pauses cost more than the character does.  Storing the
delta in microseconds would save two bytes per keystroke (a keystroke would be
8 to 10 bytes) at a resolution no timeline can tell apart -- but the unit is
fixed by the contract, so it stays nanoseconds.

Lifecycle records cost 38 bytes (SESSION_START), 11 (SESSION_STOP), 11
(HEARTBEAT) and 17 (WALL_ANCHOR).  At the default 30 s heartbeat that is 56
bytes per minute of idle uptime, ~79 KiB per idle day.

Torn tails
----------

:meth:`EventLogReader.scan` stops at the first record whose length, kind,
checksum or payload does not decode and reports ``tail_ok=False`` together with
``good_length``, the byte offset just past the last good record.  The writer
truncates to that offset before appending.  Good records are never rewritten.

Reader and writer agree on exactly what "does not decode" means: both walk the
file through :func:`_iter_raw`, which decodes the edit payload as part of
verifying the record.  A one-byte checksum lets about one corrupted edit record
in 256 through, and without decoding its payload the damage used to be invisible
to :meth:`EventLogReader.scan` (``tail_ok=True``) while
:meth:`EventLogReader.iter_events` quietly stopped there -- a truncated document
and a wrong sequence number, with no warning anywhere.

Record size limit
-----------------

A record body may not exceed :data:`MAX_RECORD_BODY` (256 MiB), which is what
the reader treats as corruption; :func:`build_record` and
:func:`check_payload_size` raise :class:`RecordTooLarge` instead of writing a
record that the next open would throw away together with everything behind it.
For an INSERT that bounds the pasted text at slightly under 256 MiB of UTF-8.
"""

from __future__ import annotations

import errno
import logging
import os
import zlib
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Iterator, NamedTuple

from scratchpad.core.ops import Op, OpError, OpKind
from scratchpad.core.varint import (
    MAX_VARINT_BYTES,
    VarintError,
    append_uvarint,
    decode_uvarint,
    varint_size,
)

log = logging.getLogger(__name__)

MAGIC = b"SCRPLOG1"
FORMAT_VERSION = 1
HEADER_SIZE = 12
#: Sanity bound on a record body; anything larger is treated as corruption by
#: the reader, so :func:`build_record` refuses to write it in the first place.
#: 256 MiB of UTF-8 in a single operation -- a paste no scratchpad wants.
MAX_RECORD_BODY = 1 << 28
#: Largest payload that is guaranteed to fit into a record, whatever the kind
#: and the monotonic delta in front of it cost.
MAX_PAYLOAD = MAX_RECORD_BODY - MAX_VARINT_BYTES - 1
#: A pause this long between two records inside one session is not typing: it is
#: a suspend, a hibernation or a machine too busy to write a heartbeat.  The
#: scan collects such gaps so that :class:`~scratchpad.core.history.History` can
#: split the session into segments (it applies its own, larger, threshold).
MIN_SESSION_GAP_NS = 120 * 1_000_000_000


class EventKind(IntEnum):
    """Kind of a log record."""

    SESSION_START = 1
    SESSION_STOP = 2
    HEARTBEAT = 3
    WALL_ANCHOR = 4
    INSERT = 5
    DELETE = 6
    REPLACE = 7


#: Records that change the document.
EDIT_KINDS = frozenset({EventKind.INSERT, EventKind.DELETE, EventKind.REPLACE})
#: Records that keep the full 4-byte CRC32.
FULL_CRC_KINDS = frozenset(
    {EventKind.SESSION_START, EventKind.SESSION_STOP, EventKind.HEARTBEAT, EventKind.WALL_ANCHOR}
)
_KNOWN_KINDS = frozenset(int(k) for k in EventKind)
_OP_KIND_OF = {
    EventKind.INSERT: OpKind.INSERT,
    EventKind.DELETE: OpKind.DELETE,
    EventKind.REPLACE: OpKind.REPLACE,
}
_EVENT_KIND_OF = {
    OpKind.INSERT: EventKind.INSERT,
    OpKind.DELETE: EventKind.DELETE,
    OpKind.REPLACE: EventKind.REPLACE,
}


class LogFormatError(Exception):
    """The file is not a scratchpad event log (bad magic or unknown version)."""


class RecordTooLarge(OpError):
    """A record would exceed :data:`MAX_RECORD_BODY` and must not be written.

    The reader treats any body larger than that as corruption, so writing one
    would append data that the next open throws away -- together with
    everything appended after it.  Raised by :func:`build_record` and, before
    anything at all has happened, by
    :meth:`scratchpad.core.store.ScratchpadStore.apply`, where the UI can catch
    it (it is an :class:`~scratchpad.core.ops.OpError`) and tell the user that
    the paste is too large to keep in the document.
    """


def crc_size(kind: int) -> int:
    """Checksum width in bytes for ``kind`` (4 for lifecycle records, else 1)."""
    return 4 if kind in FULL_CRC_KINDS else 1


# --- dataclasses ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Event:
    """One decoded record."""

    seq: int
    kind: EventKind
    mono_ns: int
    wall_ns: int
    session_id: int
    op: Op | None
    offset: int

    @property
    def is_edit(self) -> bool:
        return self.op is not None


@dataclass(frozen=True, slots=True)
class LogPosition:
    """Everything needed to resume decoding at ``offset``.

    ``seq`` is the sequence number of the record starting at ``offset``; the
    remaining fields describe the decoder state produced by all records before
    it.  Index entries store one of these, which is what makes "jump into the
    middle of the log and keep going" cheap.
    """

    offset: int = HEADER_SIZE
    seq: int = 0
    session_id: int = 0
    anchor_wall_ns: int = 0
    anchor_mono_ns: int = 0
    prev_mono_ns: int = 0
    last_wall_ns: int = 0


@dataclass(frozen=True, slots=True)
class IndexEntry:
    """A sampled point in the log: where it is, when it is, how to resume."""

    offset: int
    seq: int
    wall_ns: int
    state: LogPosition


class SessionSpan(NamedTuple):
    """A run of records between two SESSION_START records."""

    session_id: int
    start_wall_ns: int
    end_wall_ns: int
    clean_stop: bool
    first_seq: int
    last_seq: int


@dataclass(frozen=True, slots=True)
class ScanResult:
    """Outcome of a full pass over the log."""

    tail_ok: bool
    good_length: int
    count: int
    last_event: Event | None
    error: str = ""
    index: tuple[IndexEntry, ...] = ()
    sessions: tuple[SessionSpan, ...] = ()
    end_state: LogPosition = LogPosition()
    first_wall_ns: int = 0
    last_wall_ns: int = 0
    activity: tuple[tuple[int, int], ...] = ()
    """Edit counts per whole second, ``(second_since_epoch, count)``, ascending."""
    gaps: tuple[tuple[int, int, int, int], ...] = ()
    """Pauses of at least :data:`MIN_SESSION_GAP_NS` between two consecutive
    records of one session, as ``(seq_before, wall_before, seq_after,
    wall_after)``.  History turns the long ones into session segments."""


# --- encoding ---------------------------------------------------------------


def file_header() -> bytes:
    """The 12-byte file header."""
    return MAGIC + FORMAT_VERSION.to_bytes(2, "little") + b"\x00\x00"


def _put_str(out: bytearray, value: str) -> None:
    raw = value.encode("utf-8")
    append_uvarint(out, len(raw))
    out += raw


def encode_session_start(session_id: int, wall_ns: int, mono_ns: int, app_version: str) -> bytes:
    """SESSION_START payload."""
    out = bytearray()
    append_uvarint(out, session_id)
    append_uvarint(out, wall_ns)
    append_uvarint(out, mono_ns)
    _put_str(out, app_version)
    return bytes(out)


def encode_wall_anchor(wall_ns: int) -> bytes:
    """WALL_ANCHOR payload."""
    out = bytearray()
    append_uvarint(out, wall_ns)
    return bytes(out)


def encode_op(op: Op) -> tuple[EventKind, bytes]:
    """Record kind and payload for a document operation."""
    out = bytearray()
    append_uvarint(out, op.pos)
    if op.kind is OpKind.INSERT:
        _put_str(out, op.text)
    elif op.kind is OpKind.DELETE:
        _put_str(out, op.old_text)
    elif op.kind is OpKind.REPLACE:
        _put_str(out, op.old_text)
        _put_str(out, op.text)
    else:  # pragma: no cover - OpKind is exhaustive
        raise ValueError(f"unknown op kind {op.kind!r}")
    return _EVENT_KIND_OF[op.kind], bytes(out)


def check_payload_size(kind: int, payload: bytes) -> None:
    """Raise :class:`RecordTooLarge` if ``payload`` can never be framed.

    Uses the worst case width of the monotonic delta so that the answer does
    not depend on *when* the record would be written; a caller can therefore
    validate a pending operation before touching any state.
    """
    body_len = varint_size(int(kind)) + MAX_VARINT_BYTES + len(payload)
    if body_len > MAX_RECORD_BODY:
        raise RecordTooLarge(
            f"record payload of {len(payload)} bytes does not fit the "
            f"{MAX_RECORD_BODY}-byte record limit"
        )


def build_record(kind: int, mono_delta: int, payload: bytes) -> bytes:
    """Frame one record, checksum included.

    Raises :class:`RecordTooLarge` when the body would pass
    :data:`MAX_RECORD_BODY`, which is what the reader rejects as corruption.
    """
    if mono_delta < 0:
        raise ValueError(f"negative mono_delta {mono_delta}")
    body_len = varint_size(int(kind)) + varint_size(mono_delta) + len(payload)
    if body_len > MAX_RECORD_BODY:
        raise RecordTooLarge(
            f"record body of {body_len} bytes exceeds the "
            f"{MAX_RECORD_BODY}-byte record limit"
        )
    body = bytearray()
    append_uvarint(body, int(kind))
    append_uvarint(body, mono_delta)
    body += payload
    out = bytearray()
    append_uvarint(out, len(body))
    out += body
    crc = zlib.crc32(body) & 0xFFFFFFFF
    if kind in FULL_CRC_KINDS:
        out += crc.to_bytes(4, "little")
    else:
        out.append(crc & 0xFF)
    return bytes(out)


def record_size(kind: int, mono_delta: int, payload: bytes) -> int:
    """Size on disk of the record :func:`build_record` would produce."""
    return len(build_record(kind, mono_delta, payload))


# --- decoding helpers -------------------------------------------------------


def _get_str(buf: bytearray | bytes, offset: int, limit: int) -> tuple[str, int]:
    length, offset = decode_uvarint(buf, offset)
    end = offset + length
    if end > limit:
        raise VarintError(f"string at {offset} runs past the record")
    return bytes(buf[offset:end]).decode("utf-8"), end


def decode_session_start(buf: bytearray | bytes, offset: int, limit: int) -> tuple[int, int, int, str]:
    """Decode a SESSION_START payload: ``(session_id, wall_ns, mono_ns, app_version)``."""
    session_id, offset = decode_uvarint(buf, offset)
    wall_ns, offset = decode_uvarint(buf, offset)
    mono_ns, offset = decode_uvarint(buf, offset)
    app_version, offset = _get_str(buf, offset, limit)
    return session_id, wall_ns, mono_ns, app_version


def decode_op(kind: int, buf: bytearray | bytes, offset: int, limit: int) -> Op:
    """Decode an INSERT/DELETE/REPLACE payload."""
    pos, offset = decode_uvarint(buf, offset)
    if kind == EventKind.INSERT:
        text, offset = _get_str(buf, offset, limit)
        return Op(OpKind.INSERT, pos, text=text)
    if kind == EventKind.DELETE:
        old_text, offset = _get_str(buf, offset, limit)
        return Op(OpKind.DELETE, pos, old_text=old_text)
    old_text, offset = _get_str(buf, offset, limit)
    text, offset = _get_str(buf, offset, limit)
    return Op(OpKind.REPLACE, pos, text=text, old_text=old_text)


def check_op_payload(kind: int, buf: bytearray | bytes, offset: int, limit: int) -> None:
    """Verify an INSERT/DELETE/REPLACE payload without building the :class:`Op`.

    Raises exactly what :func:`decode_op` would raise, which is what lets
    :meth:`EventLogReader.scan` agree with
    :meth:`EventLogReader.iter_events` about which records are damaged while
    paying only for the checking, not for the objects.
    """
    _pos, offset = decode_uvarint(buf, offset)
    _text, offset = _get_str(buf, offset, limit)
    if kind == EventKind.REPLACE:
        _text, offset = _get_str(buf, offset, limit)


class _Cursor:
    """Mutable decoder state, shared with :func:`_iter_raw`.

    Kept as a plain slotted object rather than a frozen dataclass because it is
    updated once per record; allocating an immutable state object per record
    dominates the cost of opening a large log.
    """

    __slots__ = (
        "offset", "seq", "session_id", "anchor_wall_ns", "anchor_mono_ns",
        "prev_mono_ns", "last_wall_ns", "tail_ok", "error",
    )

    def __init__(self, state: LogPosition | None = None) -> None:
        state = state or LogPosition()
        self.offset = state.offset
        self.seq = state.seq
        self.session_id = state.session_id
        self.anchor_wall_ns = state.anchor_wall_ns
        self.anchor_mono_ns = state.anchor_mono_ns
        self.prev_mono_ns = state.prev_mono_ns
        self.last_wall_ns = state.last_wall_ns
        self.tail_ok = True
        self.error = ""

    def position(self) -> LogPosition:
        """Snapshot, usable as a resume point."""
        return LogPosition(
            self.offset, self.seq, self.session_id, self.anchor_wall_ns,
            self.anchor_mono_ns, self.prev_mono_ns, self.last_wall_ns,
        )


#: What one record decodes to: ``(offset, seq, kind, mono_ns, wall_ns,
#: session_id, payload_start, payload_end, op)``.
RawRecord = tuple[int, int, int, int, int, int, int, int, "Op | None"]


def _iter_raw(buf: bytearray | bytes, cursor: _Cursor, want_ops: bool = True) -> Iterator[RawRecord]:
    """Walk records, yielding :data:`RawRecord` tuples.

    ``cursor`` is updated to the state *after* the yielded record before the
    yield happens, so a consumer can snapshot a resume point at any time.  On a
    damaged record the iterator sets ``cursor.tail_ok`` / ``cursor.error``,
    leaves ``cursor.offset`` at the start of that record and stops.

    Edit payloads are verified here, inside the same guard as the framing.  A
    checksum is only one byte wide on edit records, so roughly one corrupted
    record in 256 passes it; catching the undecodable payload here is what turns
    such a record into an ordinary torn tail (reported by :meth:`scan`, verified
    and truncated at open) instead of a silent early stop that would drop every
    record behind it from the document.  ``want_ops=False`` verifies the payload
    without building the :class:`Op`, which is what :meth:`scan` wants: for a
    200k event log that is a fifth of the opening cost.
    """
    size = len(buf)
    off = cursor.offset
    seq = cursor.seq
    session_id = cursor.session_id
    anchor_wall = cursor.anchor_wall_ns
    anchor_mono = cursor.anchor_mono_ns
    prev_mono = cursor.prev_mono_ns
    last_wall = cursor.last_wall_ns
    crc32 = zlib.crc32

    while off < size:
        try:
            body_len, body_start = decode_uvarint(buf, off)
            if body_len == 0 or body_len > MAX_RECORD_BODY:
                raise VarintError(f"implausible record length {body_len}")
            body_end = body_start + body_len
            kind, after_kind = decode_uvarint(buf, body_start)
            if kind not in _KNOWN_KINDS:
                raise VarintError(f"unknown record kind {kind}")
            width = 4 if kind in FULL_CRC_KINDS else 1
            if body_end + width > size:
                raise VarintError("record truncated")
            crc = crc32(buf[body_start:body_end]) & 0xFFFFFFFF
            if width == 4:
                stored = int.from_bytes(buf[body_end:body_end + 4], "little")
            else:
                stored = buf[body_end]
                crc &= 0xFF
            if stored != crc:
                raise VarintError("checksum mismatch")
            mono_delta, payload_start = decode_uvarint(buf, after_kind)
            if payload_start > body_end:
                raise VarintError("record header runs past the record")

            op = None
            if kind == EventKind.SESSION_START:
                session_id, raw_wall, mono_ns, _ = decode_session_start(buf, payload_start, body_end)
                anchor_wall = raw_wall
                anchor_mono = mono_ns
            else:
                mono_ns = prev_mono + mono_delta
                if kind == EventKind.WALL_ANCHOR:
                    raw_wall, _ = decode_uvarint(buf, payload_start)
                    anchor_wall = raw_wall
                    anchor_mono = mono_ns
                else:
                    raw_wall = anchor_wall + (mono_ns - anchor_mono)
                    if kind in EDIT_KINDS:
                        if want_ops:
                            op = decode_op(kind, buf, payload_start, body_end)
                        else:
                            check_op_payload(kind, buf, payload_start, body_end)
        except (VarintError, UnicodeDecodeError, IndexError, OpError) as exc:
            cursor.offset = off
            cursor.tail_ok = False
            cursor.error = f"record at offset {off}: {exc}"
            return

        wall_ns = raw_wall if raw_wall >= last_wall else last_wall
        prev_mono = mono_ns
        last_wall = wall_ns
        next_off = body_end + width

        cursor.offset = next_off
        cursor.seq = seq + 1
        cursor.session_id = session_id
        cursor.anchor_wall_ns = anchor_wall
        cursor.anchor_mono_ns = anchor_mono
        cursor.prev_mono_ns = prev_mono
        cursor.last_wall_ns = last_wall

        yield (off, seq, kind, mono_ns, wall_ns, session_id, payload_start, body_end, op)

        seq += 1
        off = next_off


# --- reader -----------------------------------------------------------------


class EventLogReader:
    """Reads events out of the log file.

    The file content is cached in memory and refreshed incrementally (only the
    bytes appended since the last look are read), so a live store can keep
    reconstructing history while the writer appends.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._buf = bytearray()
        self._loaded = 0

    # -- buffer management --

    def data(self) -> bytearray:
        """The file content, refreshed from disk if it grew or shrank."""
        try:
            size = os.path.getsize(self.path)
        except FileNotFoundError:
            self._buf = bytearray()
            self._loaded = 0
            return self._buf
        if size < self._loaded:
            del self._buf[size:]
            self._loaded = size
        elif size > self._loaded:
            with open(self.path, "rb") as handle:
                handle.seek(self._loaded)
                self._buf += handle.read()
            self._loaded = len(self._buf)
        return self._buf

    def reset(self) -> None:
        """Forget everything; the file underneath was replaced (history rewrite)."""
        self._buf = bytearray()
        self._loaded = 0

    def truncate_cache(self, length: int) -> None:
        """Forget everything past ``length`` (after the writer truncated)."""
        if length < self._loaded:
            del self._buf[length:]
            self._loaded = length

    def check_header(self) -> None:
        """Raise :class:`LogFormatError` unless the file starts with our header."""
        buf = self.data()
        if len(buf) < HEADER_SIZE:
            raise LogFormatError(f"{self.path}: file shorter than the {HEADER_SIZE}-byte header")
        if bytes(buf[:8]) != MAGIC:
            raise LogFormatError(f"{self.path}: not a scratchpad event log")
        version = int.from_bytes(buf[8:10], "little")
        if version != FORMAT_VERSION:
            raise LogFormatError(f"{self.path}: unsupported format version {version}")

    # -- iteration --

    def iter_events(
        self,
        start_offset: int = 0,
        *,
        state: LogPosition | None = None,
        stop_seq: int | None = None,
    ) -> Iterator[Event]:
        """Yield events starting at ``start_offset`` (0 means "from the top").

        ``state`` resumes from an :class:`IndexEntry`; without it the offset must
        be the start of the records.  ``stop_seq`` stops after that sequence
        number (inclusive).  Iteration stops at a damaged record with a warning
        -- :meth:`scan` is the authority on tail damage and sees exactly the
        same damage, because both walk the log through the same decoder.
        """
        buf = self.data()
        if not buf:
            return
        if state is None:
            state = LogPosition(offset=max(start_offset, HEADER_SIZE), seq=0)
        elif start_offset and start_offset != state.offset:
            raise ValueError("start_offset and state.offset disagree")
        cursor = _Cursor(state)
        for offset, seq, kind, mono_ns, wall_ns, session_id, _start, _end, op in _iter_raw(buf, cursor):
            yield Event(seq, EventKind(kind), mono_ns, wall_ns, session_id, op, offset)
            if stop_seq is not None and seq >= stop_seq:
                return
        if not cursor.tail_ok:
            log.warning(
                "event log %s: stopping at a damaged record (%s); everything "
                "behind it is ignored until the tail is truncated",
                self.path, cursor.error,
            )

    def scan(self, *, index_stride: int = 0) -> ScanResult:
        """Walk the whole log once.

        Verifies every record (framing, checksum *and* payload), counts them,
        derives session spans, notes the long pauses inside a session,
        accumulates a one-second activity histogram and (when ``index_stride``
        is positive) samples an index every ``index_stride`` events.
        """
        buf = self.data()
        if len(buf) < HEADER_SIZE:
            if len(buf) == 0:
                return ScanResult(True, 0, 0, None, end_state=LogPosition())
            return ScanResult(False, 0, 0, None, error="truncated file header")
        self.check_header()

        cursor = _Cursor(LogPosition())
        index: list[IndexEntry] = []
        sessions: list[SessionSpan] = []
        activity: dict[int, int] = {}
        stride = index_stride if index_stride > 0 else 0

        count = 0
        first_wall = 0
        last_wall = 0
        last_record: RawRecord | None = None
        gaps: list[tuple[int, int, int, int]] = []
        prev_seq = -1
        prev_wall = 0

        # Session accumulator, flushed on every SESSION_START and at the end.
        cur_id = 0
        cur_start_wall = 0
        cur_first_seq = 0
        cur_last_seq = -1
        cur_last_wall = 0
        cur_clean = False
        have_session = False

        # Index sampling: the resume state for seq N is the decoder state after
        # record N-1, so it is snapshotted one record early and emitted when the
        # record it points at is reached (which is where its wall time is known).
        pending_state: LogPosition | None = LogPosition() if stride else None

        for record in _iter_raw(buf, cursor, want_ops=False):
            offset, seq, kind, mono_ns, wall_ns, session_id = record[:6]
            if (
                kind != EventKind.SESSION_START
                and prev_seq >= 0
                and wall_ns - prev_wall >= MIN_SESSION_GAP_NS
            ):
                gaps.append((prev_seq, prev_wall, seq, wall_ns))
            prev_seq = seq
            prev_wall = wall_ns
            if pending_state is not None:
                index.append(IndexEntry(pending_state.offset, pending_state.seq, wall_ns, pending_state))
                pending_state = None

            if kind == EventKind.SESSION_START:
                if have_session:
                    sessions.append(
                        SessionSpan(cur_id, cur_start_wall, cur_last_wall, cur_clean,
                                    cur_first_seq, cur_last_seq)
                    )
                have_session = True
                cur_id = session_id
                cur_start_wall = wall_ns
                cur_first_seq = seq
                cur_clean = False
            elif kind == EventKind.SESSION_STOP:
                cur_clean = True
            elif kind >= EventKind.INSERT:
                second = wall_ns // 1_000_000_000
                activity[second] = activity.get(second, 0) + 1

            cur_last_seq = seq
            cur_last_wall = wall_ns
            if count == 0:
                first_wall = wall_ns
            last_wall = wall_ns
            count += 1
            last_record = record

            if stride and (seq + 1) % stride == 0:
                pending_state = cursor.position()

        if have_session:
            sessions.append(
                SessionSpan(cur_id, cur_start_wall, cur_last_wall, cur_clean, cur_first_seq, cur_last_seq)
            )

        tail_ok = cursor.tail_ok
        good_length = cursor.offset
        error = cursor.error
        last_event = None
        if last_record is not None:
            try:
                last_event = self._event_from_raw(buf, last_record)
            except (VarintError, UnicodeDecodeError, IndexError, OpError) as exc:
                # Belt and braces: _iter_raw already verified this payload, so
                # this cannot normally happen -- but scan() must never raise on
                # a damaged file, so the record becomes tail damage like any
                # other, and the store truncates it away at open.
                offset = last_record[0]
                log.warning("event log %s: record at offset %d: %s", self.path, offset, exc)
                tail_ok = False
                good_length = offset
                error = f"record at offset {offset}: {exc}"
                count -= 1

        return ScanResult(
            tail_ok=tail_ok,
            good_length=good_length,
            count=count,
            last_event=last_event,
            error=error,
            index=tuple(index),
            sessions=tuple(sessions),
            end_state=cursor.position(),
            first_wall_ns=first_wall,
            last_wall_ns=last_wall,
            activity=tuple(sorted(activity.items())),
            gaps=tuple(gaps),
        )

    @staticmethod
    def _event_from_raw(buf: bytearray, record: RawRecord) -> Event:
        """Build an :class:`Event` from one raw record tuple.

        Decodes the payload when the walk did not (``want_ops=False``); the
        caller guards the call, because a decode is the one thing here that can
        fail on a corrupt file.
        """
        offset, seq, kind, mono_ns, wall_ns, session_id, pay_start, pay_end, op = record
        if op is None and kind in EDIT_KINDS:
            op = decode_op(kind, buf, pay_start, pay_end)
        return Event(seq, EventKind(kind), mono_ns, wall_ns, session_id, op, offset)


# --- writer -----------------------------------------------------------------


def ensure_log(path: Path) -> int:
    """Make sure ``path`` exists and carries a valid header.  Returns its size.

    An empty file gets the header.  A file shorter than a header can only be a
    crash during creation (no record can have been written yet), so it is reset.
    """
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        size = os.fstat(fd).st_size
        if size == 0:
            os.write(fd, file_header())
            os.fsync(fd)
            return HEADER_SIZE
        head = os.pread(fd, HEADER_SIZE, 0)
        if len(head) < HEADER_SIZE:
            log.warning("event log %s: torn header (%d bytes); recreating", path, len(head))
            os.ftruncate(fd, 0)
            os.write(fd, file_header())
            os.fsync(fd)
            return HEADER_SIZE
        if head[:8] != MAGIC:
            raise LogFormatError(f"{path}: not a scratchpad event log")
        version = int.from_bytes(head[8:10], "little")
        if version != FORMAT_VERSION:
            raise LogFormatError(f"{path}: unsupported format version {version}")
        return size
    finally:
        os.close(fd)


class TornWriteError(OSError):
    """A record was half written and the file could not be repaired.

    The writer refuses to append after this (appending would leave the torn
    bytes in the middle of the file, where the next reader would cut the log);
    :meth:`EventLogWriter.truncate` to :attr:`EventLogWriter.size` clears it.
    """


class EventLogWriter:
    """Appends records.

    Every :meth:`append` hands the bytes to the OS immediately (``os.write``),
    so an application crash loses nothing that has already been appended.
    ``fsync`` is a separate, batched decision made by the caller (the store runs
    it on a timer), because that is the only expensive part.

    ``buffer_bytes`` (default 0, meaning write-through) exists for bulk writers
    -- history rewrites and test fixtures -- that are going to fsync at the end
    anyway and would otherwise pay one syscall per keystroke.

    Failure model
    -------------

    :meth:`append` is all or nothing.  When the write fails (a full disk is the
    realistic case) and no byte of the record reached the kernel, the record is
    dropped again and :attr:`size` rolls back, so the caller's exception
    handler sees a writer that never heard of the record -- the store can keep
    its sequence number, its index and its text exactly as they were.  When a
    *part* of the record did reach the kernel the file is torn, so the writer
    truncates it back to where the record began before re-raising; if even that
    fails it marks itself (:attr:`needs_truncate`) and refuses to append until
    the caller has truncated.

    Either way the record is never left behind in the buffer to be written by
    the *next* append, which used to hand the store a log holding an operation
    it had never counted.
    """

    def __init__(self, path: Path | str, *, buffer_bytes: int = 0) -> None:
        self.path = Path(path)
        self._buffer_bytes = max(0, buffer_bytes)
        self._size = ensure_log(self.path)
        self._fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CLOEXEC)
        self._pending = bytearray()
        self._closed = False
        self._needs_truncate = False

    @property
    def size(self) -> int:
        """File length including bytes still buffered in this process."""
        return self._size

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def needs_truncate(self) -> bool:
        """True after a half written record that could not be truncated away."""
        return self._needs_truncate

    def fileno(self) -> int:
        return self._fd

    def append(self, kind: int, mono_delta: int, payload: bytes) -> int:
        """Append one record and return its byte offset.

        Raises :class:`RecordTooLarge` (before anything is written or buffered)
        for a record the reader would reject, and re-raises any ``OSError`` from
        the write after undoing the append.
        """
        if self._closed:
            raise ValueError("writer is closed")
        if self._needs_truncate:
            raise TornWriteError(
                errno.EIO,
                f"{self.path}: a half written record must be truncated at {self._size} first",
            )
        record = build_record(kind, mono_delta, payload)
        offset = self._size
        buffered_before = len(self._pending)
        self._pending += record
        self._size += len(record)
        if len(self._pending) >= self._buffer_bytes:
            try:
                self._write_out()
            except OSError:
                self._undo_append(offset, buffered_before, len(record))
                raise
        return offset

    def _undo_append(self, offset: int, buffered_before: int, record_len: int) -> None:
        """Take the just appended record back out after a failed write."""
        written = buffered_before + record_len - len(self._pending)
        if written <= buffered_before:
            # Nothing of this record reached the kernel: forget it entirely.
            del self._pending[len(self._pending) - record_len:]
            self._size = offset
            return
        # A prefix of the record is on disk; the file is torn at `offset`.
        self._pending.clear()
        try:
            os.ftruncate(self._fd, offset)
        except OSError:
            log.exception("event log %s: cannot truncate a half written record", self.path)
            self._needs_truncate = True
            self._size = offset
            return
        log.warning(
            "event log %s: a record was half written and %d torn byte(s) were truncated away",
            self.path, written - buffered_before,
        )
        self._size = offset

    def _write_out(self) -> None:
        """Hand every buffered byte to the kernel.

        Bytes that were written are removed from the buffer even when a later
        write in the same call fails, so a retry never writes them a second
        time (which would duplicate a record and corrupt the log).
        """
        while self._pending:
            written = os.write(self._fd, self._pending)
            if written <= 0:  # pragma: no cover - os.write raises instead
                raise OSError(errno.EIO, f"{self.path}: write made no progress")
            del self._pending[:written]

    def flush(self, fsync: bool = False) -> None:
        """Push buffered bytes to the OS; optionally make them durable."""
        if self._closed:
            return
        self._write_out()
        if fsync:
            os.fsync(self._fd)

    def truncate(self, length: int) -> None:
        """Drop everything past ``length`` (torn-tail recovery)."""
        if self._closed:
            raise ValueError("writer is closed")
        if length < HEADER_SIZE:
            raise ValueError(f"refusing to truncate below the header ({length})")
        self._pending.clear()
        os.ftruncate(self._fd, length)
        os.fsync(self._fd)
        self._size = length
        self._needs_truncate = False

    def close(self) -> None:
        """Flush, fsync and close.  Idempotent."""
        if self._closed:
            return
        try:
            self._write_out()
            os.fsync(self._fd)
        finally:
            self._closed = True
            os.close(self._fd)

    def __enter__(self) -> "EventLogWriter":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
