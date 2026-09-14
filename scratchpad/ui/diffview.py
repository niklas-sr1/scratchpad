"""Unified diff between two document states.

`DiffView` is used by the "Compare with current" action of history mode
(`Design Specification.md` section 33).  It renders `difflib.unified_diff` into
a `LineNumberedTextView` with colored tags: additions on a green background,
deletions on a red background, hunk headers bold, file headers dim and bold.
A summary line above the diff reads e.g. "+12 / -3 lines".

The widget is purely presentational: it never mutates either text.
"""

from __future__ import annotations

import difflib

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Pango", "1.0")

from gi.repository import Gdk, Gtk, Pango  # noqa: E402

from scratchpad.ui.textview_extras import LineNumberedTextView  # noqa: E402

__all__ = ["DiffView"]

_NO_DIFFERENCES = "(no differences)"


def _rgba(r: float, g: float, b: float, a: float = 1.0) -> Gdk.RGBA:
    color = Gdk.RGBA()
    color.red, color.green, color.blue, color.alpha = r, g, b, a
    return color


def _is_dark_theme(widget: Gtk.Widget) -> bool:
    """True when the widget is drawn on a dark background."""
    try:
        import gi as _gi

        _gi.require_version("Adw", "1")
        from gi.repository import Adw

        return bool(Adw.StyleManager.get_default().get_dark())
    except Exception:
        pass
    try:  # light text implies a dark background
        fg = widget.get_color()
        return (0.299 * fg.red + 0.587 * fg.green + 0.114 * fg.blue) > 0.5
    except Exception:  # pragma: no cover - defensive
        return False


class DiffView(Gtk.Box):
    """A read-only unified diff viewer.

    Public API (consumed by `scratchpad.ui.window`):
        set_texts(old, new, *, old_label=..., new_label=...)
        clear()
        get_summary() -> str            e.g. "+12 / -3 lines"
        get_diff_text() -> str          the rendered diff body
        counts() -> (additions, deletions)
        view                            the `LineNumberedTextView`
        summary_label / labels_label    the header labels
    """

    def __init__(self, *, show_line_numbers: bool = True) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.add_css_class("diff-view")
        self.set_vexpand(True)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header.set_margin_start(8)
        header.set_margin_end(8)
        header.set_margin_top(6)

        self.summary_label = Gtk.Label(label="+0 / -0 lines", xalign=0.0)
        self.summary_label.add_css_class("heading")
        header.append(self.summary_label)

        self.labels_label = Gtk.Label(label="", xalign=0.0)
        self.labels_label.add_css_class("dim-label")
        self.labels_label.add_css_class("caption")
        self.labels_label.set_hexpand(True)
        self.labels_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        header.append(self.labels_label)
        self.append(header)

        self.view = LineNumberedTextView(editable=False, show_line_numbers=show_line_numbers)
        self.view.set_vexpand(True)
        frame = Gtk.Frame()
        frame.set_margin_start(8)
        frame.set_margin_end(8)
        frame.set_margin_bottom(8)
        frame.set_child(self.view)
        frame.set_vexpand(True)
        self.append(frame)

        self._additions = 0
        self._deletions = 0
        self._create_tags()

    # ------------------------------------------------------------------- tags

    def _create_tags(self) -> None:
        buffer = self.view.buffer
        table = buffer.get_tag_table()
        if table.lookup("diff-add") is not None:
            return
        dark = _is_dark_theme(self)
        add_bg = _rgba(0.10, 0.30, 0.14) if dark else _rgba(0.84, 0.94, 0.84)
        del_bg = _rgba(0.36, 0.12, 0.12) if dark else _rgba(0.99, 0.86, 0.86)

        tag = Gtk.TextTag(name="diff-add")
        tag.set_property("background-rgba", add_bg)
        table.add(tag)

        tag = Gtk.TextTag(name="diff-del")
        tag.set_property("background-rgba", del_bg)
        table.add(tag)

        tag = Gtk.TextTag(name="diff-hunk")
        tag.set_property("weight", Pango.Weight.BOLD)
        tag.set_property("foreground-rgba", _rgba(0.45, 0.60, 0.90) if dark else _rgba(0.20, 0.35, 0.70))
        table.add(tag)

        tag = Gtk.TextTag(name="diff-file")
        tag.set_property("weight", Pango.Weight.BOLD)
        table.add(tag)

    # ------------------------------------------------------------- public API

    def set_texts(
        self,
        old: str,
        new: str,
        *,
        old_label: str = "historical state",
        new_label: str = "current",
    ) -> None:
        """Render a unified diff of `old` vs `new` and update the summary."""
        old_lines = old.splitlines()
        new_lines = new.splitlines()
        diff_lines = list(
            difflib.unified_diff(
                old_lines,
                new_lines,
                fromfile=old_label,
                tofile=new_label,
                lineterm="",
            )
        )

        additions = sum(
            1 for line in diff_lines if line.startswith("+") and not line.startswith("+++")
        )
        deletions = sum(
            1 for line in diff_lines if line.startswith("-") and not line.startswith("---")
        )
        self._additions, self._deletions = additions, deletions
        self.summary_label.set_label(f"+{additions} / -{deletions} lines")
        self.labels_label.set_label(f"{old_label}  →  {new_label}")

        body = "\n".join(diff_lines) if diff_lines else _NO_DIFFERENCES
        self.view.set_text(body)
        if diff_lines:
            self._apply_tags(diff_lines)

    def clear(self) -> None:
        """Empty the view and reset the summary."""
        self._additions = self._deletions = 0
        self.summary_label.set_label("+0 / -0 lines")
        self.labels_label.set_label("")
        self.view.set_text("")

    def get_summary(self) -> str:
        """The summary line, e.g. "+12 / -3 lines"."""
        return self.summary_label.get_label()

    def get_diff_text(self) -> str:
        """The rendered diff body."""
        return self.view.get_text()

    def counts(self) -> tuple[int, int]:
        """(additions, deletions) of the last rendered diff."""
        return self._additions, self._deletions

    # ---------------------------------------------------------------- tagging

    def _apply_tags(self, diff_lines: list[str]) -> None:
        buffer = self.view.buffer
        for index, line in enumerate(diff_lines):
            if line.startswith("+++") or line.startswith("---"):
                name = "diff-file"
            elif line.startswith("@@"):
                name = "diff-hunk"
            elif line.startswith("+"):
                name = "diff-add"
            elif line.startswith("-"):
                name = "diff-del"
            else:
                continue
            start = buffer.get_iter_at_line(index)
            start = start[1] if isinstance(start, tuple) else start
            end = start.copy()
            if not end.ends_line():
                end.forward_to_line_end()
            buffer.apply_tag_by_name(name, start, end)
