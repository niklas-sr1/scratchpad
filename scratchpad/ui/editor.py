"""The scratchpad editor: a plain ``Gtk.TextView`` whose every mutation is logged.

The editor is deliberately dumb (spec section 2): the buffer holds exactly the
text the user sees and exactly the text the store holds.  There are no tags, no
hidden objects and no inline replacement of attachment tokens.

Mutation interception (ARCHITECTURE.md section 12)
--------------------------------------------------

``insert-text`` and ``delete-range`` are connected *before* the default handler
(plain :meth:`connect`), so the iterators still describe the document as it is
about to be changed.  That is exactly the coordinate system :class:`Op` uses::

    insert-text (iter, text)   ->  Op(INSERT, iter.get_offset(), text)
    delete-range (start, end)  ->  Op(DELETE, start.get_offset(), old_text=...)

``store.apply`` is called synchronously from inside the handler so that the log
order is the buffer order.  Two guards protect that path:

``loading``      set while the initial document (or a reloaded one after a
                 redaction) is pushed into the buffer; nothing is logged.
``_applying``    re-entrancy guard; a mutation triggered from inside a handler
                 would otherwise be logged twice or out of order.

Every programmatic edit (indent, move line, restore, token insertion, paste)
goes through the buffer inside ``begin_user_action``/``end_user_action``, so it
is intercepted like typing and lands on the undo stack as one step.

With ``SCRATCHPAD_DEBUG=1`` the editor verifies ``store.text == buffer text``
from an idle callback after each op and logs an error if they ever diverge.

Input methods
-------------

The editor's key controller runs in the CAPTURE phase because ``GtkTextView``
consumes Return and Tab in its own key handler, so a BUBBLE controller (which
GTK would run *before* the text view's own controller anyway, controllers being
run in reverse order of addition) is not an option.  Capturing means running
ahead of the input method as well, so ``GtkTextView::preedit-changed`` keeps
:attr:`ScratchpadEditor._preedit_active` up to date and the handler returns
``False`` for Return/Tab/Escape while a composition is on screen: with a CJK
candidate list, a dead key or a Compose sequence pending those keys belong to
the IM, not to the bullet continuation.

The line-editing commands at the bottom of the module are pure functions over
``str`` so that they can be tested without GTK.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Pango", "1.0")

from gi.repository import Gdk, GLib, GObject, Gtk, Pango  # noqa: E402

from scratchpad import tokens as tokens_mod  # noqa: E402
from scratchpad.core.ops import Op, OpKind  # noqa: E402

__all__ = [
    "ScratchpadEditor",
    "TextEdit",
    "DEFAULT_INDENT_WIDTH",
    "apply_edit",
    "line_start",
    "line_end",
    "block_span",
    "indent_lines",
    "outdent_lines",
    "move_lines",
    "bullet_block_range",
    "continuation_for_line",
    "empty_bullet_body",
    "pad_token",
    "font_css",
]

log = logging.getLogger(__name__)

#: Fallback indentation width; spec section 2.2 shows two-space nested bullets.
DEFAULT_INDENT_WIDTH = 2

#: Hover resolution debounce, in milliseconds.
HOVER_DEBOUNCE_MS = 40


# --------------------------------------------------------------------------- #
# pure text helpers (no GTK; tested directly)
# --------------------------------------------------------------------------- #

@dataclass(frozen=True, slots=True)
class TextEdit:
    """A replacement of ``doc[start:end]`` by ``text`` plus the resulting selection.

    ``sel_start``/``sel_end`` are offsets in the *new* document.  The editor
    performs the edit as one delete + one insert inside a single user action.
    """

    start: int
    end: int
    text: str
    sel_start: int
    sel_end: int


def apply_edit(doc: str, edit: TextEdit) -> str:
    """Return ``doc`` with ``edit`` applied (pure; used by the editor and tests)."""
    return doc[: edit.start] + edit.text + doc[edit.end :]


def line_start(text: str, offset: int) -> int:
    """Offset of the first character of the line containing ``offset``."""
    return text.rfind("\n", 0, offset) + 1


def line_end(text: str, offset: int) -> int:
    """Offset of the newline ending the line containing ``offset`` (or ``len``)."""
    index = text.find("\n", offset)
    return len(text) if index < 0 else index


def block_span(text: str, sel_start: int, sel_end: int) -> tuple[int, int]:
    """Whole-line span covering ``[sel_start, sel_end]``.

    A selection that ends exactly at the start of a line does not drag that
    line in, which is what every editor does with line-wise commands.
    """
    if sel_end < sel_start:
        sel_start, sel_end = sel_end, sel_start
    start = line_start(text, sel_start)
    if sel_end > sel_start and line_start(text, sel_end) == sel_end:
        sel_end -= 1
    return start, line_end(text, sel_end)


def _leading_whitespace(line: str) -> str:
    return line[: len(line) - len(line.lstrip(" \t"))]


def _remap(offset: int, changes: list[tuple[int, int, int]]) -> int:
    """Map an old offset through per-line prefix rewrites.

    ``changes`` is ``(line_start, old_prefix_len, new_prefix_len)`` per line, in
    document order.  An offset that sat inside a removed prefix lands on the new
    start of its line.
    """
    shift = 0
    for line_begin, old_len, new_len in changes:
        if offset < line_begin:
            break
        if offset < line_begin + old_len:
            return min(offset + shift, line_begin + shift + new_len)
        shift += new_len - old_len
    return offset + shift


def indent_lines(
    text: str, sel_start: int, sel_end: int, width: int = DEFAULT_INDENT_WIDTH
) -> TextEdit:
    """Add ``width`` spaces in front of every line touched by the selection."""
    start, end = block_span(text, sel_start, sel_end)
    pad = " " * width
    lines = text[start:end].split("\n")
    single = len(lines) == 1
    new_lines: list[str] = []
    changes: list[tuple[int, int, int]] = []
    cursor = start
    for line in lines:
        if line or single:
            new_lines.append(pad + line)
            changes.append((cursor, 0, width))
        else:
            new_lines.append(line)
        cursor += len(line) + 1
    low, high = min(sel_start, sel_end), max(sel_start, sel_end)
    return TextEdit(
        start, end, "\n".join(new_lines), _remap(low, changes), _remap(high, changes)
    )


def outdent_lines(
    text: str, sel_start: int, sel_end: int, width: int = DEFAULT_INDENT_WIDTH
) -> TextEdit:
    """Remove up to ``width`` leading spaces (or one tab) from every touched line."""
    start, end = block_span(text, sel_start, sel_end)
    lines = text[start:end].split("\n")
    new_lines: list[str] = []
    changes: list[tuple[int, int, int]] = []
    cursor = start
    for line in lines:
        if line.startswith("\t"):
            take = 1
        else:
            take = 0
            while take < width and take < len(line) and line[take] == " ":
                take += 1
        new_lines.append(line[take:])
        if take:
            changes.append((cursor, take, 0))
        cursor += len(line) + 1
    low, high = min(sel_start, sel_end), max(sel_start, sel_end)
    return TextEdit(
        start, end, "\n".join(new_lines), _remap(low, changes), _remap(high, changes)
    )


def move_lines(text: str, sel_start: int, sel_end: int, direction: int) -> TextEdit | None:
    """Move the touched lines one line up (``direction=-1``) or down (``+1``).

    Returns ``None`` when the block is already at that end of the document.
    """
    start, end = block_span(text, sel_start, sel_end)
    low, high = min(sel_start, sel_end), max(sel_start, sel_end)
    block = text[start:end]
    if direction < 0:
        if start == 0:
            return None
        prev_start = line_start(text, start - 1)
        prev = text[prev_start : start - 1]
        new_text = block + "\n" + prev
        shift = -(len(prev) + 1)
        return TextEdit(prev_start, end, new_text, low + shift, high + shift)
    if end >= len(text):
        return None
    next_end = line_end(text, end + 1)
    following = text[end + 1 : next_end]
    new_text = following + "\n" + block
    shift = len(following) + 1
    return TextEdit(start, next_end, new_text, low + shift, high + shift)


def bullet_block_range(text: str, offset: int) -> tuple[int, int]:
    """Span of the bullet block at ``offset``: its line plus deeper-indented lines.

    The trailing newline is part of the span so that deleting it removes the
    whole block instead of leaving an empty line behind.
    """
    start = line_start(text, offset)
    end = line_end(text, start)
    first = text[start:end]
    base = len(_leading_whitespace(first).expandtabs(4))
    scan = end
    while scan < len(text):
        next_start = scan + 1
        next_end = line_end(text, next_start)
        line = text[next_start:next_end]
        if not line.strip():
            # A blank line only belongs to the block if a deeper line follows.
            scan = next_end
            continue
        if len(_leading_whitespace(line).expandtabs(4)) <= base:
            break
        end = scan = next_end
    if end < len(text):
        end += 1  # swallow the newline
    elif start > 0:
        start -= 1  # last block of the document: take the newline in front instead
    return start, end


def continuation_for_line(line: str, continue_bullets: bool) -> str:
    """Text to insert after a newline so the next line continues ``line``.

    Always the leading whitespace; plus ``"- "`` when bullet continuation is on
    and ``line`` is a non-empty ``- `` bullet.  An *empty* bullet continues with
    nothing (:func:`empty_bullet_body` tells the caller to remove the marker).
    """
    indent = _leading_whitespace(line)
    if not continue_bullets:
        return indent
    rest = line[len(indent) :]
    if rest.startswith("- ") and rest[2:].strip():
        return indent + "- "
    return indent


def empty_bullet_body(line: str, continue_bullets: bool) -> tuple[int, int] | None:
    """Span *inside* ``line`` to delete when Enter is pressed on an empty bullet.

    ``"  - "`` (or ``"  -"``) collapses to ``"  "``: the marker goes, the
    indentation stays, and no newline is inserted.  ``None`` when the line is
    not an empty bullet.
    """
    if not continue_bullets:
        return None
    indent = _leading_whitespace(line)
    rest = line[len(indent) :]
    if rest.rstrip() != "-":
        return None
    return len(indent), len(line)


def pad_token(doc: str, offset: int, token: str) -> str:
    """Return ``token`` padded with spaces so that it stays a token candidate.

    A candidate is a *maximal* run of ``[0-9A-Za-z]`` of exactly 22 characters
    (spec section 20).  Inserting the token directly after (or before) other
    alphanumeric text would merge it into a longer run, and the reference would
    silently stop resolving.  So a space is added on whichever side touches an
    alphanumeric character, and nowhere else.
    """
    before = doc[offset - 1] if offset > 0 else ""
    after = doc[offset] if offset < len(doc) else ""
    prefix = " " if before.isalnum() and before.isascii() else ""
    suffix = " " if after.isalnum() and after.isascii() else ""
    return prefix + token + suffix


def font_css(font: str, selector: str) -> str:
    """Translate a Pango font description such as ``"monospace 11"`` into CSS."""
    desc = Pango.FontDescription.from_string(font or "monospace 11")
    family = desc.get_family() or "monospace"
    size = desc.get_size()
    declarations = [f'font-family: "{family}", monospace;']
    if size > 0:
        if desc.get_size_is_absolute():
            declarations.append(f"font-size: {size / Pango.SCALE:.1f}px;")
        else:
            declarations.append(f"font-size: {size / Pango.SCALE:.1f}pt;")
    weight = desc.get_weight()
    if weight and int(weight) >= int(Pango.Weight.BOLD):
        declarations.append("font-weight: bold;")
    return selector + " { " + " ".join(declarations) + " }"


# --------------------------------------------------------------------------- #
# the widget
# --------------------------------------------------------------------------- #

class ScratchpadEditor(Gtk.Box):
    """The main editing surface.

    Public API consumed by the window, the commands and the capture flows::

        get_text() / replace_all(text) / load_text(text)
        insert_at_cursor(text) / insert_at_end(text) / insert_token(token)
        cursor_offset() / selected_text() / select_range(start, end)
        grab_focus()
        undo() / redo()
        indent() / outdent() / move_line_up() / move_line_down()
        delete_bullet_block() / newline(smart=True)
        set_resolver(...) / invalidate_resolution_cache()

    Callback attributes (set by the window): ``on_hover_resolution``,
    ``on_cursor_resolution``, ``on_escape``, ``on_applied``, ``on_paste``.
    """

    def __init__(self, store, config, *, indent_width: int | None = None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.store = store
        self.config = config
        self.indent_width = indent_width if indent_width is not None else _config_indent(config)
        self.debug = bool(os.environ.get("SCRATCHPAD_DEBUG"))

        #: True while the document is being (re)loaded; suppresses logging.
        self.loading = False
        self._applying = False
        self._replace_all_old: str | None = None
        self._desynced = False
        self._debug_check_queued = False

        #: True while an input method shows a preedit (see _on_preedit_changed).
        self._preedit_active = False

        # preview plumbing
        self._resolve: Callable[[str], object] | None = None
        self._resolution_cache: dict[str, object] = {}
        self._hover_timeout: int = 0
        self._hover_offset: int | None = None
        self._hover_token: str | None = None
        self._cursor_token: str | None = None

        self.on_hover_resolution: Callable[[object | None], None] | None = None
        self.on_cursor_resolution: Callable[[object | None], None] | None = None
        self.on_escape: Callable[[], None] | None = None
        self.on_applied: Callable[[], None] | None = None
        self.on_paste: Callable[[], None] | None = None
        self.on_error: Callable[[str], None] | None = None

        self.buffer = Gtk.TextBuffer()
        self.buffer.set_enable_undo(True)
        self.buffer.set_max_undo_levels(0)  # unlimited

        self.text_view = Gtk.TextView(buffer=self.buffer)
        self.text_view.set_monospace(True)
        self.text_view.set_wrap_mode(Gtk.WrapMode.NONE)
        self.text_view.set_tabs(_tab_array(4))
        self.text_view.set_left_margin(8)
        self.text_view.set_right_margin(8)
        self.text_view.set_top_margin(6)
        self.text_view.set_bottom_margin(6)
        self.text_view.set_hexpand(True)
        self.text_view.set_vexpand(True)
        self.text_view.add_css_class("scratchpad-editor")

        self.scrolled_window = Gtk.ScrolledWindow()
        self.scrolled_window.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.scrolled_window.set_child(self.text_view)
        self.scrolled_window.set_hexpand(True)
        self.scrolled_window.set_vexpand(True)
        self.append(self.scrolled_window)

        self._install_font_css()

        # interception: plain connect, i.e. before the default handler.
        self.buffer.connect("insert-text", self._on_insert_text)
        self.buffer.connect("delete-range", self._on_delete_range)
        self.buffer.connect("notify::cursor-position", self._on_cursor_moved)
        self.text_view.connect("paste-clipboard", self._on_paste_clipboard)

        # The key controller has to run in CAPTURE: GtkTextView handles Return
        # and Tab in its own key handler, and a BUBBLE controller would never
        # see them.  Running ahead of that handler also means running ahead of
        # the input method, so the preedit state is tracked separately below and
        # the IM keys are handed back while a composition is on screen.
        self.text_view.connect("preedit-changed", self._on_preedit_changed)

        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key_pressed)
        self.text_view.add_controller(keys)

        focus = Gtk.EventControllerFocus()
        focus.connect("leave", self._on_focus_leave)
        self.text_view.add_controller(focus)

        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        motion.connect("leave", self._on_motion_leave)
        self.text_view.add_controller(motion)

        self.load_text(store.text)

    # -- font ---------------------------------------------------------------

    def _install_font_css(self) -> None:
        provider = Gtk.CssProvider()
        provider.load_from_string(font_css(getattr(self.config, "font", "monospace 11"),
                                           "textview.scratchpad-editor"))
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )

    # -- interception -------------------------------------------------------

    def _on_insert_text(
        self, buffer: Gtk.TextBuffer, location: Gtk.TextIter, text: str, length: int
    ) -> None:
        if self.loading or self._applying:
            return
        # The signal's `text` is not guaranteed to end at `length` bytes.
        raw = text.encode("utf-8")
        if 0 <= length < len(raw):
            text = raw[:length].decode("utf-8", "ignore")
        if not text:
            return
        pos = location.get_offset()
        if self._replace_all_old is not None:
            op = Op(OpKind.REPLACE, 0, text=text, old_text=self._replace_all_old)
            self._replace_all_old = None
        else:
            op = Op(OpKind.INSERT, pos, text=text)
        self._log(op)

    def _on_delete_range(
        self, buffer: Gtk.TextBuffer, start: Gtk.TextIter, end: Gtk.TextIter
    ) -> None:
        if self.loading or self._applying:
            return
        deleted = buffer.get_text(start, end, True)
        if not deleted:
            return
        if self._replace_all_old is not None:
            # The matching insert logs one REPLACE for the pair.
            return
        self._log(Op(OpKind.DELETE, start.get_offset(), old_text=deleted))

    def _log(self, op: Op) -> None:
        """Hand one op to the store, synchronously, never raising.

        ``store.apply`` can fail (a rejected op, a full disk, a record over the
        log's size limit) *after* the buffer has already changed.  The user's
        text is the truth in that situation, so the failure is reported and a
        single catch-up REPLACE is scheduled instead of reverting what was typed.
        """
        self._applying = True
        try:
            self.store.apply(op)
        except Exception as exc:  # noqa: BLE001 - a desync must not kill the UI
            log.exception("store rejected %s; scheduling a resync", op.summary())
            self._report_failure(exc)
            self._schedule_resync()
        finally:
            self._applying = False
        if self.on_applied is not None:
            self.on_applied()
        if self.debug:
            self._queue_debug_check()

    def _report_failure(self, exc: BaseException) -> None:
        if self.on_error is not None:
            self.on_error(f"Could not record an edit: {exc}")

    def _schedule_resync(self) -> None:
        if self._desynced:
            return
        self._desynced = True
        GLib.idle_add(self._resync)

    def _resync(self) -> bool:
        """Make the store catch up with the buffer after a rejected op.

        Runs from an idle callback, i.e. after the buffer mutation that failed
        has completed, so the buffer holds the text the user sees.  One
        ``REPLACE(0, store.text -> buffer text)`` restores the invariant without
        losing anything the user typed.  If even that fails the application
        keeps running; the document is still on screen and still exportable.
        """
        self._desynced = False
        buffer_text = self.get_text()
        if buffer_text == self.store.text:
            return False
        op = Op(OpKind.REPLACE, 0, text=buffer_text, old_text=self.store.text)
        self._applying = True
        try:
            self.store.apply(op)
        except Exception as exc:  # noqa: BLE001
            log.exception("resynchronising the store with the buffer failed")
            self._report_failure(exc)
            return False
        finally:
            self._applying = False
        log.warning("store resynchronised with the buffer (%d characters)", len(buffer_text))
        return False

    def _queue_debug_check(self) -> None:
        if self._debug_check_queued:
            return
        self._debug_check_queued = True
        GLib.idle_add(self._debug_check)

    def _debug_check(self) -> bool:
        self._debug_check_queued = False
        buffer_text = self.get_text()
        if buffer_text != self.store.text:
            log.error(
                "SCRATCHPAD_DEBUG: store/buffer divergence (%d vs %d chars)",
                len(self.store.text), len(buffer_text),
            )
            assert buffer_text == self.store.text, "store text diverged from the buffer"
        return False

    # -- text access --------------------------------------------------------

    def get_text(self) -> str:
        """The whole buffer contents."""
        start, end = self.buffer.get_bounds()
        return self.buffer.get_text(start, end, True)

    def load_text(self, text: str) -> None:
        """Replace the buffer without logging anything (initial load, resync).

        The undo history is cleared: stepping back across a reload would
        recreate text the store no longer has (and, after a redaction, text the
        user asked to remove).
        """
        self.loading = True
        try:
            self.buffer.begin_irreversible_action()
            self.buffer.set_text(text, -1)
            self.buffer.end_irreversible_action()
        finally:
            self.loading = False
        self.buffer.place_cursor(self.buffer.get_end_iter())
        self._resolution_cache.clear()
        self._update_cursor_preview()

    def replace_all(self, text: str) -> None:
        """Replace the whole document as one logged REPLACE (history restore)."""
        old = self.get_text()
        if old == text:
            return
        start, end = self.buffer.get_bounds()
        self.buffer.begin_user_action()
        try:
            if text:
                self._replace_all_old = old
            self.buffer.delete(start, end)
            if text:
                self.buffer.insert(self.buffer.get_start_iter(), text, -1)
        finally:
            self._replace_all_old = None
            self.buffer.end_user_action()

    def insert_at_cursor(self, text: str) -> None:
        """Insert ``text`` at the cursor as one undoable, logged step."""
        if not text:
            return
        self.buffer.begin_user_action()
        try:
            self.buffer.delete_selection(True, True)
            self.buffer.insert_at_cursor(text, -1)
        finally:
            self.buffer.end_user_action()

    def insert_at_end(self, text: str) -> None:
        """Append ``text``, separated from a non-empty document by a newline."""
        if not text:
            return
        current = self.get_text()
        prefix = "" if not current or current.endswith("\n") else "\n"
        self.buffer.begin_user_action()
        try:
            self.buffer.insert(self.buffer.get_end_iter(), prefix + text, -1)
        finally:
            self.buffer.end_user_action()
        self.buffer.place_cursor(self.buffer.get_end_iter())

    def insert_token(self, token: str) -> str:
        """Insert an attachment token at the cursor, keeping it a candidate.

        See :func:`pad_token`: a space is inserted on either side only when the
        neighbouring character would otherwise extend the alphanumeric run.
        """
        offset = self.cursor_offset()
        text = self.get_text()
        if self.buffer.get_has_selection():
            start, end = self.buffer.get_selection_bounds()
            offset = start.get_offset()
            text = text[: start.get_offset()] + text[end.get_offset() :]
        padded = pad_token(text, offset, token)
        self.insert_at_cursor(padded)
        return padded

    def cursor_offset(self) -> int:
        """Character offset of the insertion point."""
        return self.buffer.get_iter_at_mark(self.buffer.get_insert()).get_offset()

    def selected_text(self) -> str:
        """The selection, or ``""`` when there is none."""
        bounds = self.buffer.get_selection_bounds()
        if not bounds:
            return ""
        start, end = bounds
        return self.buffer.get_text(start, end, True)

    def selection_offsets(self) -> tuple[int, int]:
        """``(start, end)`` of the selection; both equal the cursor when empty."""
        bounds = self.buffer.get_selection_bounds()
        if not bounds:
            offset = self.cursor_offset()
            return offset, offset
        start, end = bounds
        return start.get_offset(), end.get_offset()

    def select_range(self, start: int, end: int) -> None:
        """Select ``[start, end)`` (a cursor move when they are equal)."""
        self.buffer.select_range(
            self.buffer.get_iter_at_offset(start), self.buffer.get_iter_at_offset(end)
        )

    def grab_focus(self) -> bool:  # type: ignore[override]
        """Focus the text view (not the box)."""
        return self.text_view.grab_focus()

    # -- editing commands ---------------------------------------------------

    def _apply_text_edit(self, edit: TextEdit | None) -> None:
        if edit is None:
            return
        buffer = self.buffer
        buffer.begin_user_action()
        try:
            start_iter = buffer.get_iter_at_offset(edit.start)
            end_iter = buffer.get_iter_at_offset(edit.end)
            buffer.delete(start_iter, end_iter)
            if edit.text:
                buffer.insert(buffer.get_iter_at_offset(edit.start), edit.text, -1)
        finally:
            buffer.end_user_action()
        self.select_range(edit.sel_start, edit.sel_end)

    def undo(self) -> None:
        """Undo one user action (an ordinary mutation as far as history goes)."""
        if self.buffer.get_can_undo():
            self.buffer.undo()

    def redo(self) -> None:
        """Redo one user action."""
        if self.buffer.get_can_redo():
            self.buffer.redo()

    def indent(self) -> None:
        """Indent the selected lines, or the current line."""
        text = self.get_text()
        start, end = self.selection_offsets()
        self._apply_text_edit(indent_lines(text, start, end, self.indent_width))

    def outdent(self) -> None:
        """Outdent the selected lines, or the current line."""
        text = self.get_text()
        start, end = self.selection_offsets()
        self._apply_text_edit(outdent_lines(text, start, end, self.indent_width))

    def move_line_up(self) -> None:
        """Move the current line or selected block one line up."""
        text = self.get_text()
        start, end = self.selection_offsets()
        self._apply_text_edit(move_lines(text, start, end, -1))

    def move_line_down(self) -> None:
        """Move the current line or selected block one line down."""
        text = self.get_text()
        start, end = self.selection_offsets()
        self._apply_text_edit(move_lines(text, start, end, 1))

    def delete_bullet_block(self) -> None:
        """Delete the current line plus every line indented deeper than it."""
        text = self.get_text()
        if not text:
            return
        start, end = bullet_block_range(text, self.cursor_offset())
        if start >= end:
            return
        self._apply_text_edit(TextEdit(start, end, "", start, start))

    def newline(self, *, smart: bool = True) -> None:
        """Insert a newline; ``smart`` keeps the indentation and bullets."""
        if not smart:
            self.insert_at_cursor("\n")
            return
        text = self.get_text()
        offset = self.cursor_offset()
        start = line_start(text, offset)
        line = text[start : line_end(text, offset)]
        continue_bullets = bool(getattr(self.config, "continue_bullets", True))
        empty = empty_bullet_body(line, continue_bullets)
        if empty is not None and offset >= start + empty[1]:
            body_start, body_end = empty
            self._apply_text_edit(
                TextEdit(start + body_start, start + body_end, "",
                         start + body_start, start + body_start)
            )
            return
        self.insert_at_cursor("\n" + continuation_for_line(line[: offset - start], continue_bullets))

    # -- keyboard -----------------------------------------------------------

    #: Keys an input method needs while it is composing: they commit, cycle the
    #: candidate list or cancel the composition instead of editing the document.
    _IM_KEYS = frozenset({
        Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_ISO_Enter,
        Gdk.KEY_Tab, Gdk.KEY_KP_Tab, Gdk.KEY_ISO_Left_Tab,
        Gdk.KEY_Escape,
    })

    def _on_preedit_changed(self, _view: Gtk.TextView, preedit: str) -> None:
        """Track whether the text view's input method is composing.

        ``GtkTextView::preedit-changed`` carries the current preedit string of
        the active :class:`Gtk.IMContext` (empty once the composition was
        committed or cancelled), which is the only public handle on that state:
        the text view keeps its IM context private.  Tracking it here lets the
        CAPTURE controller step aside for the keys the IM owns.
        """
        self._preedit_active = bool(preedit)

    def _on_focus_leave(self, _controller: Gtk.EventControllerFocus) -> None:
        """Never let a preedit flag survive the focus that produced it."""
        self._preedit_active = False

    def _on_key_pressed(
        self, _controller: Gtk.EventControllerKey, keyval: int, _keycode: int,
        state: Gdk.ModifierType,
    ) -> bool:
        if self._preedit_active and keyval in self._IM_KEYS:
            # A composition is on screen (ibus/fcitx, a dead key, Compose):
            # Enter commits it, Tab cycles candidates, Escape cancels it.  Let
            # the event through to GtkTextView, which feeds the input method.
            return False

        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        alt = bool(state & Gdk.ModifierType.ALT_MASK)

        if keyval == Gdk.KEY_Escape and not (ctrl or alt):
            if self.on_escape is not None:
                self.on_escape()
                return True
            return False
        if keyval in (Gdk.KEY_Tab, Gdk.KEY_KP_Tab) and not ctrl and not alt:
            self.indent()
            return True
        if keyval == Gdk.KEY_ISO_Left_Tab and not ctrl and not alt:
            self.outdent()
            return True
        if alt and not ctrl and keyval in (Gdk.KEY_Up, Gdk.KEY_KP_Up):
            self.move_line_up()
            return True
        if alt and not ctrl and keyval in (Gdk.KEY_Down, Gdk.KEY_KP_Down):
            self.move_line_down()
            return True
        if ctrl and shift and keyval in (Gdk.KEY_k, Gdk.KEY_K):
            self.delete_bullet_block()
            return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_ISO_Enter) and not ctrl and not alt:
            self.newline(smart=not shift)
            return True
        return False

    def _on_paste_clipboard(self, _view: Gtk.TextView) -> None:
        """Route Ctrl+V through the capture flow (large-paste guardrail, images)."""
        if self.on_paste is None:
            return
        GObject.signal_stop_emission_by_name(self.text_view, "paste-clipboard")
        self.on_paste()

    # -- previews -----------------------------------------------------------

    def set_resolver(self, resolve: Callable[[str], object] | None) -> None:
        """Install the token resolver (normally ``AttachmentStore.resolve``)."""
        self._resolve = resolve
        self._resolution_cache.clear()

    def invalidate_resolution_cache(self, token: str | None = None) -> None:
        """Drop cached resolutions (after a purge or a garbage collection)."""
        if token is None:
            self._resolution_cache.clear()
        else:
            self._resolution_cache.pop(token, None)
        self._hover_token = None
        self._cursor_token = None
        self._update_cursor_preview()

    def _resolution_for(self, token: str) -> object | None:
        if self._resolve is None:
            return None
        cached = self._resolution_cache.get(token)
        if cached is None:
            try:
                cached = self._resolve(token)
            except Exception:  # noqa: BLE001 - a preview must never break typing
                log.exception("resolving %r failed", token)
                return None
            self._resolution_cache[token] = cached
        return cached

    def _candidate_at_offset(self, offset: int) -> str | None:
        text = self.get_text()
        if offset < 0 or offset > len(text):
            return None
        found = tokens_mod.candidate_at(text, offset)
        return None if found is None else found[2]

    def _on_cursor_moved(self, *_args: object) -> None:
        self._update_cursor_preview()

    def _update_cursor_preview(self) -> None:
        if self.on_cursor_resolution is None:
            return
        token = self._candidate_at_offset(self.cursor_offset())
        if token == self._cursor_token:
            return
        self._cursor_token = token
        self.on_cursor_resolution(None if token is None else self._resolution_for(token))

    def _on_motion(self, _controller: Gtk.EventControllerMotion, x: float, y: float) -> None:
        if self.on_hover_resolution is None:
            return
        bx, by = self.text_view.window_to_buffer_coords(Gtk.TextWindowType.WIDGET, int(x), int(y))
        over, iter_ = self.text_view.get_iter_at_location(bx, by)
        offset = iter_.get_offset() if over else None
        if offset == self._hover_offset:
            return
        self._hover_offset = offset
        if self._hover_timeout:
            GLib.source_remove(self._hover_timeout)
        self._hover_timeout = GLib.timeout_add(HOVER_DEBOUNCE_MS, self._hover_tick)

    def _on_motion_leave(self, _controller: Gtk.EventControllerMotion) -> None:
        self._hover_offset = None
        if self._hover_timeout:
            GLib.source_remove(self._hover_timeout)
        self._hover_timeout = GLib.timeout_add(HOVER_DEBOUNCE_MS, self._hover_tick)

    def _hover_tick(self) -> bool:
        self._hover_timeout = 0
        if self.on_hover_resolution is None:
            return False
        offset = self._hover_offset
        token = None if offset is None else self._candidate_at_offset(offset)
        if token == self._hover_token:
            return False
        self._hover_token = token
        self.on_hover_resolution(None if token is None else self._resolution_for(token))
        return False

    # -- teardown -----------------------------------------------------------

    def shutdown(self) -> None:
        """Cancel pending timers (called when the window goes away)."""
        if self._hover_timeout:
            GLib.source_remove(self._hover_timeout)
            self._hover_timeout = 0


def _tab_array(width: int) -> Pango.TabArray:
    """A tab stop every ``width`` characters, in an approximate monospace unit."""
    tabs = Pango.TabArray.new(1, True)
    tabs.set_tab(0, Pango.TabAlign.LEFT, width * 8)
    return tabs


def _config_indent(config: object) -> int:
    """Indentation width from the config, if it ever grows such a key."""
    for name in ("indent_width", "indent_spaces", "tab_width"):
        value = getattr(config, name, None)
        if isinstance(value, int) and value > 0:
            return value
    return DEFAULT_INDENT_WIDTH
