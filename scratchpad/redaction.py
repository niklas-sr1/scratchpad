"""Destructive history management: rewrite the log without a piece of text.

Normal deletion preserves history (that is the whole point of the event log),
so the spec asks for one deliberate exception: an explicit operation that takes
a pasted API key, password or screenshot token out of *every* retained
historical state.  The product wording for it is "Remove permanently from
scratchpad history"; this module never claims more than that, because crash
persistence leaves copies behind in places no application controls (filesystem
snapshots, backups, flash translation layers, swap).  See spec section 26.

How the rewrite works
---------------------

The log is never mutated in place.  Instead every event is replayed while two
documents are carried along:

``untransformed``
    exactly what the old log says the document was at that point,
``transformed``
    the same state with every occurrence of the target text taken out.

For each edit event the new untransformed state is computed with ``apply_op``,
the new transformed state is derived from it, and the difference between the
two consecutive *transformed* states is expressed as fresh operations with
:func:`scratchpad.textdiff.diff_ops`.  Those operations inherit the timing of
the event that produced them.  When the difference is empty (the edit only
touched text that is being removed) no record is written at all and the
event's share of the monotonic timeline is folded into the next record, so
every surviving event keeps its original ``wall_ns`` to the nanosecond.
SESSION_START, SESSION_STOP, HEARTBEAT and WALL_ANCHOR records are copied
verbatim: they carry the anchors that every timestamp in the file is derived
from, and none of them carries document text.

Cost.  The interesting property is that an edit which neither sits in a
redacted state nor produces one needs no work beyond ``apply_op``: the original
record still expresses it exactly, so its payload bytes are copied straight
through and no diff is computed.  Whether the target is present is tracked
incrementally -- a new occurrence can only appear within ``len(target) - 1``
characters of the edit, so the usual per-event check scans a few dozen
characters instead of the whole document.  The expensive path (strip the target
out of the whole state, diff the result against the previous one) runs only
while an occurrence is actually present, which is a small part of a real
history.  Measured: 200000 events over an 8 KB document rewrite in about four
seconds (see ``tests/test_redaction.py::test_rewrite_of_a_large_history_is_fast``).

Removal is exhaustive, not a single pass: ``str.replace`` alone can leave a
fresh occurrence behind (removing ``ab`` from ``aabb`` yields ``ab`` again), so
the strip repeats until nothing is left.  That guarantees the property the
operation promises -- no retained state contains the text any more -- at the
price of occasionally removing a few characters that only formed the target
after an earlier removal.

Crash safety
------------

The new log is written to ``history/events.log.rewrite``, the name the recovery
protocol knows (ARCHITECTURE.md section 11), and fsynced before anything else
happens.  Handing it to :meth:`ScratchpadStore.replace_history` journals the
swap and performs it; a crash at any point leaves either the old history or the
new one, and the next :meth:`ScratchpadStore.open` finishes or discards what
was in flight.  Because the file is written under its protocol name from the
start, even a crash *during* the write leaves nothing but a stale
``.rewrite`` file that the next open deletes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from scratchpad import paths
from scratchpad.core import recovery
from scratchpad.core.eventlog import Event, EventKind, EventLogWriter, encode_op
from scratchpad.core.history import History
from scratchpad.core.ops import Op, apply_op
from scratchpad.core.varint import decode_uvarint
from scratchpad.textdiff import diff_ops

log = logging.getLogger(__name__)

__all__ = [
    "ACTION_WORDING",
    "RedactionError",
    "RedactionReport",
    "find_occurrence_window",
    "purge_attachment",
    "redact_text",
]

#: The rewritten log is a bulk write; buffer it instead of one syscall per event.
WRITE_BUFFER_BYTES = 1 << 20

#: Product wording (spec section 26).  Not "erase", not "shred", and no promise
#: about physical storage.
ACTION_WORDING = "Remove permanently from scratchpad history"


class RedactionError(RuntimeError):
    """A history rewrite could not be completed or did not achieve its goal."""


@dataclass(frozen=True)
class RedactionReport:
    """What a rewrite did (or, for a dry run, what it would do).

    ``occurrences_removed`` counts occurrences *per historical state*: text that
    sat in the document across 300 edits was present in 300 states and is
    counted 300 times.  ``states_changed`` counts the states (one per event,
    including the ones a heartbeat merely carries forward) that came out
    different from the original.
    """

    events_before: int
    events_after: int
    occurrences_removed: int
    states_changed: int
    dry_run: bool = False
    window: tuple[int, int] | None = None

    @property
    def events_dropped(self) -> int:
        """Events that disappeared because they only touched removed text."""
        return max(0, self.events_before - self.events_after)

    @property
    def changed(self) -> bool:
        """True when the rewrite altered anything at all."""
        return self.states_changed > 0 or self.events_before != self.events_after

    def summary(self) -> str:
        """One paragraph for the confirmation dialog and the log."""
        if not self.changed:
            found = "The text was not found in any retained historical state; nothing to do."
            return f"{ACTION_WORDING}: {found}"
        if self.dry_run:
            head = f"{ACTION_WORDING} (preview, nothing has been changed yet)"
            verb = "would be removed from"
            tail = (
                f"the history would be rewritten from {self.events_before} "
                f"to {self.events_after} events"
            )
        else:
            head = ACTION_WORDING
            verb = "removed from"
            tail = (
                f"the history was rewritten from {self.events_before} "
                f"to {self.events_after} events"
            )
        body = (
            f"{self.occurrences_removed} occurrence(s) {verb} "
            f"{self.states_changed} of {self.events_before} historical states; {tail}"
        )
        if self.window is not None:
            body += (
                f"; restricted to {_format_wall(self.window[0])} .. "
                f"{_format_wall(self.window[1])}"
            )
        return f"{head}: {body}."


def _format_wall(wall_ns: int) -> str:
    """Local time of a wall clock nanosecond value, for the report."""
    try:
        return datetime.fromtimestamp(wall_ns / 1e9).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):  # pragma: no cover - absurd clocks
        return f"{wall_ns} ns"


# --- the transform ----------------------------------------------------------


def _strip_all(text: str, target: str) -> tuple[str, int]:
    """``text`` without any occurrence of ``target``, plus how many were removed.

    Repeats until no occurrence is left: a single pass can join two halves into
    a fresh occurrence.  Each round is one C-level ``str.replace``.
    """
    removed = 0
    width = len(target)
    while True:
        stripped = text.replace(target, "")
        gone = len(text) - len(stripped)
        if not gone:
            return text, removed
        removed += gone // width
        text = stripped


def _contains_after(new_doc: str, op: Op, target: str, had: bool) -> bool:
    """Whether ``target`` occurs in ``new_doc``, cheaply when it did not before.

    An edit at ``op.pos`` can only create an occurrence that overlaps the
    characters it wrote, so when the previous state was free of the target only
    a window of ``len(op.text) + 2 * len(target)`` characters has to be looked
    at.  Once an occurrence exists the whole state is scanned, which happens
    only while the redaction is actually doing something.
    """
    if had:
        return target in new_doc
    width = len(target)
    start = op.pos - width + 1
    if start < 0:
        start = 0
    stop = op.pos + len(op.text) + width - 1
    return target in new_doc[start:stop]


# --- occurrence windows -----------------------------------------------------


def find_occurrence_window(
    history: History, text: str, at_wall_ns: int
) -> tuple[int, int] | None:
    """Wall clock bounds of the run of states around ``at_wall_ns`` holding ``text``.

    Returns ``(first_wall_ns, last_wall_ns)`` of the events that bound the
    maximal contiguous run of historical states containing ``text`` that covers
    ``at_wall_ns``, or ``None`` when the state at that moment does not contain
    it.  This is what turns "redact this occurrence" into a time window: the
    whole life of the occurrence, not just the moment the user pointed at.
    """
    if not text:
        raise ValueError("find_occurrence_window needs a non-empty text")
    anchor = history.event_at_or_before(at_wall_ns)
    if anchor is None:
        return None
    target_seq = anchor.seq

    doc = ""
    has = False
    run_first_seq = -1
    run_first_wall = 0
    run_last_seq = -1
    run_last_wall = 0

    for event in history.events(0, history.count):
        if event.op is not None:
            doc = apply_op(doc, event.op)
            has = _contains_after(doc, event.op, text, has)
        if has:
            if run_first_seq < 0:
                run_first_seq, run_first_wall = event.seq, event.wall_ns
            run_last_seq, run_last_wall = event.seq, event.wall_ns
        else:
            if run_first_seq >= 0 and run_first_seq <= target_seq <= run_last_seq:
                return (run_first_wall, run_last_wall)
            run_first_seq = -1
        if event.seq >= target_seq and not has:
            return None
    if run_first_seq >= 0 and run_first_seq <= target_seq <= run_last_seq:
        return (run_first_wall, run_last_wall)
    return None


def _expand_window(
    history: History, text: str, within: tuple[int, int] | None
) -> tuple[int, int] | None:
    """Grow ``within`` so that it covers the whole occurrence runs at its ends."""
    if within is None:
        return None
    low, high = within
    if high < low:
        low, high = high, low
    bounds = [low, high]
    for moment in (low, high):
        run = find_occurrence_window(history, text, moment)
        if run is not None:
            bounds.extend(run)
    return (min(bounds), max(bounds))


# --- the rewrite ------------------------------------------------------------


def _record_payload(buf: bytearray, offset: int) -> bytes:
    """The payload bytes of the record starting at ``offset``.

    Copying the payload rather than re-encoding keeps records that the redaction
    does not touch bit-identical, including the application version string in
    SESSION_START and the raw anchor in WALL_ANCHOR (which can differ from the
    clamped ``Event.wall_ns``).
    """
    body_len, body_start = decode_uvarint(buf, offset)
    _kind, after_kind = decode_uvarint(buf, body_start)
    _delta, payload_start = decode_uvarint(buf, after_kind)
    return bytes(buf[payload_start : body_start + body_len])


@dataclass
class _PassResult:
    """Totals of one replay over the old log."""

    events_after: int = 0
    occurrences: int = 0
    states_changed: int = 0


def _in_window(window: tuple[int, int] | None, wall_ns: int) -> bool:
    return window is None or window[0] <= wall_ns <= window[1]


def _would_change(history: History, target: str, window: tuple[int, int] | None) -> bool:
    """Whether any state inside ``window`` contains ``target``.  Stops at the first."""
    doc = ""
    has = False
    for event in history.events(0, history.count):
        op = event.op
        if op is None:
            continue
        doc = apply_op(doc, op)
        has = _contains_after(doc, op, target, has)
        if has and _in_window(window, event.wall_ns):
            return True
    return False


def _run_pass(
    history: History,
    target: str,
    window: tuple[int, int] | None,
    writer: EventLogWriter | None,
) -> _PassResult:
    """Replay the old log, writing the redacted one to ``writer`` (or nowhere).

    With ``writer=None`` this computes exactly the same totals without touching
    the filesystem, which is what a dry run is.
    """
    buf = history.reader.data()
    result = _PassResult()
    doc = ""            # the original state
    red = ""            # the state as it will be kept
    has = False         # target occurs in `doc`
    dirty = False       # `red` differs from `doc`
    prev_mono_ns = 0

    for event in history.events(0, history.count):
        op = event.op
        if op is None:
            # Lifecycle record: no document text, copied unchanged.
            if writer is not None:
                delta = 0 if event.kind is EventKind.SESSION_START else _delta(event, prev_mono_ns)
                writer.append(int(event.kind), delta, _record_payload(buf, event.offset))
            prev_mono_ns = event.mono_ns
            result.events_after += 1
            if dirty:
                result.states_changed += 1
            continue

        new_doc = apply_op(doc, op)
        new_has = _contains_after(new_doc, op, target, has)
        if new_has and _in_window(window, event.wall_ns):
            new_red, removed = _strip_all(new_doc, target)
            result.occurrences += removed
            new_dirty = True
        else:
            new_red = new_doc
            new_dirty = False

        if not dirty and not new_dirty:
            # Neither state is redacted, so the original record still says
            # exactly the right thing; copy its payload straight through.
            if writer is not None:
                writer.append(int(event.kind), _delta(event, prev_mono_ns),
                              _record_payload(buf, event.offset))
            prev_mono_ns = event.mono_ns
            result.events_after += 1
        else:
            for new_op in diff_ops(red, new_red):
                if writer is not None:
                    kind, payload = encode_op(new_op)
                    writer.append(int(kind), _delta(event, prev_mono_ns), payload)
                # Several records share one event's timestamp: the first carries
                # the delta, the rest follow at delta 0.
                prev_mono_ns = event.mono_ns
                result.events_after += 1

        doc, red, has, dirty = new_doc, new_red, new_has, new_dirty
        if dirty:
            result.states_changed += 1

    return result


def _delta(event: Event, prev_mono_ns: int) -> int:
    """Monotonic delta from the last written record; never negative."""
    delta = event.mono_ns - prev_mono_ns
    return delta if delta > 0 else 0


def redact_text(
    store,
    text: str,
    *,
    within: tuple[int, int] | None = None,
    dry_run: bool = False,
) -> RedactionReport:
    """Take every occurrence of ``text`` out of the retained history.

    ``text`` is matched exactly and case sensitively and must not be empty.
    With ``within=(a, b)`` the window is first grown to cover the whole
    occurrence runs around ``a`` and around ``b`` (see
    :func:`find_occurrence_window`) and only states whose event falls inside the
    grown window are redacted; states outside keep the text, which is what
    "redact this occurrence" means.  Without ``within`` every state is redacted
    and the current document is guaranteed to be free of the text afterwards.

    Returns a :class:`RedactionReport`.  ``dry_run=True`` computes the report
    without writing anything.
    """
    if not text:
        raise ValueError("refusing to redact an empty string")
    store.flush(fsync=False)
    history = store.history
    events_before = history.count
    window = _expand_window(history, text, within)

    if not _would_change(history, text, window):
        log.info("redaction: %d character(s) not present in any retained state", len(text))
        return RedactionReport(events_before, events_before, 0, 0, dry_run, window)

    if dry_run:
        result = _run_pass(history, text, window, None)
        return RedactionReport(
            events_before, result.events_after, result.occurrences,
            result.states_changed, True, window,
        )

    data_dir = Path(store.data_dir)
    staged = recovery.pending_log_path(data_dir)
    staged.unlink(missing_ok=True)      # a leftover from an aborted attempt
    writer = EventLogWriter(staged, buffer_bytes=WRITE_BUFFER_BYTES)
    try:
        result = _run_pass(history, text, window, writer)
        writer.close()                  # flush + fsync + close
        paths.fsync_dir(paths.history_dir(data_dir))
    except BaseException:
        writer.close()
        staged.unlink(missing_ok=True)
        raise

    # From here on the swap protocol owns the files: whatever goes wrong,
    # recover_pending_rewrite() at the next open decides old-or-new.
    store.replace_history(staged, None)

    if window is None and text in store.text:  # pragma: no cover - guarded by _strip_all
        raise RedactionError(
            "the rewritten history still contains the text; this is a bug in the rewrite"
        )
    report = RedactionReport(
        events_before, result.events_after, result.occurrences,
        result.states_changed, False, window,
    )
    log.warning("redaction: %s", report.summary())
    return report


def purge_attachment(
    store,
    attachments,
    object_id: int,
    *,
    dry_run: bool = False,
) -> RedactionReport:
    """Remove an attachment's token from history, then delete the attachment.

    The blob goes only after the history rewrite has committed, so a crash can
    never leave history pointing at an object that was already thrown away.
    Afterwards the token still authenticates cryptographically -- the
    installation key did not change -- so resolving it would report MISSING.
    That is harmless: the token no longer appears anywhere in the retained
    history, so nothing can resolve it.
    """
    attachment = attachments.get(object_id)
    if attachment is None:
        raise LookupError(f"no attachment with object id {object_id}")
    report = redact_text(store, attachment.token, within=None, dry_run=dry_run)
    if not dry_run:
        attachments.delete(object_id)
        log.warning("purge: attachment %d deleted after the history rewrite", object_id)
    return report
