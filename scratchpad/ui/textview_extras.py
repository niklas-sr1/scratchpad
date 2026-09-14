"""A monospace text view with a line-number gutter, search and scroll helpers.

`LineNumberedTextView` is a plain `Gtk.Box` that pairs a `Gtk.DrawingArea`
gutter with a `Gtk.TextView` inside a `Gtk.ScrolledWindow`.  The gutter draws
only the currently visible lines: it asks the text view for its visible
rectangle and for the buffer location of each line, so the numbers stay glued
to the text at any scroll offset and with any font size.

It is used for attachment text previews (see `scratchpad.ui.preview`), for the
read-only reconstructed history view and as the body of `scratchpad.ui.diffview`.

GtkSourceView is deliberately not used (it is not installed on the target
system); everything here is plain GTK 4.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")

from gi.repository import Gdk, Gtk, Pango, PangoCairo  # noqa: E402

__all__ = ["LineNumberedTextView"]

_GUTTER_PAD_LEFT = 6
_GUTTER_PAD_RIGHT = 8
_GUTTER_MIN_DIGITS = 2


def _casefold_preserving_length(text: str) -> str:
    """Lower-case `text` without ever changing its length.

    `str.lower()` can grow a string (for example 'İ'), which would break
    the mapping between search offsets and `Gtk.TextBuffer` offsets.  Characters
    whose lower-case form is not exactly one character are left untouched.
    """
    out: list[str] = []
    for ch in text:
        low = ch.lower()
        out.append(low if len(low) == 1 else ch)
    return "".join(out)


class LineNumberedTextView(Gtk.Box):
    """A read-only-by-default monospace text view with line numbers.

    Public API:
        set_text(text)                      replace the whole buffer
        get_text() -> str                   current buffer contents
        set_editable(editable)              toggle editability
        search(needle, forward=True) -> bool  case-insensitive, wrapping search
        scroll_to_start() / scroll_to_end()
        get_selected_text() -> str
        set_show_line_numbers(show)
        buffer                              the `Gtk.TextBuffer` (property)
        text_view                           the underlying `Gtk.TextView`
        scrolled_window                     the `Gtk.ScrolledWindow`
    """

    def __init__(
        self,
        *,
        editable: bool = False,
        show_line_numbers: bool = True,
        monospace: bool = True,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.add_css_class("line-numbered-text-view")

        self._buffer = Gtk.TextBuffer()
        self._buffer.set_enable_undo(False)

        self.text_view = Gtk.TextView(buffer=self._buffer)
        self.text_view.set_monospace(monospace)
        self.text_view.set_wrap_mode(Gtk.WrapMode.NONE)
        self.text_view.set_editable(editable)
        self.text_view.set_cursor_visible(True)
        self.text_view.set_left_margin(6)
        self.text_view.set_right_margin(6)
        self.text_view.set_top_margin(2)
        self.text_view.set_bottom_margin(2)
        self.text_view.set_hexpand(True)
        self.text_view.set_vexpand(True)

        self.scrolled_window = Gtk.ScrolledWindow()
        self.scrolled_window.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.scrolled_window.set_child(self.text_view)
        self.scrolled_window.set_hexpand(True)
        self.scrolled_window.set_vexpand(True)

        self.gutter = Gtk.DrawingArea()
        self.gutter.set_content_width(self._gutter_width())
        self.gutter.set_vexpand(True)
        self.gutter.set_draw_func(self._draw_gutter)
        self.gutter.set_visible(show_line_numbers)

        self.append(self.gutter)
        self.append(self.scrolled_window)

        self._show_line_numbers = show_line_numbers
        self._buffer.connect("changed", self._on_buffer_changed)
        vadj = self.scrolled_window.get_vadjustment()
        vadj.connect("value-changed", lambda *_a: self.gutter.queue_draw())
        vadj.connect("changed", lambda *_a: self.gutter.queue_draw())

    # ------------------------------------------------------------------ text

    @property
    def buffer(self) -> Gtk.TextBuffer:
        """The text buffer displayed by this widget."""
        return self._buffer

    def set_text(self, text: str) -> None:
        """Replace the whole buffer contents and scroll back to the top."""
        self._buffer.set_text(text)
        self._buffer.place_cursor(self._buffer.get_start_iter())
        self._update_gutter_width()
        self.scroll_to_start()

    def get_text(self) -> str:
        """Return the full buffer contents."""
        return self._buffer.get_text(
            self._buffer.get_start_iter(), self._buffer.get_end_iter(), False
        )

    def set_editable(self, editable: bool) -> None:
        """Allow or forbid interactive editing (read-only by default)."""
        self.text_view.set_editable(editable)

    def get_editable(self) -> bool:
        """Whether the view is currently editable."""
        return self.text_view.get_editable()

    def get_selected_text(self) -> str:
        """Return the selected text, or the empty string when nothing is selected."""
        bounds = self._buffer.get_selection_bounds()
        if not bounds:
            return ""
        start, end = bounds
        return self._buffer.get_text(start, end, False)

    def set_show_line_numbers(self, show: bool) -> None:
        """Show or hide the line-number gutter."""
        self._show_line_numbers = show
        self.gutter.set_visible(show)

    def get_line_count(self) -> int:
        """Number of lines in the buffer."""
        return self._buffer.get_line_count()

    # ---------------------------------------------------------------- search

    def search(self, needle: str, *, forward: bool = True) -> bool:
        """Search case-insensitively from the cursor, wrapping around.

        The match is selected and scrolled into view.  Returns True when a match
        was found, False for an empty needle or no match anywhere.
        """
        if not needle:
            return False
        haystack = _casefold_preserving_length(self.get_text())
        target = _casefold_preserving_length(needle)
        if not target or len(target) > len(haystack):
            return False

        insert = self._buffer.get_iter_at_mark(self._buffer.get_insert()).get_offset()
        bound = self._buffer.get_iter_at_mark(self._buffer.get_selection_bound()).get_offset()

        if forward:
            start = max(insert, bound)
            index = haystack.find(target, start)
            if index < 0:  # wrap
                index = haystack.find(target, 0)
        else:
            end = min(insert, bound)
            index = haystack.rfind(target, 0, max(end, 0))
            if index < 0:  # wrap
                index = haystack.rfind(target)
        if index < 0:
            return False

        match_start = self._buffer.get_iter_at_offset(index)
        match_end = self._buffer.get_iter_at_offset(index + len(target))
        if forward:
            self._buffer.select_range(match_end, match_start)
        else:
            self._buffer.select_range(match_start, match_end)
        self.text_view.scroll_to_mark(self._buffer.get_insert(), 0.1, False, 0.0, 0.5)
        self.gutter.queue_draw()
        return True

    # --------------------------------------------------------------- scrolling

    def scroll_to_start(self) -> None:
        """Move the cursor to the first line and scroll there."""
        start = self._buffer.get_start_iter()
        self._buffer.place_cursor(start)
        self.text_view.scroll_to_iter(start, 0.0, True, 0.0, 0.0)
        vadj = self.scrolled_window.get_vadjustment()
        vadj.set_value(vadj.get_lower())
        self.gutter.queue_draw()

    def scroll_to_end(self) -> None:
        """Move the cursor to the last line and scroll there."""
        end = self._buffer.get_end_iter()
        self._buffer.place_cursor(end)
        self.text_view.scroll_to_iter(end, 0.0, True, 0.0, 1.0)
        vadj = self.scrolled_window.get_vadjustment()
        vadj.set_value(max(vadj.get_lower(), vadj.get_upper() - vadj.get_page_size()))
        self.gutter.queue_draw()

    def scroll_to_line(self, line: int) -> None:
        """Place the cursor at `line` (0-based) and scroll it into view."""
        line = max(0, min(line, self._buffer.get_line_count() - 1))
        it = self._buffer.get_iter_at_line(line)
        it = it[1] if isinstance(it, tuple) else it
        self._buffer.place_cursor(it)
        self.text_view.scroll_to_iter(it, 0.0, True, 0.0, 0.5)
        self.gutter.queue_draw()

    # ---------------------------------------------------------------- gutter

    def _on_buffer_changed(self, _buffer: Gtk.TextBuffer) -> None:
        self._update_gutter_width()
        self.gutter.queue_draw()

    def _digits(self) -> int:
        return max(_GUTTER_MIN_DIGITS, len(str(max(1, self._buffer.get_line_count()))))

    def _font_description(self) -> Pango.FontDescription:
        desc = self.text_view.get_pango_context().get_font_description()
        return desc.copy() if desc is not None else Pango.FontDescription.from_string("monospace")

    def _digit_width(self) -> float:
        layout = self.text_view.create_pango_layout("0")
        layout.set_font_description(self._font_description())
        width, _height = layout.get_pixel_size()
        return float(width or 8)

    def _gutter_width(self) -> int:
        return int(self._digits() * self._digit_width()) + _GUTTER_PAD_LEFT + _GUTTER_PAD_RIGHT

    def _update_gutter_width(self) -> None:
        width = self._gutter_width()
        if width != self.gutter.get_content_width():
            self.gutter.set_content_width(width)

    def _draw_gutter(self, area: Gtk.DrawingArea, cr, width: int, height: int) -> None:
        """Draw the numbers of the lines that are currently visible."""
        if not self._show_line_numbers or width <= 0 or height <= 0:
            return
        tv = self.text_view
        color: Gdk.RGBA = area.get_color()

        # Faint separator between gutter and text.
        cr.set_source_rgba(color.red, color.green, color.blue, color.alpha * 0.15)
        cr.rectangle(width - 1, 0, 1, height)
        cr.fill()

        rect = tv.get_visible_rect()
        if rect.height <= 0:
            return

        found, it = tv.get_iter_at_location(0, rect.y)
        if not found:
            line_iter = tv.get_line_at_y(rect.y)
            it = line_iter[0] if isinstance(line_iter, tuple) else line_iter
        if it is None:
            return
        it.set_line_offset(0)

        font = self._font_description()
        layout = PangoCairo.create_layout(cr)
        layout.set_font_description(font)
        cr.set_source_rgba(color.red, color.green, color.blue, color.alpha * 0.55)

        bottom = rect.y + rect.height
        guard = 0
        while guard < 100_000:
            guard += 1
            loc = tv.get_iter_location(it)
            if loc.y > bottom:
                break
            _wx, wy = tv.buffer_to_window_coords(Gtk.TextWindowType.TEXT, 0, loc.y)
            if wy > height:
                break
            if wy + loc.height >= 0:
                layout.set_text(str(it.get_line() + 1), -1)
                text_w, _text_h = layout.get_pixel_size()
                cr.move_to(width - _GUTTER_PAD_RIGHT - text_w, wy)
                PangoCairo.show_layout(cr, layout)
            if not it.forward_line():
                break
