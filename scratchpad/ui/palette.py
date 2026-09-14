"""The command palette (spec section 28).

``Ctrl+Shift+P`` opens a search entry over every registered command.  Typing
filters by a case-insensitive *subsequence* match on ``"Category: Title"``, so
``rdct`` finds ``History: Redact selection from history`` and ``attach`` finds
all three attachment commands.  Each row shows its keyboard shortcut on the
right, which is how shortcuts get learned.

The palette never defines behaviour of its own: it only runs commands from the
:class:`~scratchpad.ui.commands.CommandRegistry`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, GLib, Gtk, Pango  # noqa: E402

from scratchpad.ui.commands import Command, CommandRegistry  # noqa: E402

__all__ = ["CommandPalette", "fuzzy_score", "filter_commands"]

log = logging.getLogger(__name__)


def fuzzy_score(query: str, text: str) -> int | None:
    """Score a subsequence match of ``query`` in ``text``; ``None`` when absent.

    Higher is better.  Contiguous runs and matches at word boundaries score
    extra, so the obvious candidate ends up on top without a ranking library.
    """
    if not query:
        return 0
    needle = query.casefold()
    hay = text.casefold()
    score = 0
    index = 0
    previous = -2
    for char in needle:
        if char == " ":
            continue
        found = hay.find(char, index)
        if found < 0:
            return None
        if found == previous + 1:
            score += 12
        if found == 0 or hay[found - 1] in " :-/":
            score += 8
        score += max(0, 6 - (found - index))
        previous = found
        index = found + 1
    # Prefer short labels when the score is otherwise equal.
    return score * 100 - len(text)


@dataclass(frozen=True, slots=True)
class _Hit:
    command: Command
    score: int


def filter_commands(commands: list[Command], query: str) -> list[Command]:
    """The commands matching ``query``, best first (stable for equal scores)."""
    hits: list[_Hit] = []
    for command in commands:
        score = fuzzy_score(query, command.label)
        if score is not None:
            hits.append(_Hit(command, score))
    hits.sort(key=lambda hit: -hit.score)
    return [hit.command for hit in hits]


class CommandPalette(Adw.Dialog):
    """A searchable list of every command, shown over the main window."""

    def __init__(self, registry: CommandRegistry) -> None:
        super().__init__()
        self.registry = registry
        self.set_title("Commands")
        self.set_content_width(560)
        self.set_content_height(420)
        self.set_presentation_mode(Adw.DialogPresentationMode.FLOATING)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)
        box.append(header)

        self.entry = Gtk.SearchEntry()
        self.entry.set_placeholder_text("Type a command, e.g. 'redact' or 'attach'")
        self.entry.set_margin_start(12)
        self.entry.set_margin_end(12)
        self.entry.set_margin_top(6)
        self.entry.set_margin_bottom(6)
        self.entry.connect("search-changed", self._on_search_changed)
        self.entry.connect("activate", lambda _e: self._activate_selected())
        box.append(self.entry)

        self.list_box = Gtk.ListBox()
        self.list_box.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.list_box.add_css_class("navigation-sidebar")
        self.list_box.connect("row-activated", lambda _lb, _row: self._activate_selected())

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(self.list_box)
        scroller.set_vexpand(True)
        box.append(scroller)

        self.set_child(box)

        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key_pressed)
        self.entry.add_controller(keys)

        self._rows: list[Command] = []
        self.populate("")

    # -- content -------------------------------------------------------------

    def populate(self, query: str) -> None:
        """Rebuild the list for ``query`` and select the first row."""
        while (row := self.list_box.get_first_child()) is not None:
            self.list_box.remove(row)
        self._rows = filter_commands(self.registry.all(), query)
        for command in self._rows:
            self.list_box.append(self._build_row(command))
        first = self.list_box.get_row_at_index(0)
        if first is not None:
            self.list_box.select_row(first)

    def _build_row(self, command: Command) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row.set_activatable(True)
        line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        line.set_margin_start(12)
        line.set_margin_end(12)
        line.set_margin_top(6)
        line.set_margin_bottom(6)

        title = Gtk.Label(label=command.label, xalign=0.0)
        title.set_hexpand(True)
        title.set_ellipsize(Pango.EllipsizeMode.END)
        if not self.registry.is_enabled(command.id):
            title.add_css_class("dim-label")
        line.append(title)

        accel = self.registry.accel_label(command)
        if accel:
            shortcut = Gtk.Label(label=accel, xalign=1.0)
            shortcut.add_css_class("dim-label")
            shortcut.add_css_class("monospace")
            line.append(shortcut)

        row.set_child(line)
        return row

    # -- interaction ---------------------------------------------------------

    def _on_search_changed(self, entry: Gtk.SearchEntry) -> None:
        self.populate(entry.get_text())

    def _move_selection(self, delta: int) -> None:
        if not self._rows:
            return
        current = self.list_box.get_selected_row()
        index = 0 if current is None else current.get_index() + delta
        index = max(0, min(len(self._rows) - 1, index))
        row = self.list_box.get_row_at_index(index)
        if row is not None:
            self.list_box.select_row(row)
            row.grab_focus()
            self.entry.grab_focus()

    def _activate_selected(self) -> None:
        row = self.list_box.get_selected_row()
        if row is None:
            return
        index = row.get_index()
        if not 0 <= index < len(self._rows):
            return
        command = self._rows[index]
        self.close()
        # Let the dialog finish closing before a command opens another one.
        GLib.idle_add(self._run_later, command.id)

    def _run_later(self, command_id: str) -> bool:
        try:
            self.registry.activate(command_id)
        except KeyError:  # pragma: no cover - the registry owns the ids
            log.warning("palette tried to run unknown command %r", command_id)
        return False

    def _on_key_pressed(
        self, _controller: Gtk.EventControllerKey, keyval: int, _keycode: int,
        _state: Gdk.ModifierType,
    ) -> bool:
        if keyval in (Gdk.KEY_Down, Gdk.KEY_KP_Down):
            self._move_selection(1)
            return True
        if keyval in (Gdk.KEY_Up, Gdk.KEY_KP_Up):
            self._move_selection(-1)
            return True
        if keyval in (Gdk.KEY_Page_Down,):
            self._move_selection(10)
            return True
        if keyval in (Gdk.KEY_Page_Up,):
            self._move_selection(-10)
            return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_ISO_Enter):
            self._activate_selected()
            return True
        return False

    # -- presentation --------------------------------------------------------

    def open(self, parent: Gtk.Widget) -> None:
        """Show the palette over ``parent`` with an empty query."""
        self.entry.set_text("")
        self.populate("")
        self.present(parent)
        self.entry.grab_focus()
