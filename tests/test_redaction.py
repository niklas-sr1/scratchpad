"""Redaction: rewriting scratchpad history without a piece of text.

Acceptance criteria from `Design Specification.md` section 39 covered here:
18 (normal deletion preserves historical content), 19 (explicit redaction
removes unwanted content from retained history), 20 (the application never
promises forensic secure deletion -- the wording test), 21 (clearing the
scratchpad does not destroy history).

The crash tests take a small history, let every single ``os.replace`` of the
rewrite fail in turn, abandon the store the way a killed process would and
reopen the data directory.  After the recovery hook has run the history must be
exactly the old one or exactly the new one, with no protocol leftovers.
"""
from __future__ import annotations

import contextlib
import errno
import os
import random
import shutil
import time
from pathlib import Path

import pytest

from scratchpad import paths, redaction
from scratchpad.config import Config
from scratchpad.core import recovery
from scratchpad.core.clock import ManualClock
from scratchpad.core.eventlog import (
    EventKind,
    EventLogReader,
    EventLogWriter,
    encode_op,
    encode_session_start,
)
from scratchpad.core.ops import apply_op, delete, insert, replace
from scratchpad.core.store import ScratchpadStore
from scratchpad.redaction import RedactionReport, find_occurrence_window, redact_text

SECOND = 1_000_000_000
WALL0 = 1_700_000_000 * SECOND
MONO0 = 1_000 * SECOND
VERSION = "0.1.0-test"
SECRET = "sk-live-9Q2H4E7M"


# --- fixtures and helpers ---------------------------------------------------


def new_clock() -> ManualClock:
    return ManualClock(wall=WALL0, mono=MONO0)


def open_store(directory: Path, clock: ManualClock, config: Config | None = None) -> ScratchpadStore:
    directory.mkdir(parents=True, exist_ok=True)
    return ScratchpadStore.open(
        directory, config or Config(), app_version=VERSION, clock=clock
    )


class Scribe:
    """Drives a store with a deterministic clock, one event per call."""

    def __init__(self, store: ScratchpadStore, clock: ManualClock, step_ns: int = 1_000_000):
        self.store = store
        self.clock = clock
        self.step = step_ns

    def _tick(self) -> None:
        self.clock.advance(self.step)

    def type(self, text: str, at: int | None = None) -> None:
        """One INSERT per character, as real typing produces."""
        start = len(self.store.text) if at is None else at
        for index, char in enumerate(text):
            self._tick()
            self.store.apply(insert(start + index, char))

    def paste(self, text: str, at: int | None = None) -> None:
        """One INSERT for the whole string, as a paste produces."""
        self._tick()
        self.store.apply(insert(len(self.store.text) if at is None else at, text))

    def erase(self, start: int, length: int) -> None:
        self._tick()
        self.store.apply(delete(start, self.store.text[start : start + length]))

    def replace_range(self, start: int, length: int, text: str) -> None:
        self._tick()
        self.store.apply(replace(start, self.store.text[start : start + length], text))

    def clear(self) -> None:
        self._tick()
        self.store.apply(delete(0, self.store.text))

    def idle(self, seconds: float = 1.0) -> None:
        self.clock.advance(int(seconds * SECOND))
        self.store.heartbeat()


def read_events(directory: Path) -> list:
    return list(EventLogReader(paths.events_log(directory)).iter_events())


def states_by_wall(directory: Path) -> dict[int, str]:
    """The document state after the last event at each wall clock value."""
    doc = ""
    out: dict[int, str] = {}
    for event in read_events(directory):
        if event.op is not None:
            doc = apply_op(doc, event.op)
        out[event.wall_ns] = doc
    return out


def edit_fingerprint(directory: Path) -> list[tuple[int, int, str, str]]:
    """Every edit event as (wall_ns, kind, text, old_text); ignores lifecycle records."""
    return [
        (event.wall_ns, int(event.kind), event.op.text, event.op.old_text)
        for event in read_events(directory)
        if event.op is not None
    ]


def lifecycle_fingerprint(directory: Path) -> list[tuple[int, int]]:
    return [
        (event.wall_ns, int(event.kind))
        for event in read_events(directory)
        if event.op is None
    ]


def strip_all(text: str, target: str) -> str:
    """Brute force reference: remove until nothing is left to remove."""
    while target in text:
        text = text.replace(target, "")
    return text


def leftovers(directory: Path) -> list[str]:
    """Rewrite protocol files that must not survive an open."""
    allowed = {"events.log", "checkpoints", "index.cache"}
    return sorted(p.name for p in paths.history_dir(directory).iterdir() if p.name not in allowed)


def crash(store: ScratchpadStore) -> None:
    """Abandon a store the way a killed process would: no stop record, no checkpoint."""
    if store.closed:
        return
    store._closed = True
    with contextlib.suppress(Exception):
        store._writer.close()
    os.close(store._lock_fd)


@contextlib.contextmanager
def failing_replace(nth: int | None = None):
    """Count ``os.replace`` calls; with ``nth`` set, make that one fail."""
    real_replace, real_rename = os.replace, os.rename
    seen = {"count": 0}

    def fake(src, dst, **kwargs):
        seen["count"] += 1
        if nth is not None and seen["count"] == nth:
            raise OSError(errno.EIO, "simulated power failure", str(src))
        return real_replace(src, dst, **kwargs)

    os.replace = fake
    os.rename = fake
    try:
        yield seen
    finally:
        os.replace = real_replace
        os.rename = real_rename


def build_history(directory: Path, *, secret: str = SECRET) -> tuple[str, dict[int, str]]:
    """A small but varied history: two sessions, typing, a paste, a deletion."""
    clock = new_clock()
    store = open_store(directory, clock)
    scribe = Scribe(store, clock)
    scribe.type("notes\n")
    scribe.paste("api key: ")
    scribe.paste(secret)
    scribe.type("\nmore notes\n")
    scribe.idle()
    scribe.erase(6, 9 + len(secret))          # the user deletes the key again
    scribe.type("done\n")
    store.close()

    clock2 = ManualClock(wall=clock.wall + 60 * SECOND, mono=clock.mono + 60 * SECOND)
    store = open_store(directory, clock2)
    scribe = Scribe(store, clock2)
    scribe.type("second session\n")
    scribe.paste(secret, at=0)                 # and pastes it once more, elsewhere
    scribe.type("tail")
    text = store.text
    store.close()
    return text, states_by_wall(directory)


# --- the basic contract -----------------------------------------------------


def test_redaction_removes_the_text_from_every_retained_state(tmp_path: Path) -> None:
    """Acceptance criterion 19, checked three ways."""
    directory = tmp_path / "data"
    build_history(directory)
    before = states_by_wall(directory)

    store = open_store(directory, new_clock())
    try:
        report = redact_text(store, SECRET)
        assert report.changed
        assert SECRET not in store.text

        # 1. every historical state, reconstructed at its own timestamp
        for wall_ns, original in before.items():
            reconstructed, _seq = store.history.reconstruct_at(wall_ns)
            assert reconstructed == strip_all(original, SECRET)
            assert SECRET not in reconstructed
        # 2. no event payload carries it
        for event in read_events(directory):
            if event.op is not None:
                assert SECRET not in event.op.text
                assert SECRET not in event.op.old_text
        # 3. not even the raw bytes of the log
        assert SECRET.encode("utf-8") not in paths.events_log(directory).read_bytes()
    finally:
        store.close()

    reopened = open_store(directory, new_clock())
    try:
        assert SECRET not in reopened.text
    finally:
        reopened.close()


def test_the_report_counts_events_states_and_occurrences(tmp_path: Path) -> None:
    directory = tmp_path / "data"
    clock = new_clock()
    store = open_store(directory, clock)
    scribe = Scribe(store, clock)
    try:
        scribe.paste("a" + SECRET + "b")
        scribe.paste("c")
        before = store.event_count
        report = redact_text(store, SECRET)
    finally:
        store.close()
    # Two states contained the secret (the paste and the "c" that followed).
    assert report.states_changed == 2
    assert report.occurrences_removed == 2
    assert report.events_before == before
    assert report.events_after == before      # nothing became empty
    assert report.window is None
    assert not report.dry_run


def test_redacting_an_empty_string_is_refused(tmp_path: Path) -> None:
    directory = tmp_path / "data"
    build_history(directory)
    store = open_store(directory, new_clock())
    try:
        with pytest.raises(ValueError):
            redact_text(store, "")
    finally:
        store.close()


def test_an_absent_text_is_a_no_op_that_writes_nothing(tmp_path: Path) -> None:
    directory = tmp_path / "data"
    build_history(directory)
    log = paths.events_log(directory)

    store = open_store(directory, new_clock())
    try:
        # After the session record: opening a store always appends SESSION_START.
        before_bytes = log.read_bytes()
        before_stat = log.stat()
        events = store.event_count
        report = redact_text(store, "this text was never in the scratchpad")
        assert not report.changed
        assert report.events_before == report.events_after == events
        assert report.occurrences_removed == 0
        assert store.event_count == events          # no new session was started
        assert not recovery.pending_log_path(directory).exists()
        assert not recovery.journal_path(directory).exists()
        assert log.read_bytes() == before_bytes
        assert log.stat().st_mtime_ns == before_stat.st_mtime_ns
    finally:
        store.close()
    assert not leftovers(directory)


def test_a_dry_run_reports_without_changing_anything(tmp_path: Path) -> None:
    directory = tmp_path / "data"
    build_history(directory)
    log = paths.events_log(directory)

    store = open_store(directory, new_clock())
    try:
        before_bytes = log.read_bytes()
        preview = redact_text(store, SECRET, dry_run=True)
        assert preview.dry_run
        assert preview.changed
        assert log.read_bytes() == before_bytes
        assert not recovery.pending_log_path(directory).exists()
        real = redact_text(store, SECRET)
    finally:
        store.close()
    assert (preview.events_before, preview.events_after) == (real.events_before, real.events_after)
    assert preview.occurrences_removed == real.occurrences_removed
    assert preview.states_changed == real.states_changed
    assert "preview" in preview.summary()


# --- timing and session structure -------------------------------------------


def test_timing_and_session_boundaries_survive_the_rewrite(tmp_path: Path) -> None:
    directory = tmp_path / "data"
    build_history(directory)
    old_walls = {event.wall_ns for event in read_events(directory)}
    old_lifecycle = lifecycle_fingerprint(directory)
    old_sessions = [(s.session_id, s.start_wall_ns) for s in
                    _sessions_of(directory)]

    store = open_store(directory, new_clock())
    session_start_walls = {e.wall_ns for e in read_events(directory)}
    try:
        redact_text(store, SECRET)
        new_events = read_events(directory)
        # The reopened session appends its own SESSION_START; everything before
        # it must carry a timestamp that existed in the old log.
        for event in new_events[:-1]:
            assert event.wall_ns in old_walls or event.wall_ns in session_start_walls
        # Lifecycle records are copied through unchanged (minus the new one).
        assert lifecycle_fingerprint(directory)[: len(old_lifecycle)] == old_lifecycle
        new_sessions = [(s.session_id, s.start_wall_ns) for s in store.history.sessions()]
        assert new_sessions[: len(old_sessions)] == old_sessions
    finally:
        store.close()


def _sessions_of(directory: Path) -> list:
    from scratchpad.core.history import History

    history = History(paths.events_log(directory), paths.checkpoints_dir(directory))
    history.rebuild()
    return history.sessions()


def test_dropped_events_fold_their_time_into_the_next_record(tmp_path: Path) -> None:
    """Typing the secret one character at a time collapses, but time does not shift."""
    directory = tmp_path / "data"
    clock = new_clock()
    store = open_store(directory, clock)
    scribe = Scribe(store, clock)
    try:
        scribe.type("x")
        scribe.paste(SECRET, at=1)      # one event holds the whole secret
        scribe.paste(SECRET, at=1)      # and another one right after it
        scribe.type("y")
        marker_wall = read_events(directory)[-1].wall_ns
        report = redact_text(store, SECRET)
        assert report.events_after < report.events_before     # records disappeared
        assert report.events_dropped == 2
        walls = [event.wall_ns for event in read_events(directory)]
        assert marker_wall in walls                            # the survivor kept its time
        assert store.text == "xy"
    finally:
        store.close()


# --- occurrence windows -----------------------------------------------------


def test_find_occurrence_window_covers_the_whole_run(tmp_path: Path) -> None:
    directory = tmp_path / "data"
    clock = new_clock()
    store = open_store(directory, clock)
    scribe = Scribe(store, clock)
    try:
        scribe.type("head\n")
        scribe.paste(SECRET)
        first_wall = read_events(directory)[-1].wall_ns
        scribe.type("abc")
        middle_wall = read_events(directory)[-1].wall_ns
        scribe.erase(5, len(SECRET))
        gone_wall = read_events(directory)[-1].wall_ns
        scribe.type("tail")

        window = find_occurrence_window(store.history, SECRET, middle_wall)
        assert window is not None
        assert window[0] == first_wall
        assert window[1] < gone_wall
        # the state at `gone_wall` no longer holds it
        assert find_occurrence_window(store.history, SECRET, gone_wall) is None
        # ... and neither does a moment before it was ever pasted
        assert find_occurrence_window(store.history, SECRET, WALL0) is None
    finally:
        store.close()


def test_within_restricts_the_rewrite_to_the_occurrence_run(tmp_path: Path) -> None:
    directory = tmp_path / "data"
    clock = new_clock()
    store = open_store(directory, clock)
    scribe = Scribe(store, clock)
    try:
        scribe.type("head\n")
        scribe.paste(SECRET)                       # first occurrence
        first_wall = read_events(directory)[-1].wall_ns
        scribe.type("mid")
        inside_wall = read_events(directory)[-1].wall_ns
        scribe.erase(5, len(SECRET))
        scribe.type("\nlater\n")
        scribe.paste(SECRET)                       # second occurrence
        second_wall = read_events(directory)[-1].wall_ns
        scribe.type("end")
        before = states_by_wall(directory)

        # Ask for a single instant inside the first run; it must expand to the run.
        report = redact_text(store, SECRET, within=(inside_wall, inside_wall))
        assert report.window is not None
        assert report.window[0] <= first_wall
        assert report.window[1] < second_wall
        assert report.changed

        for wall_ns, original in before.items():
            reconstructed, _seq = store.history.reconstruct_at(wall_ns)
            if report.window[0] <= wall_ns <= report.window[1]:
                assert reconstructed == strip_all(original, SECRET)
            else:
                assert reconstructed == original
        # The second occurrence is untouched, including in the current document.
        assert SECRET in store.text
        assert SECRET in store.history.reconstruct_at(second_wall)[0]
    finally:
        store.close()


def test_a_window_without_an_occurrence_stays_as_given(tmp_path: Path) -> None:
    directory = tmp_path / "data"
    clock = new_clock()
    store = open_store(directory, clock)
    scribe = Scribe(store, clock)
    try:
        scribe.type("nothing to see")
        quiet = read_events(directory)[-1].wall_ns
        scribe.paste(SECRET)
        report = redact_text(store, SECRET, within=(WALL0, quiet))
        assert report.window == (WALL0, quiet)
        assert not report.changed          # the secret arrived after the window
        assert SECRET in store.text
    finally:
        store.close()


def test_checkpoints_cannot_resurrect_redacted_text(tmp_path: Path) -> None:
    """A checkpoint is a cache of a pre-redaction state; the rewrite must drop it.

    Reconstruction starts from the newest checkpoint at or before the target, so
    a surviving checkpoint would hand back the un-redacted text (exactly, when
    the target sequence number hits the checkpoint itself).  The rewrite passes
    no checkpoints to ``replace_history``, which must therefore clear the live
    checkpoint directory.
    """
    directory = tmp_path / "data"
    clock = new_clock()
    store = open_store(directory, clock, Config(checkpoint_every_events=8))
    scribe = Scribe(store, clock)
    try:
        scribe.type("start\n")
        scribe.paste(SECRET)
        scribe.type("\nmiddle\n")
        store.checkpoint()                       # a checkpoint holding the secret
        scribe.type("more text to push past the checkpoint interval\n")
        store.tick()
        store.checkpoint()
        scribe.paste(SECRET)
        scribe.type("\nend\n")
        store.checkpoint()
        checkpoint_files = sorted(paths.checkpoints_dir(directory).iterdir())
        assert checkpoint_files, "the fixture must produce checkpoints"
        before = states_by_wall(directory)

        redact_text(store, SECRET)

        for wall_ns, original in before.items():
            reconstructed, _seq = store.history.reconstruct_at(wall_ns)
            assert reconstructed == strip_all(original, SECRET), wall_ns
        for seq in range(store.history.count):
            assert SECRET not in store.history.reconstruct_seq(seq), seq
        for path in paths.checkpoints_dir(directory).iterdir():
            assert SECRET.encode("utf-8") not in path.read_bytes(), path
    finally:
        store.close()

    reopened = open_store(directory, new_clock())
    try:
        assert SECRET not in reopened.text
        for seq in range(reopened.history.count):
            assert SECRET not in reopened.history.reconstruct_seq(seq), seq
    finally:
        reopened.close()


# --- acceptance criteria 18, 20, 21 -----------------------------------------


def test_normal_deletion_preserves_historical_content(tmp_path: Path) -> None:
    """Acceptance criterion 18."""
    directory = tmp_path / "data"
    clock = new_clock()
    store = open_store(directory, clock)
    scribe = Scribe(store, clock)
    try:
        scribe.paste("keep " + SECRET)
        while_present = read_events(directory)[-1].wall_ns
        scribe.erase(0, len(store.text))
        assert store.text == ""
        assert store.history.reconstruct_at(while_present)[0] == "keep " + SECRET
        assert SECRET.encode("utf-8") in paths.events_log(directory).read_bytes()
    finally:
        store.close()


def test_clearing_the_scratchpad_does_not_destroy_history(tmp_path: Path) -> None:
    """Acceptance criterion 21."""
    directory = tmp_path / "data"
    clock = new_clock()
    store = open_store(directory, clock)
    scribe = Scribe(store, clock)
    try:
        scribe.type("first note\n")
        marker = read_events(directory)[-1].wall_ns
        events_before = store.event_count
        scribe.clear()
        assert store.text == ""
        assert store.event_count == events_before + 1        # clearing is one more event
        assert store.history.reconstruct_at(marker)[0] == "first note\n"
    finally:
        store.close()
    reopened = open_store(directory, new_clock())
    try:
        assert reopened.history.reconstruct_at(marker)[0] == "first note\n"
    finally:
        reopened.close()


def test_the_wording_never_promises_secure_erasure(tmp_path: Path) -> None:
    """Acceptance criterion 20 (spec section 26)."""
    report = RedactionReport(120, 118, 3, 12, False, (WALL0, WALL0 + SECOND))
    for text in (report.summary(), RedactionReport(1, 1, 0, 0, True).summary()):
        assert "secure" not in text.lower()
        assert "erase" not in text.lower()
        assert "shred" not in text.lower()
    assert "Remove permanently from scratchpad history" in report.summary()
    source = Path(redaction.__file__).read_text("utf-8")
    assert "secure" not in source.lower().replace("crash-safe", "")
    import scratchpad.gc as gc_module

    assert "secure" not in Path(gc_module.__file__).read_text("utf-8").lower()


def test_exact_matching_leaves_partial_prefixes_behind(tmp_path: Path) -> None:
    """Documented limitation: redaction matches the exact text, nothing else.

    A secret typed character by character existed as every one of its prefixes,
    and those prefixes are not the text the user asked to remove.  The UI is
    expected to offer the pasted (whole) string, which is the case that matters.
    """
    directory = tmp_path / "data"
    clock = new_clock()
    store = open_store(directory, clock)
    scribe = Scribe(store, clock)
    try:
        scribe.type("ab")
        half = read_events(directory)[-1].wall_ns
        scribe.type("cd")
        redact_text(store, "abcd")
        assert store.text == ""
        assert store.history.reconstruct_at(half)[0] == "ab"
    finally:
        store.close()


# --- agreement with a brute force reference ---------------------------------


@pytest.mark.parametrize("seed", range(8))
def test_random_histories_agree_with_a_brute_force_reference(tmp_path: Path, seed: int) -> None:
    """Random edits over a tiny alphabet, where the target overlaps constantly."""
    target = "AB"
    rng = random.Random(seed)
    directory = tmp_path / f"data-{seed}"
    clock = new_clock()
    store = open_store(directory, clock)
    scribe = Scribe(store, clock)
    try:
        for _ in range(120):
            text = store.text
            choice = rng.random()
            if choice < 0.45 or not text:
                where = rng.randrange(0, len(text) + 1)
                payload = "".join(rng.choice("AB C") for _ in range(rng.randrange(1, 4)))
                scribe.paste(payload, at=where)
            elif choice < 0.8:
                where = rng.randrange(0, len(text))
                scribe.erase(where, rng.randrange(1, min(4, len(text) - where + 1)))
            else:
                where = rng.randrange(0, len(text))
                length = rng.randrange(0, min(3, len(text) - where + 1))
                scribe.replace_range(where, length, rng.choice(["A", "BA", "AB", ""]) or "C")
        before = states_by_wall(directory)
        report = redact_text(store, target)
        for wall_ns, original in before.items():
            reconstructed, _seq = store.history.reconstruct_at(wall_ns)
            assert reconstructed == strip_all(original, target), (seed, wall_ns)
        assert target not in store.text
        assert report.events_after <= report.events_before
    finally:
        store.close()


# --- crash safety -----------------------------------------------------------


def test_every_rename_of_the_rewrite_is_crash_safe(tmp_path: Path) -> None:
    template = tmp_path / "template"
    old_text, _states = build_history(template)
    old_fingerprint = edit_fingerprint(template)

    reference = tmp_path / "reference"
    shutil.copytree(template, reference)
    store = open_store(reference, new_clock())
    with failing_replace() as counter:
        redact_text(store, SECRET)
    new_text = store.text
    store.close()
    new_fingerprint = edit_fingerprint(reference)
    renames = counter["count"]
    assert renames >= 5                      # journal, two swaps, checkpoint, current.txt
    assert new_fingerprint != old_fingerprint

    for nth in range(1, renames + 1):
        work = tmp_path / f"crash-{nth}"
        shutil.copytree(template, work)
        store = open_store(work, new_clock())
        with failing_replace(nth):
            with pytest.raises(OSError):
                redact_text(store, SECRET)
        crash(store)

        reopened = open_store(work, new_clock())
        try:
            fingerprint = edit_fingerprint(work)
            assert fingerprint in (old_fingerprint, new_fingerprint), f"mixed history at {nth}"
            assert reopened.text in (old_text, new_text), f"mixed text at {nth}"
            assert not leftovers(work), f"leftovers at {nth}: {leftovers(work)}"
        finally:
            reopened.close()
        assert not leftovers(work)


def test_a_crash_while_writing_the_new_log_keeps_the_old_history(tmp_path: Path) -> None:
    directory = tmp_path / "data"
    old_text, _states = build_history(directory)
    old_fingerprint = edit_fingerprint(directory)

    store = open_store(directory, new_clock())
    real_append = EventLogWriter.append
    state = {"n": 0}

    def exploding_append(self, kind, mono_delta, payload):
        state["n"] += 1
        if state["n"] == 12:
            raise OSError(errno.ENOSPC, "simulated full disk")
        return real_append(self, kind, mono_delta, payload)

    EventLogWriter.append = exploding_append
    try:
        with pytest.raises(OSError):
            redact_text(store, SECRET)
    finally:
        EventLogWriter.append = real_append
    crash(store)

    # The half written replacement was cleaned up immediately; even if it had
    # not been, the next open would discard it (no journal was written).
    assert not recovery.pending_log_path(directory).exists()
    reopened = open_store(directory, new_clock())
    try:
        assert reopened.text == old_text
        assert edit_fingerprint(directory) == old_fingerprint
        assert not leftovers(directory)
    finally:
        reopened.close()


def test_a_stale_rewrite_file_is_discarded_at_open(tmp_path: Path) -> None:
    directory = tmp_path / "data"
    old_text, _states = build_history(directory)
    stale = recovery.pending_log_path(directory)
    stale.write_bytes(b"SCRPLOG1" + b"\x01\x00\x00\x00" + b"garbage")
    store = open_store(directory, new_clock())
    try:
        assert store.text == old_text
        assert not stale.exists()
        assert not leftovers(directory)
    finally:
        store.close()


# --- attachments ------------------------------------------------------------


def test_purging_an_attachment_removes_the_token_and_the_object(tmp_path: Path) -> None:
    from scratchpad.attachments import AttachmentStore, ResolutionState
    from scratchpad.tokens import TokenCodec, load_or_create_secret

    directory = tmp_path / "data"
    directory.mkdir()
    codec = TokenCodec(load_or_create_secret(paths.secret_key(directory)))
    attachments = AttachmentStore(directory, codec)
    keeper = attachments.create_text(b"kept log output\n")
    victim = attachments.create_text(b"a screenshot that should never have been taken\n")

    clock = new_clock()
    store = open_store(directory, clock)
    scribe = Scribe(store, clock)
    try:
        scribe.paste("log: " + keeper.token + "\n")
        scribe.paste("oops: " + victim.token + "\n")
        while_present = read_events(directory)[-1].wall_ns
        scribe.erase(0, len(store.text))            # deleting it does not purge it
        assert attachments.get(victim.object_id) is not None

        report = redaction.purge_attachment(store, attachments, victim.object_id)
        assert report.changed
        assert victim.token not in store.text
        for wall_ns in states_by_wall(directory):
            assert victim.token not in store.history.reconstruct_at(wall_ns)[0]
        assert victim.token.encode("ascii") not in paths.events_log(directory).read_bytes()
        assert attachments.get(victim.object_id) is None

        # The keeper is untouched, and its history still resolves.
        assert attachments.get(keeper.object_id) is not None
        assert store.history.reconstruct_at(while_present)[0].count(keeper.token) == 1
        assert attachments.resolve(keeper.token).state is ResolutionState.VALID

        # Documented and harmless: the purged token still authenticates against
        # the installation key (the key did not change), so resolving the string
        # reports MISSING rather than ORDINARY.  Nothing in the retained history
        # contains it any more, so nothing can resolve it in practice.
        assert attachments.resolve(victim.token).state is ResolutionState.MISSING
    finally:
        attachments.close()
        store.close()


def test_purging_an_unknown_attachment_raises(tmp_path: Path) -> None:
    from scratchpad.attachments import AttachmentStore
    from scratchpad.tokens import TokenCodec, load_or_create_secret

    directory = tmp_path / "data"
    directory.mkdir()
    codec = TokenCodec(load_or_create_secret(paths.secret_key(directory)))
    attachments = AttachmentStore(directory, codec)
    store = open_store(directory, new_clock())
    try:
        with pytest.raises(LookupError):
            redaction.purge_attachment(store, attachments, 999)
    finally:
        attachments.close()
        store.close()


def test_a_dry_run_purge_keeps_the_attachment(tmp_path: Path) -> None:
    from scratchpad.attachments import AttachmentStore
    from scratchpad.tokens import TokenCodec, load_or_create_secret

    directory = tmp_path / "data"
    directory.mkdir()
    codec = TokenCodec(load_or_create_secret(paths.secret_key(directory)))
    attachments = AttachmentStore(directory, codec)
    clock = new_clock()
    store = open_store(directory, clock)
    scribe = Scribe(store, clock)
    try:
        victim = attachments.create_text(b"payload\n")
        scribe.paste("see " + victim.token)
        report = redaction.purge_attachment(store, attachments, victim.object_id, dry_run=True)
        assert report.dry_run and report.changed
        assert attachments.get(victim.object_id) is not None
        assert victim.token in store.text
    finally:
        attachments.close()
        store.close()


# --- performance ------------------------------------------------------------


def _build_big_log(directory: Path, *, events: int, doc_chars: int, secret: str) -> None:
    """Write a log directly: 8 KB of text, then many small edits around it.

    The secret sits in the document for 2000 of the events, so the rewrite has
    to take both paths: copy records through untouched, and strip + diff.
    """
    paths.ensure_data_layout(directory)
    writer = EventLogWriter(paths.events_log(directory), buffer_bytes=1 << 20)
    writer.append(EventKind.SESSION_START, 0, encode_session_start(7, WALL0, MONO0, VERSION))
    filler = ("scratchpad " * (doc_chars // 11 + 1))[:doc_chars]
    kind, payload = encode_op(insert(0, filler))
    writer.append(kind, 1_000_000, payload)

    cursor = doc_chars // 2
    present = False
    pending = False          # an "x" is currently in the document
    for index in range(events):
        if index == 1000:
            op = insert(cursor, secret)
            present = True
        elif index == 3000:
            op = delete(cursor, secret)
            present = False
        else:
            where = cursor + (len(secret) if present else 0)
            op = delete(where, "x") if pending else insert(where, "x")
            pending = not pending
        kind, payload = encode_op(op)
        writer.append(kind, 1_000_000, payload)
    writer.close()


def test_rewrite_of_a_large_history_is_fast(tmp_path: Path) -> None:
    """200000 events over an 8 KB document must rewrite well under 30 seconds."""
    directory = tmp_path / "big"
    events = 200_000
    _build_big_log(directory, events=events, doc_chars=8192, secret=SECRET)
    config = Config(checkpoint_every_events=10 ** 9)
    store = open_store(directory, new_clock(), config)
    try:
        assert store.event_count == events + 3   # + the log header session and this one
        started = time.monotonic()
        report = redact_text(store, SECRET)
        elapsed = time.monotonic() - started
        assert report.changed
        assert SECRET not in store.text
        assert elapsed < 30.0, f"rewrite took {elapsed:.1f} s"
        print(f"\n200k-event rewrite: {elapsed:.1f} s")
    finally:
        store.close()
