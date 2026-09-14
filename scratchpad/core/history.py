"""Sessions, the time index and historical reconstruction.

Opening a log scans it once (payloads are skipped, only framing and checksums
are touched) and keeps three things in memory:

* the session spans -- the "powered on" intervals of the timeline,
* an index sampling ``(wall_ns, seq, offset, decoder state)`` every
  ``index_stride`` events, so any point in the log can be reached without
  reading what comes before it,
* a one-second activity histogram for the timeline's density strip.

Reconstruction of an arbitrary point in time is therefore: pick the newest
checkpoint at or before the target sequence number, jump to the nearest index
entry after it, replay the few hundred events in between.  The checkpoint
*listing* is cached (one ``iterdir`` + ``stat`` per file is not something to
repeat for every reconstruction while the user drags the timeline); the store
invalidates it whenever it writes or prunes one, and :meth:`History.adopt` does
so whenever the underlying files were swapped.

The store feeds live events in through :meth:`History.note_event`, which keeps
all three structures current without re-reading the file.

Sessions versus segments
------------------------

A session is what lies between two SESSION_START records, but a session is not
necessarily one continuous run: suspend the machine for three hours and the log
gets two clusters of records with a three hour hole in between, and nothing was
running in that hole.  :meth:`History.sessions` therefore splits a session
wherever two consecutive records are further apart than
``max(4 * heartbeat_seconds, 120 s)`` -- comfortably more than the heartbeat
interval, so an ordinary idle session with heartbeats is never split.  The parts
share the session id and are numbered in :attr:`Session.segment`; only the last
one can carry ``clean_stop``.
"""

from __future__ import annotations

import logging
from bisect import bisect_right
from dataclasses import dataclass, replace as _replace
from pathlib import Path
from typing import Iterator

from scratchpad.core import checkpoint as checkpoint_mod
from scratchpad.core.eventlog import (
    EDIT_KINDS,
    MIN_SESSION_GAP_NS,
    EventKind,
    EventLogReader,
    Event,
    HEADER_SIZE,
    IndexEntry,
    LogPosition,
    ScanResult,
)
from scratchpad.core.ops import OpError, apply_op, replay_ops

log = logging.getLogger(__name__)

DEFAULT_INDEX_STRIDE = 256
#: Above this many events in a window, :meth:`History.activity` switches from
#: exact counting to the one-second histogram.
EXACT_ACTIVITY_EVENTS = 100_000
#: Default heartbeat interval; the store passes the configured one in.
DEFAULT_HEARTBEAT_SECONDS = 30
#: A session is never split at a pause shorter than this, whatever the
#: configured heartbeat interval is.
MIN_GAP_THRESHOLD_NS = MIN_SESSION_GAP_NS


@dataclass
class Session:
    """One run of the application, as seen on the timeline.

    A session interrupted by a suspend (or by any pause longer than the gap
    threshold) is reported as several :class:`Session` values that share
    ``session_id`` and differ in ``segment``; ``clean_stop`` belongs to the last
    segment only.
    """

    session_id: int
    start_wall_ns: int
    end_wall_ns: int
    clean_stop: bool
    first_seq: int
    last_seq: int
    segment: int = 0

    @property
    def duration_ns(self) -> int:
        return max(0, self.end_wall_ns - self.start_wall_ns)


@dataclass(frozen=True, slots=True)
class ReplayDamage:
    """A record that decodes but does not apply to the document before it.

    Reported by :attr:`History.replay_damage` after a replay had to stop early,
    so that the store can treat the record as a torn tail (truncate there) and
    the application can start at all.
    """

    seq: int
    offset: int
    message: str


class History:
    """Read-only view of the event log plus its checkpoints."""

    def __init__(
        self,
        log_path: Path,
        checkpoints_dir: Path,
        *,
        index_stride: int = DEFAULT_INDEX_STRIDE,
        reader: EventLogReader | None = None,
        heartbeat_seconds: int = DEFAULT_HEARTBEAT_SECONDS,
    ) -> None:
        if index_stride < 1:
            raise ValueError("index_stride must be positive")
        self.log_path = Path(log_path)
        self.checkpoints_dir = Path(checkpoints_dir)
        self.index_stride = index_stride
        self.heartbeat_seconds = max(1, heartbeat_seconds)
        self._reader = reader if reader is not None else EventLogReader(self.log_path)

        self._checkpoint_listing: list[tuple[int, Path]] | None = None
        self._replay_damage: ReplayDamage | None = None
        self._gaps: list[tuple[int, int, int, int]] = []
        self._index: list[IndexEntry] = []
        self._index_seq: list[int] = []
        self._index_wall: list[int] = []
        self._sessions: list[Session] = []
        self._activity: dict[int, int] = {}
        self._activity_seconds: list[int] = []
        self._activity_counts: list[int] = []
        self._activity_dirty = False
        self._count = 0
        self._first_wall_ns = 0
        self._last_wall_ns = 0
        # Running decoder state, mirroring eventlog._Cursor for live appends.
        self._session_id = 0
        self._anchor_wall_ns = 0
        self._anchor_mono_ns = 0
        self._prev_mono_ns = 0
        self._last_wall_state = 0
        self._pending_sample: tuple[int, int, int, int, int] | None = None

    # -- construction --------------------------------------------------------

    @property
    def reader(self) -> EventLogReader:
        return self._reader

    @property
    def gap_threshold_ns(self) -> int:
        """Pause after which a session is split into a new segment."""
        return max(4 * self.heartbeat_seconds * 1_000_000_000, MIN_GAP_THRESHOLD_NS)

    @property
    def replay_damage(self) -> ReplayDamage | None:
        """The record at which the last replay had to stop, if any.

        Set by :meth:`reconstruct_seq` when a record decodes but does not apply;
        cleared by :meth:`adopt`.  The store turns it into a truncation at open.
        """
        return self._replay_damage

    def invalidate_checkpoints(self) -> None:
        """Forget the cached checkpoint listing (one was written or removed)."""
        self._checkpoint_listing = None

    def _checkpoint_listing_cached(self) -> list[tuple[int, Path]]:
        if self._checkpoint_listing is None:
            self._checkpoint_listing = checkpoint_mod.list_checkpoints(self.checkpoints_dir)
        return self._checkpoint_listing

    def rebuild(self) -> ScanResult:
        """Scan the log and (re)build sessions, index and activity."""
        result = self._reader.scan(index_stride=self.index_stride)
        self.adopt(result)
        return result

    def adopt(self, result: ScanResult) -> None:
        """Take over an already computed scan (avoids scanning twice at open)."""
        self._replay_damage = None
        self._checkpoint_listing = None
        self._gaps = list(result.gaps)
        self._index = list(result.index)
        self._index_seq = [entry.seq for entry in self._index]
        self._index_wall = [entry.wall_ns for entry in self._index]
        self._sessions = [Session(*span) for span in result.sessions]
        self._activity = dict(result.activity)
        self._activity_dirty = True
        self._count = result.count
        self._first_wall_ns = result.first_wall_ns
        self._last_wall_ns = result.last_wall_ns

        state = result.end_state
        self._session_id = state.session_id
        self._anchor_wall_ns = state.anchor_wall_ns
        self._anchor_mono_ns = state.anchor_mono_ns
        self._prev_mono_ns = state.prev_mono_ns
        self._last_wall_state = state.last_wall_ns
        self._pending_sample = (
            self._running_state() if self._count % self.index_stride == 0 else None
        )

    def notify_truncated(self, good_length: int) -> None:
        """Tell the reader that the file was truncated to ``good_length``."""
        self._reader.truncate_cache(good_length)

    def _running_state(self) -> tuple[int, int, int, int, int]:
        return (
            self._session_id,
            self._anchor_wall_ns,
            self._anchor_mono_ns,
            self._prev_mono_ns,
            self._last_wall_state,
        )

    def note_event(self, event: Event, *, anchor_wall_ns: int | None = None) -> None:
        """Fold a freshly appended event into the in-memory structures.

        ``anchor_wall_ns`` is the raw wall clock value carried by SESSION_START
        and WALL_ANCHOR payloads; it can differ from ``event.wall_ns``, which is
        clamped to be non-decreasing.  Passing it keeps the live index bit-exact
        with what a rescan of the file would produce.
        """
        if self._pending_sample is not None:
            state = LogPosition(event.offset, event.seq, *self._pending_sample)
            self._index.append(IndexEntry(event.offset, event.seq, event.wall_ns, state))
            self._index_seq.append(event.seq)
            self._index_wall.append(event.wall_ns)
            self._pending_sample = None

        kind = event.kind
        if (
            kind is not EventKind.SESSION_START
            and self._count > 0
            and event.wall_ns - self._last_wall_state >= MIN_SESSION_GAP_NS
        ):
            self._gaps.append(
                (self._count - 1, self._last_wall_state, event.seq, event.wall_ns)
            )
        if kind is EventKind.SESSION_START:
            self._session_id = event.session_id
            self._anchor_wall_ns = anchor_wall_ns if anchor_wall_ns is not None else event.wall_ns
            self._anchor_mono_ns = event.mono_ns
            self._sessions.append(
                Session(event.session_id, event.wall_ns, event.wall_ns, False, event.seq, event.seq)
            )
        else:
            if kind is EventKind.WALL_ANCHOR:
                self._anchor_wall_ns = (
                    anchor_wall_ns if anchor_wall_ns is not None else event.wall_ns
                )
                self._anchor_mono_ns = event.mono_ns
            if self._sessions:
                current = self._sessions[-1]
                current.end_wall_ns = event.wall_ns
                current.last_seq = event.seq
                if kind is EventKind.SESSION_STOP:
                    current.clean_stop = True
            if kind in EDIT_KINDS:
                second = event.wall_ns // 1_000_000_000
                self._activity[second] = self._activity.get(second, 0) + 1
                self._activity_dirty = True

        self._prev_mono_ns = event.mono_ns
        self._last_wall_state = event.wall_ns
        if self._count == 0:
            self._first_wall_ns = event.wall_ns
        self._last_wall_ns = event.wall_ns
        self._count = event.seq + 1
        if (event.seq + 1) % self.index_stride == 0:
            self._pending_sample = self._running_state()

    # -- basic queries -------------------------------------------------------

    @property
    def count(self) -> int:
        """Number of events in the log."""
        return self._count

    @property
    def last_seq(self) -> int:
        """Sequence number of the last event, or -1 for an empty log."""
        return self._count - 1

    def sessions(self) -> list[Session]:
        """The application's run intervals, oldest first (copies).

        A session whose records contain a pause longer than
        :attr:`gap_threshold_ns` -- a suspend, a hibernation -- is reported as
        one :class:`Session` per continuous segment, sharing the session id.
        """
        threshold = self.gap_threshold_ns
        splits = [gap for gap in self._gaps if gap[3] - gap[1] >= threshold]
        out: list[Session] = []
        for session in self._sessions:
            inside = [
                gap for gap in splits
                if session.first_seq <= gap[0] and gap[2] <= session.last_seq
            ]
            if not inside:
                out.append(_replace(session, segment=0))
                continue
            start_wall = session.start_wall_ns
            first_seq = session.first_seq
            for number, (seq_before, wall_before, seq_after, wall_after) in enumerate(inside):
                out.append(
                    Session(session.session_id, start_wall, wall_before, False,
                            first_seq, seq_before, number)
                )
                start_wall = wall_after
                first_seq = seq_after
            out.append(
                Session(session.session_id, start_wall, session.end_wall_ns,
                        session.clean_stop, first_seq, session.last_seq, len(inside))
            )
        return out

    def time_range(self) -> tuple[int, int]:
        """First and last known wall clock time; ``(0, 0)`` for an empty log."""
        return (self._first_wall_ns, self._last_wall_ns)

    def index_entries(self) -> list[IndexEntry]:
        """The sampled index (diagnostics and tests)."""
        return list(self._index)

    # -- positioning ---------------------------------------------------------

    def _entry_for_seq(self, seq: int) -> IndexEntry | None:
        if not self._index_seq:
            return None
        position = bisect_right(self._index_seq, seq) - 1
        if position < 0:
            return None
        return self._index[position]

    def _entry_for_wall(self, wall_ns: int) -> IndexEntry | None:
        if not self._index_wall:
            return None
        position = bisect_right(self._index_wall, wall_ns) - 1
        if position < 0:
            return None
        return self._index[position]

    def _iter_from(self, entry: IndexEntry | None, stop_seq: int | None = None) -> Iterator[Event]:
        if entry is None:
            return self._reader.iter_events(HEADER_SIZE, stop_seq=stop_seq)
        return self._reader.iter_events(entry.offset, state=entry.state, stop_seq=stop_seq)

    def events(self, start_seq: int, end_seq: int) -> Iterator[Event]:
        """Events with ``start_seq <= seq < end_seq`` (half-open, like a slice)."""
        if end_seq <= start_seq:
            return
        entry = self._entry_for_seq(max(0, start_seq))
        for event in self._iter_from(entry, stop_seq=end_seq - 1):
            if event.seq < start_seq:
                continue
            yield event

    def event_at_or_before(self, wall_ns: int) -> Event | None:
        """Last event whose derived wall time is ``<= wall_ns``."""
        if self._count == 0 or wall_ns < self._first_wall_ns:
            return None
        if wall_ns >= self._last_wall_ns:
            entry = self._entry_for_seq(self.last_seq)
            found = None
            for event in self._iter_from(entry):
                found = event
            return found
        entry = self._entry_for_wall(wall_ns)
        found = None
        for event in self._iter_from(entry):
            if event.wall_ns > wall_ns:
                break
            found = event
        return found

    def event_after(self, wall_ns: int) -> Event | None:
        """First event whose derived wall time is strictly after ``wall_ns``."""
        if self._count == 0 or wall_ns >= self._last_wall_ns:
            return None
        entry = self._entry_for_wall(wall_ns)
        for event in self._iter_from(entry):
            if event.wall_ns > wall_ns:
                return event
        return None

    # -- reconstruction ------------------------------------------------------

    def reconstruct_seq(self, seq: int) -> str:
        """Document text after the event with sequence number ``seq``.

        ``seq < 0`` is the state before the log starts: the empty document.
        A checkpoint that does not match the log (it should not happen, but a
        damaged file must not corrupt a reconstruction) is reported and skipped
        in favour of an older one, and finally of a full replay.
        """
        if seq < 0 or self._count == 0:
            return ""
        seq = min(seq, self.last_seq)
        ceiling = seq
        while True:
            base = checkpoint_mod.latest_at_or_before(
                self.checkpoints_dir, ceiling, listing=self._checkpoint_listing_cached()
            )
            if base is None:
                return self._replay_or_stop("", -1, seq)
            if base.seq == seq:
                return base.text
            try:
                return self._replay(base.text, base.seq, seq)
            except OpError as exc:
                log.warning(
                    "history: checkpoint %s disagrees with the log (%s); falling back",
                    base.path, exc,
                )
                ceiling = base.seq - 1
                if ceiling < 0:
                    return self._replay_or_stop("", -1, seq)

    def _replay(self, base_text: str, base_seq: int, target_seq: int) -> str:
        entry = self._entry_for_seq(base_seq + 1)
        stream = self._iter_from(entry, stop_seq=target_seq)
        ops = (
            event.op
            for event in stream
            if event.op is not None and event.seq > base_seq
        )
        return replay_ops(base_text, ops)

    def _replay_or_stop(self, base_text: str, base_seq: int, target_seq: int) -> str:
        """:meth:`_replay`, but a record that does not apply ends the replay.

        A full replay is the last fallback there is -- there is no older
        checkpoint to retreat to -- so it must not raise: a single corrupt
        record that passes its one-byte checksum would otherwise make
        :meth:`ScratchpadStore.open` raise forever and the application
        unstartable.  Instead the replay stops at the last operation that
        applied, says so loudly and records the offending record in
        :attr:`replay_damage`, which the store turns into a truncation.
        """
        try:
            return self._replay(base_text, base_seq, target_seq)
        except OpError as exc:
            log.error(
                "history: the event log does not replay cleanly (%s); "
                "reconstructing what applies and stopping there", exc,
            )
        return self._replay_until_it_breaks(base_text, base_seq, target_seq)

    def _replay_until_it_breaks(self, base_text: str, base_seq: int, target_seq: int) -> str:
        """Apply operations one by one and stop at the first that does not fit."""
        entry = self._entry_for_seq(base_seq + 1)
        text = base_text
        for event in self._iter_from(entry, stop_seq=target_seq):
            if event.op is None or event.seq <= base_seq:
                continue
            try:
                text = apply_op(text, event.op)
            except OpError as exc:
                self._replay_damage = ReplayDamage(event.seq, event.offset, str(exc))
                log.error(
                    "history: event %d at byte offset %d does not apply (%s); "
                    "history ends there",
                    event.seq, event.offset, exc,
                )
                break
        return text

    def reconstruct_at(self, wall_ns: int) -> tuple[str, int]:
        """Document text at ``wall_ns`` and the sequence number it belongs to.

        The state is the one *after* the last event at or before ``wall_ns``.
        Before the first event this is ``("", -1)``.
        """
        event = self.event_at_or_before(wall_ns)
        if event is None:
            return "", -1
        return self.reconstruct_seq(event.seq), event.seq

    # -- activity ------------------------------------------------------------

    def _activity_arrays(self) -> tuple[list[int], list[int]]:
        if self._activity_dirty:
            items = sorted(self._activity.items())
            self._activity_seconds = [second for second, _ in items]
            self._activity_counts = [count for _, count in items]
            self._activity_dirty = False
        return self._activity_seconds, self._activity_counts

    def activity(self, start_wall_ns: int, end_wall_ns: int, buckets: int) -> list[int]:
        """Edit counts per time bucket across ``[start_wall_ns, end_wall_ns)``.

        Short windows are counted exactly from the events; very wide ones use
        the one-second histogram built during the scan, which is what keeps
        "zoom out to a year" cheap.
        """
        if buckets <= 0:
            return []
        span = end_wall_ns - start_wall_ns
        if span <= 0 or self._count == 0:
            return [0] * buckets
        counts = [0] * buckets

        first_entry = self._entry_for_wall(start_wall_ns)
        last_entry = self._entry_for_wall(end_wall_ns)
        first_seq = first_entry.seq if first_entry else 0
        last_seq = last_entry.seq if last_entry else self.last_seq
        estimated = max(0, last_seq - first_seq) + self.index_stride

        if estimated <= EXACT_ACTIVITY_EVENTS:
            for event in self._iter_from(first_entry):
                if event.wall_ns >= end_wall_ns:
                    break
                if event.wall_ns < start_wall_ns or event.op is None:
                    continue
                bucket = (event.wall_ns - start_wall_ns) * buckets // span
                if 0 <= bucket < buckets:
                    counts[bucket] += 1
            return counts

        seconds, per_second = self._activity_arrays()
        start_second = start_wall_ns // 1_000_000_000
        end_second = end_wall_ns // 1_000_000_000 + 1
        index = bisect_right(seconds, start_second - 1)
        while index < len(seconds) and seconds[index] < end_second:
            moment = seconds[index] * 1_000_000_000
            bucket = (moment - start_wall_ns) * buckets // span
            if 0 <= bucket < buckets:
                counts[bucket] += per_second[index]
            index += 1
        return counts
