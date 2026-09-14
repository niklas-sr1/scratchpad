"""The three destructive flows, driven through the window against the real modules.

``tests/test_ui_app.py`` only proves that a *missing* ``scratchpad.redaction``
degrades into a dialog.  This file is the other half: a real store, a real
``AttachmentStore``, the real ``scratchpad.redaction`` and ``scratchpad.gc``,
and the window's own command callbacks.

The dialogs are the only thing that is replaced.  GTK4 has no nested main loop,
so every confirmation in ``scratchpad.ui.dialogs`` is asynchronous and would
never answer itself in a test; :class:`DialogSpy` swaps the four helpers for
functions that record what was shown and invoke the confirmation callback
synchronously.  Everything behind them -- the history rewrite, the swap
protocol, the attachment deletion, the editor reload, the timeline and the
preview caches -- is the production code path.

The window is never presented: these tests run against a live Wayland display
and must not steal the focus of whoever is looking at the screen.
"""

from __future__ import annotations

import itertools
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from scratchpad import gc as gc_mod
from scratchpad import redaction as redaction_mod
from scratchpad.attachments import AttachmentStore, ResolutionState
from scratchpad.config import Config
from scratchpad.core.store import ScratchpadStore
from scratchpad.tokens import TokenCodec, load_or_create_secret
from tests.test_ui_app import _application, needs_display

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

#: One secret that never appears in any other test data.
SECRET = "sk-live-4QhT9zR2wKpM7bVn"

_APP_IDS = itertools.count()


# --------------------------------------------------------------------------- #
# harness
# --------------------------------------------------------------------------- #

class DialogSpy:
    """Replaces the modal helpers with synchronous, recording stand-ins.

    ``choice`` is the redaction mode the "which redaction?" dialog answers with,
    ``confirm`` decides whether the confirmation step of a report dialog is
    taken.  With ``confirm=False`` only the dry run happens, which is how these
    tests check that nothing is touched before the user agrees.
    """

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        choice: str | None = None,
        confirm: bool = True,
    ) -> None:
        from scratchpad.ui import dialogs

        self.dialogs = dialogs
        self.reports: list[SimpleNamespace] = []
        self.choices: list[SimpleNamespace] = []
        self.errors: list[tuple[str, str]] = []
        self.infos: list[tuple[str, str]] = []
        self.confirms: list[tuple[str, str]] = []
        self._choice = choice
        self._confirm = confirm
        monkeypatch.setattr(dialogs, "report", self._report)
        monkeypatch.setattr(dialogs, "redaction_choice", self._redaction_choice)
        monkeypatch.setattr(dialogs, "error", self._error)
        monkeypatch.setattr(dialogs, "info", self._info)
        monkeypatch.setattr(dialogs, "confirm", self._confirm_dialog)

    # the four replaced helpers ---------------------------------------------

    def _report(self, _parent, heading, body, payload, *, confirm_label=None, on_confirm=None):
        entry = SimpleNamespace(
            heading=heading,
            body=body,
            payload=payload,
            confirm_label=confirm_label,
            rendered=self.dialogs.format_report(payload),
        )
        self.reports.append(entry)
        if on_confirm is not None and self._confirm:
            on_confirm()
        return entry

    def _redaction_choice(
        self, _parent, *, snippet, can_purge, on_choice, can_redact_occurrence=True
    ):
        entry = SimpleNamespace(
            snippet=snippet,
            can_purge=can_purge,
            can_redact_occurrence=can_redact_occurrence,
        )
        self.choices.append(entry)
        if self._choice is not None:
            on_choice(self._choice)
        return entry

    def _error(self, _parent, heading, body):
        self.errors.append((heading, body))
        return None

    def _info(self, _parent, heading, body):
        self.infos.append((heading, body))
        return None

    def _confirm_dialog(self, _parent, heading, body, *, on_confirm, **_kwargs):
        self.confirms.append((heading, body))
        if self._confirm:
            on_confirm()
        return None

    # helpers ----------------------------------------------------------------

    @property
    def dry_run_report(self) -> SimpleNamespace:
        """The first report, which must always be the preview."""
        assert self.reports, "no report dialog was shown"
        return self.reports[0]

    @property
    def final_report(self) -> SimpleNamespace:
        """The report shown after the operation really ran."""
        assert len(self.reports) >= 2, f"expected a second report, got {self.reports}"
        return self.reports[-1]

    def text_of(self, entry: SimpleNamespace) -> str:
        """Everything the user can read in one dialog."""
        return f"{entry.heading}\n{entry.body}\n{entry.rendered}"


@pytest.fixture
def ui(tmp_path: Path):
    """A window on a real store in ``tmp_path``, torn down in the right order."""
    import gi

    gi.require_version("Adw", "1")
    from gi.repository import Adw

    from scratchpad import paths
    from scratchpad.ui.window import ScratchpadWindow

    Adw.init()
    data_dir = tmp_path / "data"
    store = ScratchpadStore.open(data_dir, Config(), app_version="test")
    codec = TokenCodec(load_or_create_secret(paths.secret_key(data_dir)))
    attachments = AttachmentStore(data_dir, codec)
    app = _application(f"dev.scratchpad.TestRedaction{next(_APP_IDS)}")
    window = ScratchpadWindow(
        app, store=store, config=Config(), attachments=attachments, codec=codec
    )
    try:
        yield SimpleNamespace(
            window=window,
            editor=window.editor,
            store=store,
            attachments=attachments,
            codec=codec,
            data_dir=data_dir,
        )
    finally:
        window.shutdown()
        attachments.close()
        if not store.closed:
            store.close()


def _select_in_editor(window, needle: str) -> None:
    """Select ``needle`` in the editor (the selection a redaction works on)."""
    text = window.editor.get_text()
    index = text.index(needle)
    window.editor.select_range(index, index + len(needle))
    window.registry.refresh()


def _select_in_history_view(window, needle: str) -> None:
    """Select ``needle`` in the reconstructed, read-only historical state."""
    buffer = window.history_view.buffer
    text = window.history_view.get_text()
    index = text.index(needle)
    buffer.select_range(
        buffer.get_iter_at_offset(index), buffer.get_iter_at_offset(index + len(needle))
    )
    assert window.history_view.get_selected_text() == needle
    window.registry.refresh()


def _event_walls(store: ScratchpadStore) -> list[int]:
    """The wall clock instant of every event in the log."""
    return [e.wall_ns for e in store.history.events(0, store.history.count)]


def _sampled_states(store: ScratchpadStore, extra: int = 12) -> list[str]:
    """Every per-event state plus evenly sampled instants across the whole range."""
    states = [store.history.reconstruct_at(wall)[0] for wall in _event_walls(store)]
    first, last = store.history.time_range()
    if last > first:
        step = (last - first) / (extra + 1)
        states.extend(
            store.history.reconstruct_at(int(first + step * i))[0]
            for i in range(extra + 2)
        )
    return states


def _count_refreshes(monkeypatch: pytest.MonkeyPatch, timeline) -> list[int]:
    """Count ``TimelineWidget.refresh`` calls (``set_history`` goes through it too)."""
    calls: list[int] = []
    real = timeline.refresh

    def counting() -> None:
        calls.append(1)
        real()

    monkeypatch.setattr(timeline, "refresh", counting)
    return calls


def _assert_reloaded(ui, *, absent: str | None = None) -> None:
    """The invariants every rewrite must leave behind in the window."""
    window = ui.window
    assert window.editor.get_text() == ui.store.text, "the editor must show the new document"
    assert window.editor.buffer.get_can_undo() is False, (
        "the undo stack must be cleared: an undo would restore redacted text"
    )
    assert window.cursor_pane.current_page_name() == "blank"
    assert window.hover_pane.current_page_name() == "blank"
    assert window.editor._resolution_cache == {}, "stale resolutions must be dropped"
    assert window._history_seq == -1 and window._history_wall_ns is None
    if absent is not None:
        assert absent not in ui.store.text
        for state in _sampled_states(ui.store):
            assert absent not in state, "a retained historical state still holds it"


# --------------------------------------------------------------------------- #
# (a) redact everywhere
# --------------------------------------------------------------------------- #

@needs_display
def test_redact_everywhere_previews_then_rewrites_history(ui, monkeypatch) -> None:
    """The dry run changes nothing; confirming removes the secret from every state."""
    editor = ui.window.editor
    editor.insert_at_cursor("notes before\n")
    time.sleep(0.002)
    editor.insert_at_cursor(f"key={SECRET}\n")     # one paste, one INSERT
    time.sleep(0.002)
    editor.insert_at_cursor("notes after\n")
    time.sleep(0.002)
    editor.insert_at_cursor(f"again {SECRET} inline\n")
    assert ui.store.text.count(SECRET) == 2
    events_before = ui.store.history.count

    _select_in_editor(ui.window, SECRET)
    assert ui.window.registry.is_enabled("redact-selection") is True

    # -- the preview alone must not touch anything ---------------------------
    preview_spy = DialogSpy(monkeypatch, choice="everywhere", confirm=False)
    ui.window._cmd_redact()
    assert len(preview_spy.reports) == 1, "the dry run report must come first"
    assert preview_spy.dry_run_report.payload.dry_run is True
    assert preview_spy.dry_run_report.confirm_label == "Remove permanently"
    assert preview_spy.dry_run_report.payload.occurrences_removed > 0
    assert SECRET in ui.store.text, "a cancelled preview must change nothing"
    assert ui.store.history.count == events_before
    assert not preview_spy.errors

    # the sanctioned wording, and never a promise of forensic deletion
    shown = preview_spy.text_of(preview_spy.dry_run_report)
    assert "Remove permanently from scratchpad history" in shown
    assert "secure erase" not in shown.lower()
    assert "backups" in shown and "snapshots" in shown
    # without a historical selection the single-occurrence option is not offered
    assert preview_spy.choices[0].can_redact_occurrence is False
    assert preview_spy.choices[0].snippet == SECRET

    # -- and now for real ----------------------------------------------------
    refreshes = _count_refreshes(monkeypatch, ui.window.timeline)
    spy = DialogSpy(monkeypatch, choice="everywhere", confirm=True)
    ui.window._cmd_redact()

    assert [r.payload.dry_run for r in spy.reports] == [True, False]
    assert spy.final_report.heading == "Removed from scratchpad history"
    final = spy.final_report.payload
    assert final.occurrences_removed >= 2
    assert final.states_changed > 0
    assert final.events_after <= final.events_before
    assert not spy.errors

    _assert_reloaded(ui, absent=SECRET)
    assert "notes before" in ui.store.text and "notes after" in ui.store.text
    assert "key=" in ui.store.text and "again  inline" in ui.store.text

    # the timeline was re-read and now covers the rewritten history
    assert refreshes, "the timeline was never refreshed after the rewrite"
    assert ui.window.timeline.has_data() is True
    first, last = ui.store.history.time_range()
    visible_start, visible_end = ui.window.timeline.get_visible_range()
    assert visible_start <= first and visible_end >= last


# --------------------------------------------------------------------------- #
# (b) purge an attachment
# --------------------------------------------------------------------------- #

@needs_display
def test_purge_attachment_removes_token_blob_and_preview(ui, monkeypatch) -> None:
    """The token leaves history, the row and blob go, and the preview says MISSING."""
    editor = ui.window.editor
    attachment = ui.attachments.create_text(b"a captured log\n", mime="text/plain")
    token = attachment.token
    blob = ui.attachments.blob_path(attachment)
    assert blob.exists()

    editor.insert_at_cursor("see the log: ")
    editor.insert_token(token)
    time.sleep(0.002)
    editor.insert_at_cursor("\nand some more text\n")
    assert token in ui.store.text

    # the cursor preview has the VALID resolution cached before the purge
    index = editor.get_text().index(token)
    editor.select_range(index + 3, index + 3)
    assert ui.window.cursor_pane.current_token() == token
    assert ui.window.cursor_pane.current_page_name() == "text"
    assert token in editor._resolution_cache

    _select_in_editor(ui.window, token)
    assert ui.window._selected_attachment() is not None
    assert ui.window.registry.is_enabled("purge-attachment") is True

    # -- preview only --------------------------------------------------------
    preview_spy = DialogSpy(monkeypatch, confirm=False)
    ui.window._cmd_purge()
    assert len(preview_spy.reports) == 1
    assert preview_spy.dry_run_report.payload.dry_run is True
    assert preview_spy.dry_run_report.payload.occurrences_removed > 0
    assert token in ui.store.text
    assert ui.attachments.get(attachment.object_id) is not None
    assert blob.exists(), "a preview must not delete the blob"
    assert "Remove permanently from scratchpad history" in preview_spy.text_of(
        preview_spy.dry_run_report
    )

    # -- commit --------------------------------------------------------------
    refreshes = _count_refreshes(monkeypatch, ui.window.timeline)
    spy = DialogSpy(monkeypatch, confirm=True)
    ui.window._cmd_purge()

    assert [r.payload.dry_run for r in spy.reports] == [True, False]
    assert not spy.errors
    _assert_reloaded(ui, absent=token)
    assert "see the log:" in ui.store.text and "and some more text" in ui.store.text
    assert ui.attachments.get(attachment.object_id) is None, "the row must be gone"
    assert not blob.exists(), "the blob must be gone once nothing references it"
    assert refreshes

    # the token still authenticates, so it now resolves as MISSING, and the
    # preview panes must say so rather than replay a cached VALID resolution
    resolution = ui.attachments.resolve(token)
    assert resolution.state is ResolutionState.MISSING
    assert editor._resolution_for(token).state is ResolutionState.MISSING
    ui.window.cursor_pane.show_resolution(resolution, ui.attachments)
    assert ui.window.cursor_pane.current_page_name() == "missing"
    assert ui.window.cursor_pane.current_token() == token

    # the purge is not offered any more for a token whose object is gone
    editor.insert_at_cursor(f"\nstale {token}\n")
    _select_in_editor(ui.window, token)
    assert ui.window._selected_attachment() is None
    assert ui.window.registry.is_enabled("purge-attachment") is False


# --------------------------------------------------------------------------- #
# (c) redact one occurrence
# --------------------------------------------------------------------------- #

def _two_runs(ui) -> tuple[int, int]:
    """Seed two separate lives of ``SECRET``; return an instant inside each."""
    editor = ui.window.editor
    editor.insert_at_cursor("head\n")
    time.sleep(0.002)
    editor.insert_at_cursor(f"first {SECRET}\n")
    time.sleep(0.002)
    editor.insert_at_cursor("middle\n")
    inside_first = _event_walls(ui.store)[-1]
    time.sleep(0.002)

    # take the first occurrence out again: the run ends here
    text = editor.get_text()
    start = text.index(f"first {SECRET}\n")
    editor.buffer.delete(
        editor.buffer.get_iter_at_offset(start),
        editor.buffer.get_iter_at_offset(start + len(f"first {SECRET}\n")),
    )
    assert SECRET not in editor.get_text()
    time.sleep(0.002)

    editor.insert_at_end(f"second {SECRET}")
    time.sleep(0.002)
    editor.insert_at_end("tail")
    inside_second = _event_walls(ui.store)[-1]
    assert SECRET in ui.store.text
    return inside_first, inside_second


@needs_display
def test_redact_this_occurrence_needs_a_historical_selection(ui, monkeypatch) -> None:
    """Without a selected historical state the occurrence option is not offered."""
    inside_first, _inside_second = _two_runs(ui)
    _select_in_editor(ui.window, SECRET)

    spy = DialogSpy(monkeypatch, choice=None)
    ui.window._cmd_redact()
    assert spy.choices and spy.choices[0].can_redact_occurrence is False

    # and if the mode is requested anyway it aborts instead of redacting everywhere
    before = ui.store.text
    events_before = ui.store.history.count
    ui.window._start_redaction("occurrence", SECRET, None)
    assert spy.errors and "historical state" in spy.errors[0][0].lower()
    assert not spy.reports, "nothing may be previewed, let alone removed"
    assert ui.store.text == before
    assert ui.store.history.count == events_before

    # with a state selected the option comes back
    ui.window._select_time(inside_first)
    _select_in_history_view(ui.window, SECRET)
    spy2 = DialogSpy(monkeypatch, choice=None)
    ui.window._cmd_redact()
    assert spy2.choices and spy2.choices[0].can_redact_occurrence is True


@needs_display
def test_redact_this_occurrence_spares_a_later_separate_run(ui, monkeypatch) -> None:
    """The window is the life of the occurrence; a later identical run survives."""
    inside_first, inside_second = _two_runs(ui)
    assert SECRET in ui.store.history.reconstruct_at(inside_first)[0]
    assert SECRET in ui.store.history.reconstruct_at(inside_second)[0]

    ui.window._select_time(inside_first)
    _select_in_history_view(ui.window, SECRET)
    assert ui.window._redaction_target() == SECRET
    assert ui.window._occurrence_is_selectable() is True

    expected = redaction_mod.find_occurrence_window(
        ui.store.history, SECRET, inside_first
    )
    assert expected is not None
    assert expected[0] <= inside_first <= expected[1]
    assert expected[1] < inside_second, "the two runs must not overlap"

    seen: list[object] = []
    real_redact = redaction_mod.redact_text

    def spy_redact(store, text, *, within=None, dry_run=False):
        seen.append(within)
        return real_redact(store, text, within=within, dry_run=dry_run)

    monkeypatch.setattr(redaction_mod, "redact_text", spy_redact)
    spy = DialogSpy(monkeypatch, choice="occurrence", confirm=True)
    ui.window._cmd_redact()

    assert seen and all(w == expected for w in seen), f"within was {seen}, want {expected}"
    assert [r.payload.dry_run for r in spy.reports] == [True, False]
    assert spy.reports[0].payload.window == expected
    assert not spy.errors

    # the later run is still there, in the document and in its own states
    assert SECRET in ui.store.text
    assert ui.window.editor.get_text() == ui.store.text
    assert ui.window.editor.buffer.get_can_undo() is False
    assert "second" in ui.store.text and "tail" in ui.store.text

    # ... and the redacted run is gone from every state it used to be in
    for wall in _event_walls(ui.store):
        state = ui.store.history.reconstruct_at(wall)[0]
        if SECRET in state:
            assert "second " + SECRET in state, "only the later run may survive"
            assert "first " + SECRET not in state
    assert not any(
        "first " + SECRET in ui.store.history.reconstruct_at(wall)[0]
        for wall in _event_walls(ui.store)
    )


# --------------------------------------------------------------------------- #
# (d) garbage collection
# --------------------------------------------------------------------------- #

@needs_display
def test_collect_garbage_deletes_only_the_unreferenced_attachment(ui, monkeypatch) -> None:
    """A token that history still holds keeps its blob; an unused one does not."""
    referenced = ui.attachments.create_text(b"referenced payload\n", mime="text/plain")
    unreferenced = ui.attachments.create_text(b"nobody points here\n", mime="text/plain")
    referenced_blob = ui.attachments.blob_path(referenced)
    unreferenced_blob = ui.attachments.blob_path(unreferenced)

    ui.window.editor.insert_at_cursor("kept: ")
    ui.window.editor.insert_token(referenced.token)
    assert referenced.token in ui.store.text
    assert unreferenced.token not in ui.store.text

    # -- preview -------------------------------------------------------------
    preview_spy = DialogSpy(monkeypatch, confirm=False)
    ui.window._cmd_collect_garbage()
    assert len(preview_spy.reports) == 1
    preview = preview_spy.dry_run_report
    assert preview.payload.dry_run is True
    assert preview.payload.deleted_ids == [unreferenced.object_id]
    assert referenced.object_id not in preview.payload.deleted_ids
    assert ui.attachments.get(unreferenced.object_id) is not None, "a preview deletes nothing"
    assert unreferenced_blob.exists()
    assert "forensic" in preview.body.lower()

    # -- commit --------------------------------------------------------------
    spy = DialogSpy(monkeypatch, confirm=True)
    ui.window._cmd_collect_garbage()
    assert [r.payload.dry_run for r in spy.reports] == [True, False]
    assert spy.final_report.payload.deleted_ids == [unreferenced.object_id]
    assert not spy.errors

    assert ui.attachments.get(unreferenced.object_id) is None
    assert not unreferenced_blob.exists()
    assert ui.attachments.get(referenced.object_id) is not None
    assert referenced_blob.exists()
    assert referenced.token in ui.store.text

    # the caches were dropped, and the surviving token still previews
    assert ui.window.cursor_pane.current_page_name() == "blank"
    assert ui.window.editor._resolution_for(referenced.token).state is ResolutionState.VALID
    assert ui.window.editor._resolution_for(unreferenced.token).state is ResolutionState.MISSING


@needs_display
def test_collect_garbage_keeps_an_attachment_only_history_still_mentions(ui, monkeypatch) -> None:
    """Deleting the token from the current document does not orphan the blob."""
    attachment = ui.attachments.create_text(b"only in history\n", mime="text/plain")
    blob = ui.attachments.blob_path(attachment)
    editor = ui.window.editor

    editor.insert_at_cursor("tmp ")
    padded = editor.insert_token(attachment.token)
    time.sleep(0.002)
    text = editor.get_text()
    start = text.index(padded)
    editor.buffer.delete(
        editor.buffer.get_iter_at_offset(start),
        editor.buffer.get_iter_at_offset(start + len(padded)),
    )
    assert attachment.token not in ui.store.text

    spy = DialogSpy(monkeypatch, confirm=True)
    ui.window._cmd_collect_garbage()
    assert spy.final_report.payload.deleted_ids == []
    assert ui.attachments.get(attachment.object_id) is not None
    assert blob.exists(), "a state that existed for one keystroke still counts"


# --------------------------------------------------------------------------- #
# (e) the wiring around the flows
# --------------------------------------------------------------------------- #

@needs_display
def test_purge_is_not_offered_for_a_token_whose_object_is_already_gone(ui, monkeypatch) -> None:
    """A MISSING resolution must not reach ``purge_attachment`` (it would raise)."""
    attachment = ui.attachments.create_text(b"gone\n", mime="text/plain")
    ui.window.editor.insert_at_cursor("x ")
    ui.window.editor.insert_token(attachment.token)
    ui.attachments.delete(attachment.object_id)

    _select_in_editor(ui.window, attachment.token)
    assert ui.attachments.resolve(attachment.token).state is ResolutionState.MISSING
    assert ui.window._selected_attachment() is None
    assert ui.window.registry.is_enabled("purge-attachment") is False

    spy = DialogSpy(monkeypatch, choice=None)
    ui.window._cmd_purge()
    assert not spy.reports, "the purge must not start"
    ui.window._cmd_redact()
    assert spy.choices and spy.choices[0].can_purge is False

    # redacting the dangling token everywhere is still possible
    spy2 = DialogSpy(monkeypatch, choice="everywhere", confirm=True)
    ui.window._cmd_redact()
    assert [r.payload.dry_run for r in spy2.reports] == [True, False]
    assert attachment.token not in ui.store.text


@needs_display
def test_collect_garbage_explains_a_missing_module_once(ui, monkeypatch) -> None:
    """Two failed imports are one failure to the user."""
    import importlib

    spy = DialogSpy(monkeypatch, confirm=True)

    def no_module(name: str):
        raise ImportError(f"no module named {name}")

    monkeypatch.setattr(importlib, "import_module", no_module)
    ui.window._cmd_collect_garbage()
    assert len(spy.errors) == 1, spy.errors
    assert "unavailable" in spy.errors[0][0].lower()
    assert not spy.reports


@needs_display
def test_timeline_timer_survives_a_failing_refresh(ui, monkeypatch) -> None:
    """A raising refresh must not remove the GLib source for the whole session."""
    ui.window.history_panel.set_visible(True)

    def boom() -> None:
        raise RuntimeError("the history reader exploded")

    monkeypatch.setattr(ui.window.timeline, "refresh", boom)
    assert ui.window._on_timeline_timer() is True, "the timer must keep running"

    ui.window.history_panel.set_visible(False)
    assert ui.window._on_timeline_timer() is False


@needs_display
def test_toggle_raises_a_visible_but_unfocused_window(ui, monkeypatch) -> None:
    """Toggling hides only a window that already has the focus."""
    window = ui.window
    calls: list[str] = []
    monkeypatch.setattr(window, "show_window", lambda token=None: calls.append(f"show:{token}"))
    monkeypatch.setattr(window, "hide_window", lambda: calls.append("hide"))

    monkeypatch.setattr(window, "is_visible", lambda: False)
    monkeypatch.setattr(window, "is_active", lambda: False)
    assert window.toggle_window("tok") is True

    monkeypatch.setattr(window, "is_visible", lambda: True)
    monkeypatch.setattr(window, "is_active", lambda: False)
    assert window.toggle_window("tok") is True, "an unfocused window must be raised"

    monkeypatch.setattr(window, "is_active", lambda: True)
    assert window.toggle_window() is False
    assert calls == ["show:tok", "show:tok", "hide"]


# --------------------------------------------------------------------------- #
# (f) the report dialog itself
# --------------------------------------------------------------------------- #

@needs_display
def test_format_report_leads_with_the_summary_and_reads_times_as_times() -> None:
    """Spec section 26 wording first, then a table a human can read."""
    from scratchpad.ui import dialogs

    window = (1_757_000_000_000_000_000, 1_757_000_060_000_000_000)
    report = redaction_mod.RedactionReport(
        events_before=120,
        events_after=118,
        occurrences_removed=3,
        states_changed=7,
        dry_run=True,
        window=window,
    )
    rendered = dialogs.format_report(report)
    lines = rendered.splitlines()

    assert lines[0] == report.summary()
    assert "Remove permanently from scratchpad history" in rendered
    assert "secure erase" not in rendered.lower()
    assert "Events before: 120" in rendered
    assert "Preview only: yes" in rendered
    assert "1757000000000000000" not in rendered, "raw nanoseconds are not a time"
    assert "Time window: 20" in rendered and " .. 20" in rendered
    assert "dry_run" not in rendered

    gc_report = gc_mod.GcReport(scanned_events=42, referenced_ids=1, deleted_ids=[7, 9])
    rendered_gc = dialogs.format_report(gc_report)
    assert rendered_gc.splitlines()[0] == gc_report.summary()
    assert "Events replayed: 42" in rendered_gc
    assert "Attachments deleted: 2" in rendered_gc
    assert "Preview only: no" in rendered_gc

    assert dialogs.format_report(None) == "(no report)"
