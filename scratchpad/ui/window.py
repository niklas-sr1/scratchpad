"""The main window: editor, preview sidebar, history panel, menu, commands.

Layout (ARCHITECTURE.md section 12)::

    +-----------------------------------------------------------+
    | [History]            Scratchpad                    [menu]  |  Adw.HeaderBar
    +----------------------------------+------------------------+
    |                                  |  Cursor (PreviewPane)  |
    |  ScratchpadEditor                +------------------------+
    |                                  |  Hover  (PreviewPane)  |
    +----------------------------------+------------------------+
    |  TimelineWidget                                            |
    |  2026-09-14 10:37:12  (event 1234)                         |  history panel,
    |  LineNumberedTextView (read only, reconstructed state)     |  collapsible
    |  [Copy text] [Restore state] [Compare] [Redact...] ...     |
    +-----------------------------------------------------------+

Both preview panes are permanent (spec section 22): they stay in place and go
blank when there is nothing to show, instead of appearing and disappearing.

The history panel is strictly read-only (spec section 33).  Everything that
leaves it -- copy, restore, compare -- is an explicit command that mutates the
*current* document as an ordinary edit, so restoring an old state is itself
recorded in history.

The window also implements the IPC handler, because every command the protocol
exposes is a window operation.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, GObject, Gtk  # noqa: E402

from scratchpad import __version__  # noqa: E402
from scratchpad.attachments import ResolutionState  # noqa: E402
from scratchpad.ui import dialogs  # noqa: E402
from scratchpad.ui.capture import CaptureController  # noqa: E402
from scratchpad.ui.commands import Command, CommandRegistry  # noqa: E402
from scratchpad.ui.diffview import DiffView  # noqa: E402
from scratchpad.ui.editor import ScratchpadEditor  # noqa: E402
from scratchpad.ui.layout import NaturalClamp  # noqa: E402
from scratchpad.ui.palette import CommandPalette  # noqa: E402
from scratchpad.ui.preview import PreviewPane  # noqa: E402
from scratchpad.ui.textview_extras import LineNumberedTextView  # noqa: E402
from scratchpad.ui.timeline import TimelineWidget  # noqa: E402

__all__ = ["ScratchpadWindow", "MENU_STRUCTURE", "format_history_header"]

log = logging.getLogger(__name__)

#: How often the timeline re-reads the history while the panel is open.
TIMELINE_REFRESH_MS = 2000

#: Debounce for the "refresh after an edit" path.
TIMELINE_DEBOUNCE_MS = 400

#: Primary menu, spec section 27 flattened into GMenu submenus and sections.
MENU_STRUCTURE: tuple[tuple[str, tuple[str | None, ...]], ...] = (
    ("File", ("export", "clear", None, "hide-window", "quit")),
    ("Edit", (
        "undo", "redo", None,
        "indent", "outdent", None,
        "move-block-up", "move-block-down", "delete-block",
    )),
    ("Insert", (
        "paste", "paste-as-attachment", None,
        "attach-file", "attach-clipboard-image", "screenshot",
    )),
    ("History", (
        "toggle-history", None,
        "history-copy-text", "history-copy-selection",
        "history-restore-selection", "history-restore-state", "history-compare", None,
        "redact-selection", "purge-attachment", "collect-garbage",
    )),
    ("View", ("toggle-history", "toggle-previews")),
    ("Commands", ("command-palette", "install-shortcut")),
)


def _clamp_position(paned: "Gtk.Paned", wanted: int, minimum: int) -> int:
    """Keep a paned position inside what the children's minimum sizes allow."""
    maximum = paned.get_property("max-position")
    lowest = max(minimum, paned.get_property("min-position"))
    if maximum <= 0:
        return max(wanted, lowest)
    return max(min(wanted, maximum), min(lowest, maximum))


def format_history_header(wall_ns: int | None, seq: int) -> str:
    """``"2026-09-14 10:37:12  (event 1234)"`` in local time."""
    if wall_ns is None or seq < 0:
        return "No historical state selected"
    stamp = datetime.fromtimestamp(wall_ns / 1_000_000_000).strftime("%Y-%m-%d %H:%M:%S")
    return f"{stamp}  (event {seq})"


class ScratchpadWindow(Adw.ApplicationWindow):
    """The one window of the application."""

    def __init__(self, application, *, store, config, attachments, codec) -> None:
        super().__init__(application=application)
        self.store = store
        self.config = config
        self.attachments = attachments
        self.codec = codec

        self._history_text = ""
        self._history_seq = -1
        self._history_wall_ns: int | None = None
        self._timeline_timer = 0
        self._timeline_debounce = 0

        self.set_default_size(int(config.width), int(config.height))
        self.set_title("Scratchpad")

        self.editor = ScratchpadEditor(store, config)
        self.capture = CaptureController(self, self.editor, attachments, config)
        self.registry = CommandRegistry(application, self)
        self.palette: CommandPalette | None = None

        self._build_ui()
        self._register_commands()
        self._wire_editor()

        self.connect("close-request", self._on_close_request)
        self.registry.refresh()

    # -- construction --------------------------------------------------------

    def _build_ui(self) -> None:
        self.toasts = Adw.ToastOverlay()
        toolbar = Adw.ToolbarView()
        self.toasts.set_child(toolbar)
        self.set_content(self.toasts)

        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title="Scratchpad", subtitle=""))

        self.history_button = Gtk.ToggleButton(label="History")
        self.history_button.set_tooltip_text("Show the timeline and historical states (Ctrl+H)")
        self.history_button.connect("toggled", self._on_history_toggled)
        header.pack_start(self.history_button)

        self.menu_button = Gtk.MenuButton()
        self.menu_button.set_icon_name("open-menu-symbolic")
        self.menu_button.set_tooltip_text("Main menu")
        header.pack_end(self.menu_button)
        toolbar.add_top_bar(header)

        # Persistent warning for storage trouble (store.last_io_error).
        self.banner = Adw.Banner()
        self.banner.set_revealed(False)
        toolbar.add_top_bar(self.banner)

        # editor | previews
        # Homogeneous: the two panes always split the sidebar 50/50, whatever
        # they show.  Each pane clamps its own natural size (preview.py).
        self.sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.sidebar.set_homogeneous(True)
        self.sidebar.set_size_request(280, -1)
        self.sidebar.set_margin_start(6)
        self.sidebar.set_margin_end(6)
        self.sidebar.set_margin_top(6)
        self.sidebar.set_margin_bottom(6)
        self.cursor_pane = PreviewPane("Cursor")
        self.hover_pane = PreviewPane("Hover")
        for pane in (self.cursor_pane, self.hover_pane):
            frame = Gtk.Frame()
            frame.set_child(pane)
            frame.set_vexpand(True)
            self.sidebar.append(frame)

        self.top_paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self.top_paned.set_start_child(self.editor)
        self.top_paned.set_end_child(self.sidebar)
        self.top_paned.set_resize_start_child(True)
        self.top_paned.set_resize_end_child(False)
        self.top_paned.set_shrink_start_child(False)
        self.top_paned.set_shrink_end_child(False)
        self.top_paned.set_position(int(self.config.width * 0.6))

        self.main_paned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
        self.main_paned.set_start_child(self.top_paned)
        # Clamped so that showing the panel divides the current window height
        # instead of growing the window to the panel's natural size.
        self.history_container = NaturalClamp(self._build_history_panel())
        self.history_panel.bind_property(
            "visible", self.history_container, "visible", GObject.BindingFlags.SYNC_CREATE
        )
        self.main_paned.set_end_child(self.history_container)
        self.main_paned.set_resize_start_child(True)
        self.main_paned.set_resize_end_child(True)
        self.main_paned.set_shrink_start_child(False)
        self.main_paned.set_shrink_end_child(False)
        self.main_paned.set_position(int(self.config.height * 0.5))
        self.history_panel.set_visible(False)
        toolbar.set_content(self.main_paned)
        # The compositor decides the real size, and it does so after the window
        # is mapped, so the splits are placed on the first allocation instead of
        # from `config.width`: the editor gets the space, the sidebar about a
        # third of it and never less than its own request.  ``max-position``
        # changes exactly when the paned is allocated.
        self._sized_top = False
        self._sized_main = False
        self.top_paned.connect("notify::max-position", self._on_top_allocated)
        self.main_paned.connect("notify::max-position", self._on_main_allocated)

    def _on_top_allocated(self, paned: Gtk.Paned, _param: object) -> None:
        width = paned.get_width()
        if self._sized_top or width < 400:
            return
        self._sized_top = True
        wanted = width - max(300, int(width * 0.34))
        paned.set_position(_clamp_position(paned, wanted, 320))

    def _on_main_allocated(self, paned: Gtk.Paned, _param: object) -> None:
        height = paned.get_height()
        if self._sized_main or height < 200:
            return
        self._sized_main = True
        paned.set_position(_clamp_position(paned, int(height * 0.5), 120))

    def _build_history_panel(self) -> Gtk.Widget:
        self.history_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.history_panel.set_margin_start(6)
        self.history_panel.set_margin_end(6)
        self.history_panel.set_margin_bottom(6)

        self.timeline = TimelineWidget()
        self.timeline.set_history(self.store.history)
        self.timeline.connect("time-selected", self._on_time_selected)
        self.history_panel.append(self.timeline)

        self.history_header = Gtk.Label(label=format_history_header(None, -1), xalign=0.0)
        self.history_header.add_css_class("heading")
        self.history_header.add_css_class("monospace")
        self.history_panel.append(self.history_header)

        self.history_view = LineNumberedTextView(editable=False)
        self.history_view.set_vexpand(True)
        self.history_view.buffer.connect(
            "notify::has-selection", lambda *_a: self.registry.refresh()
        )
        frame = Gtk.Frame()
        frame.set_child(self.history_view)
        frame.set_vexpand(True)
        self.history_panel.append(frame)

        self.history_buttons = Gtk.FlowBox()
        self.history_buttons.set_selection_mode(Gtk.SelectionMode.NONE)
        self.history_buttons.set_max_children_per_line(8)
        self.history_buttons.set_column_spacing(4)
        self.history_buttons.set_row_spacing(4)
        self.history_panel.append(self.history_buttons)
        return self.history_panel

    def _add_history_button(self, label: str, command_id: str) -> None:
        button = Gtk.Button(label=label)
        button.set_action_name(f"win.{command_id}")
        self.history_buttons.append(button)

    def _wire_editor(self) -> None:
        self.editor.set_resolver(self.attachments.resolve)
        self.editor.on_cursor_resolution = lambda res: self.cursor_pane.show_resolution(
            res, self.attachments
        )
        self.editor.on_hover_resolution = lambda res: self.hover_pane.show_resolution(
            res, self.attachments
        )
        self.editor.on_escape = self.hide_window
        self.editor.on_paste = self.capture.paste_normal
        self.editor.on_applied = self._on_document_changed
        self.editor.on_error = self.toast
        # "Redact selection" depends on the editor selection too, so the menu
        # and palette must re-evaluate when it changes.
        self.editor.buffer.connect(
            "notify::has-selection", lambda *_a: self.registry.refresh()
        )

    # -- commands ------------------------------------------------------------

    def _register_commands(self) -> None:
        editor = self.editor
        add = self.registry.add
        add(Command("undo", "Undo", "Edit", editor.undo, "<Control>z"))
        add(Command("redo", "Redo", "Edit", editor.redo, "<Control><Shift>z"))
        # Tab, Alt+Up/Down and Ctrl+Shift+K are handled by the editor's own key
        # controller; registering them as window accelerators would steal them
        # from every other focusable widget (the palette's entry, for instance).
        add(Command("indent", "Indent", "Edit", editor.indent, "Tab", register_accel=False))
        add(Command("outdent", "Outdent", "Edit", editor.outdent, "<Shift>Tab",
                    register_accel=False))
        add(Command("move-block-up", "Move block up", "Edit", editor.move_line_up,
                    "<Alt>Up", register_accel=False))
        add(Command("move-block-down", "Move block down", "Edit", editor.move_line_down,
                    "<Alt>Down", register_accel=False))
        add(Command("delete-block", "Delete current block", "Edit",
                    editor.delete_bullet_block, "<Control><Shift>k", register_accel=False))

        add(Command("paste", "Paste inline", "Insert", self.capture.paste_normal,
                    "<Control>v", register_accel=False))
        add(Command("paste-as-attachment", "Paste as attachment", "Insert",
                    self.capture.paste_as_attachment, "<Control><Shift>v"))
        add(Command("attach-file", "Attach file...", "Insert", self.capture.attach_file,
                    "<Control><Shift>a"))
        add(Command("attach-clipboard-image", "Attach clipboard image", "Insert",
                    self.capture.attach_clipboard_image))
        add(Command("screenshot", "Screenshot", "Insert", self._cmd_screenshot,
                    "<Control><Shift>s"))

        add(Command("export", "Export current state...", "File", self._cmd_export,
                    "<Control>e"))
        add(Command("clear", "Clear scratchpad", "File", self._cmd_clear))
        add(Command("hide-window", "Hide window", "File", self.hide_window, "Escape",
                    register_accel=False))
        add(Command("quit", "Quit", "File", self._cmd_quit, "<Control>q", scope="app"))

        add(Command("toggle-history", "Toggle history panel", "View",
                    self.toggle_history_panel, "<Control>h"))
        add(Command("toggle-previews", "Toggle preview panes", "View",
                    self.toggle_previews))
        add(Command("command-palette", "Command palette", "Commands",
                    self.open_palette, "<Control><Shift>p"))
        add(Command("install-shortcut", "Install global shortcut...", "Commands",
                    self._cmd_install_shortcut))

        has_state = lambda: self._history_seq >= 0  # noqa: E731
        has_selection = lambda: bool(self._history_selection())  # noqa: E731
        add(Command("history-copy-text", "Copy historical text", "History",
                    self._cmd_copy_history_text, enabled=has_state))
        add(Command("history-copy-selection", "Copy selection", "History",
                    self._cmd_copy_history_selection, enabled=has_selection))
        add(Command("history-restore-selection", "Restore selection at cursor", "History",
                    self._cmd_restore_selection, enabled=has_selection))
        add(Command("history-restore-state", "Restore entire state", "History",
                    self._cmd_restore_state, enabled=has_state))
        add(Command("history-compare", "Compare with current", "History",
                    self._cmd_compare, enabled=has_state))
        add(Command("redact-selection", "Redact selection from history...", "History",
                    self._cmd_redact, enabled=lambda: bool(self._redaction_target())))
        add(Command("purge-attachment", "Purge attachment from history...", "History",
                    self._cmd_purge, enabled=lambda: self._selected_attachment() is not None))
        add(Command("collect-garbage", "Collect unreferenced attachments...", "History",
                    self._cmd_collect_garbage))

        self.menu_button.set_menu_model(self.registry.build_menu(MENU_STRUCTURE))
        for label, command_id in (
            ("Copy text", "history-copy-text"),
            ("Copy selection", "history-copy-selection"),
            ("Restore selection at cursor", "history-restore-selection"),
            ("Restore entire state", "history-restore-state"),
            ("Compare with current", "history-compare"),
            ("Redact selection...", "redact-selection"),
            ("Purge attachment...", "purge-attachment"),
            ("Collect unreferenced attachments...", "collect-garbage"),
        ):
            self._add_history_button(label, command_id)

    # -- window visibility ---------------------------------------------------

    def show_window(self, activation_token: str | None = None) -> None:
        """Present the window and put the caret in the editor.

        ``activation_token`` is the ``XDG_ACTIVATION_TOKEN`` of the process that
        asked for the window; handing it to GTK is what lets the compositor give
        us the focus instead of flagging the window as demanding attention.
        """
        if activation_token:
            self.set_startup_id(activation_token)
        self.present()
        self.editor.grab_focus()

    def hide_window(self) -> None:
        """Hide the window; the process keeps running to serve the toggle."""
        self.set_visible(False)

    def toggle_window(self, activation_token: str | None = None) -> bool:
        """Hide when visible *and* focused, show otherwise.  Returns the new state.

        Visibility alone is not enough: a window that is mapped on another
        workspace, behind other windows or minimised is still "visible" to GTK,
        and hiding it there is the opposite of what pressing the global toggle
        means.  Only a window that already has the focus is hidden; everything
        else is raised and focused.
        """
        if self.is_visible() and self.is_active():
            self.hide_window()
            return False
        self.show_window(activation_token)
        return True

    def _on_close_request(self, _window: Gtk.Window) -> bool:
        """Closing the window only hides it (spec section 3: a drop-down pad)."""
        self.hide_window()
        return True

    # -- panels --------------------------------------------------------------

    def toggle_history_panel(self) -> None:
        """Show or hide the history panel (also flips the header toggle)."""
        self.history_button.set_active(not self.history_button.get_active())

    def toggle_previews(self) -> None:
        """Show or hide the preview sidebar."""
        self.sidebar.set_visible(not self.sidebar.get_visible())

    def _on_history_toggled(self, button: Gtk.ToggleButton) -> None:
        visible = button.get_active()
        self.history_panel.set_visible(visible)
        if visible:
            self.timeline.refresh()
            if self._history_wall_ns is None:
                _first, last = self.store.history.time_range()
                if last:
                    self.timeline.set_selected(last)
                    self._select_time(last)
            if not self._timeline_timer:
                self._timeline_timer = GLib.timeout_add(
                    TIMELINE_REFRESH_MS, self._on_timeline_timer
                )
        elif self._timeline_timer:
            GLib.source_remove(self._timeline_timer)
            self._timeline_timer = 0
        self.registry.refresh()

    def _on_timeline_timer(self) -> bool:
        if not self.history_panel.get_visible():
            self._timeline_timer = 0
            return False
        # A failing refresh must not remove the source: GLib drops a callback
        # that raises, and the timeline would then stay frozen for the rest of
        # the session with nothing but a traceback to say why.
        try:
            self.timeline.refresh()
        except Exception:  # noqa: BLE001
            log.exception("refreshing the timeline failed")
        return True

    def _on_document_changed(self) -> None:
        """Called after every logged op: keep the timeline roughly live."""
        if not self.history_panel.get_visible() or self._timeline_debounce:
            return
        self._timeline_debounce = GLib.timeout_add(
            TIMELINE_DEBOUNCE_MS, self._on_timeline_debounce
        )

    def _on_timeline_debounce(self) -> bool:
        self._timeline_debounce = 0
        if self.history_panel.get_visible():
            self.timeline.refresh()
        return False

    # -- history -------------------------------------------------------------

    def _on_time_selected(self, _widget: TimelineWidget, wall_ns: int) -> None:
        self._select_time(int(wall_ns))

    def _select_time(self, wall_ns: int) -> None:
        try:
            text, seq = self.store.history.reconstruct_at(wall_ns)
        except Exception as exc:  # noqa: BLE001 - a broken replay must not crash the UI
            log.exception("reconstructing the state at %d failed", wall_ns)
            dialogs.error(self, "Could not reconstruct that state", str(exc))
            return
        self._history_text = text
        self._history_seq = seq
        self._history_wall_ns = wall_ns
        self.history_header.set_label(format_history_header(wall_ns, seq))
        self.history_view.set_text(text)
        self.registry.refresh()

    def _history_selection(self) -> str:
        return self.history_view.get_selected_text()

    def _redaction_target(self) -> str:
        """What "Redact selection" would remove: the history selection, else the editor's."""
        return self._history_selection() or self.editor.selected_text()

    def _selected_attachment(self):
        """The resolution of the selection when it is exactly a *live* token.

        MISSING is deliberately excluded: the token authenticates but the row is
        already gone, so a purge would only raise ``LookupError``.  Redacting
        such a token everywhere still works, it is simply not a "purge".
        """
        candidate = self._redaction_target().strip()
        if len(candidate) != 22 or not candidate.isalnum() or not candidate.isascii():
            return None
        try:
            resolution = self.attachments.resolve(candidate)
        except Exception:  # noqa: BLE001
            log.exception("resolving %r failed", candidate)
            return None
        if resolution.state is not ResolutionState.VALID or resolution.object_id is None:
            return None
        return resolution

    def _cmd_copy_history_text(self) -> None:
        self.get_clipboard().set(self._history_text)
        self.toast("Historical text copied.")

    def _cmd_copy_history_selection(self) -> None:
        selection = self._history_selection()
        if not selection:
            return
        self.get_clipboard().set(selection)
        self.toast("Selection copied.")

    def _cmd_restore_selection(self) -> None:
        selection = self._history_selection()
        if not selection:
            return
        self.editor.insert_at_cursor(selection)
        self.editor.grab_focus()
        self.toast("Historical selection inserted at the cursor.")

    def _cmd_restore_state(self) -> None:
        if self._history_seq < 0:
            return
        text = self._history_text
        dialogs.confirm(
            self,
            "Restore this historical state?",
            "The current document is replaced by the state of "
            f"{format_history_header(self._history_wall_ns, self._history_seq)}.\n\n"
            "Nothing is lost: the replacement is recorded as an ordinary edit, so the "
            "document you have now stays reachable in the history.",
            confirm_label="Restore",
            destructive=False,
            on_confirm=lambda: self._do_restore_state(text),
        )

    def _do_restore_state(self, text: str) -> None:
        self.editor.replace_all(text)
        self.editor.grab_focus()
        self.toast("Historical state restored.")

    def _cmd_compare(self) -> None:
        if self._history_seq < 0:
            return
        view = DiffView()
        view.set_texts(
            self._history_text,
            self.editor.get_text(),
            old_label=format_history_header(self._history_wall_ns, self._history_seq),
            new_label="current",
        )
        dialog = Adw.Dialog()
        dialog.set_title("Compare with current")
        dialog.set_content_width(880)
        dialog.set_content_height(620)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        header = Adw.HeaderBar()
        box.append(header)
        box.append(view)
        dialog.set_child(box)
        dialog.present(self)

    # -- destructive operations ---------------------------------------------

    def _redaction_module(self, name: str = "redaction", *, quiet: bool = False):
        """Import ``scratchpad.redaction``/``scratchpad.gc`` lazily.

        The module is written by another agent and may be absent in a partial
        checkout; that must degrade into an explained dialog, never a traceback.
        ``quiet=True`` suppresses the dialog for a caller that tries more than
        one module and reports the failure once itself.
        """
        import importlib

        try:
            return importlib.import_module(f"scratchpad.{name}")
        except Exception as exc:  # noqa: BLE001
            log.warning("scratchpad.%s is unavailable: %s", name, exc)
            if not quiet:
                dialogs.error(
                    self,
                    "This operation is unavailable",
                    f"The history rewrite module could not be loaded: {exc}",
                )
            return None

    def _cmd_redact(self) -> None:
        target = self._redaction_target()
        if not target:
            return
        resolution = self._selected_attachment()
        dialogs.redaction_choice(
            self,
            snippet=target,
            can_purge=resolution is not None,
            # "This historical occurrence" needs an instant to bound the
            # occurrence with.  Without a selection inside a reconstructed
            # state there is none, and offering the option anyway would turn
            # the least destructive choice into the most destructive one.
            can_redact_occurrence=self._occurrence_is_selectable(),
            on_choice=lambda mode: self._start_redaction(mode, target, resolution),
        )

    def _occurrence_is_selectable(self) -> bool:
        """Whether "redact this occurrence" has a historical state to work on."""
        return self._history_wall_ns is not None and bool(self._history_selection())

    def _occurrence_window(self, module, text: str) -> tuple[int, int] | None:
        """Wall clock bounds of the occurrence the history selection points at.

        Returns ``None`` after explaining itself, in which case the caller must
        abort: an unbounded "occurrence" redaction is a redaction everywhere.
        """
        if not self._occurrence_is_selectable():
            dialogs.error(
                self,
                "Select the occurrence in a historical state",
                "Redacting a single occurrence needs a selection inside a reconstructed "
                "state: open the history panel, pick a time, and select the text there. "
                "Without one, only \"Redact this exact content everywhere\" can be used.",
            )
            return None
        at_wall_ns = int(self._history_wall_ns or 0)
        finder = getattr(module, "find_occurrence_window", None)
        if not callable(finder):  # pragma: no cover - contract guarantees it
            return (at_wall_ns, at_wall_ns)
        try:
            found = finder(self.store.history, text, at_wall_ns)
        except Exception as exc:  # noqa: BLE001
            log.exception("locating the occurrence failed")
            dialogs.error(self, "Nothing was changed",
                          f"Locating that occurrence failed: {exc}")
            return None
        if found is None:
            dialogs.error(
                self,
                "That occurrence is not in this state",
                "The selected text is not part of the historical state shown here, so "
                "there is no single occurrence to remove. Use \"Redact this exact "
                "content everywhere\" instead.",
            )
            return None
        return (int(found[0]), int(found[1]))

    def _cmd_purge(self) -> None:
        resolution = self._selected_attachment()
        if resolution is None:
            return
        self._start_redaction("purge", self._redaction_target(), resolution)

    def _start_redaction(self, mode: str, text: str, resolution) -> None:
        module = self._redaction_module()
        if module is None:
            return
        within = None
        if mode == dialogs.REDACT_OCCURRENCE:
            # The window covers the whole life of the occurrence the user
            # pointed at; states outside it keep the text.  No window means no
            # occurrence to remove, and *not* "remove everywhere".
            within = self._occurrence_window(module, text)
            if within is None:
                return

        def run(dry_run: bool):
            if mode == dialogs.REDACT_PURGE:
                return module.purge_attachment(
                    self.store, self.attachments, resolution.object_id, dry_run=dry_run
                )
            return module.redact_text(self.store, text, within=within, dry_run=dry_run)

        try:
            preview = run(True)
        except Exception as exc:  # noqa: BLE001
            log.exception("the redaction dry run failed")
            dialogs.error(self, "Nothing was changed", f"The dry run failed: {exc}")
            return

        def commit() -> None:
            try:
                final = run(False)
            except Exception as exc:  # noqa: BLE001
                log.exception("the redaction failed")
                dialogs.error(self, "The redaction failed", str(exc))
                self._reload_after_rewrite()
                return
            token = resolution.token if resolution is not None else None
            self._reload_after_rewrite(token)
            dialogs.report(self, "Removed from scratchpad history",
                           "The history has been rewritten.", final)

        dialogs.report(
            self,
            dialogs.REDACT_HEADING,
            f"{dialogs.REDACT_CAVEAT}\n\nThis is what would change:",
            preview,
            confirm_label="Remove permanently",
            on_confirm=commit,
        )

    def _cmd_collect_garbage(self) -> None:
        # Two failed imports are one failure to the user, so both are quiet and
        # the single explanation is given here.
        module = (
            self._redaction_module("gc", quiet=True)
            or self._redaction_module("redaction", quiet=True)
        )
        if module is None or not hasattr(module, "collect_garbage"):
            dialogs.error(
                self,
                "This operation is unavailable",
                "The attachment garbage collector could not be loaded.",
            )
            return

        def run(dry_run: bool):
            return module.collect_garbage(
                self.store, self.attachments, self.codec, dry_run=dry_run
            )

        try:
            preview = run(True)
        except Exception as exc:  # noqa: BLE001
            log.exception("the garbage collection dry run failed")
            dialogs.error(self, "Nothing was changed", f"The dry run failed: {exc}")
            return

        def commit() -> None:
            try:
                final = run(False)
            except Exception as exc:  # noqa: BLE001
                log.exception("the garbage collection failed")
                dialogs.error(self, "The collection failed", str(exc))
                return
            self.editor.invalidate_resolution_cache()
            self.cursor_pane.clear()
            self.hover_pane.clear()
            dialogs.report(self, "Unreferenced attachments collected",
                           "Blobs that no retained historical state references are gone.",
                           final)

        dialogs.report(
            self,
            "Collect unreferenced attachments",
            "Attachments that no retained historical state references can be deleted.\n\n"
            "This is not a forensic deletion; copies may survive in backups and "
            "filesystem snapshots.\n\nThis is what would be removed:",
            preview,
            confirm_label="Remove permanently",
            on_confirm=commit,
        )

    def _reload_after_rewrite(self, token: str | None = None) -> None:
        """Re-read the document after the history was rewritten underneath us."""
        self.editor.load_text(self.store.text)
        self.editor.invalidate_resolution_cache(token)
        self.cursor_pane.clear()
        self.hover_pane.clear()
        self._history_text = ""
        self._history_seq = -1
        self._history_wall_ns = None
        self.history_header.set_label(format_history_header(None, -1))
        self.history_view.set_text("")
        self.timeline.set_history(self.store.history)
        self.registry.refresh()

    # -- plain commands ------------------------------------------------------

    def _cmd_clear(self) -> None:
        if not self.editor.get_text():
            return
        dialogs.confirm(
            self,
            "Clear the scratchpad?",
            "The document becomes empty. This is an ordinary edit: the current "
            "contents stay reachable in the history until they are redacted.",
            confirm_label="Clear",
            on_confirm=self._do_clear,
        )

    def _do_clear(self) -> None:
        self.editor.replace_all("")
        self.editor.grab_focus()
        self.toast("Scratchpad cleared.")

    def _cmd_export(self) -> None:
        dialog = Gtk.FileDialog()
        dialog.set_title("Export current state")
        dialog.set_initial_name(
            datetime.now().strftime("scratchpad-%Y%m%d-%H%M%S.txt")
        )
        text = self.editor.get_text()

        def done(source: Gtk.FileDialog, result) -> None:
            try:
                gfile = source.save_finish(result)
            except GLib.Error as exc:
                if exc.code != Gtk.DialogError.DISMISSED:
                    dialogs.error(self, "Export failed", exc.message)
                return
            path = gfile.get_path() if gfile is not None else None
            if path is None:
                dialogs.error(self, "Export failed", "Only local files can be written.")
                return
            try:
                Path(path).write_text(text, encoding="utf-8")
            except OSError as exc:
                dialogs.error(self, "Export failed", str(exc))
                return
            self.toast(f"Exported to {Path(path).name}.")

        dialog.save(self, None, done)

    def _cmd_screenshot(self) -> None:
        self.capture.screenshot()

    def _cmd_quit(self) -> None:
        application = self.get_application()
        if application is not None:
            application.quit()

    def _cmd_install_shortcut(self) -> None:
        from scratchpad import cli

        default = getattr(cli, "DEFAULT_SHORTCUT_KEY", "Super+grave")
        dialogs.ask_text(
            self,
            "Install the global toggle shortcut",
            "The key combination that shows and hides the scratchpad. On COSMIC this "
            "is written to the custom shortcut file (the previous one is backed up).",
            initial=default,
            placeholder="Super+grave",
            confirm_label="Install",
            on_accept=self._do_install_shortcut,
        )

    def _do_install_shortcut(self, key_spec: str, *, replace: bool = False) -> None:
        from scratchpad import cli

        key_spec = key_spec or getattr(cli, "DEFAULT_SHORTCUT_KEY", "Super+grave")
        desktop = os.environ.get("XDG_CURRENT_DESKTOP", "")
        if "cosmic" not in desktop.lower():
            instructions = getattr(cli, "_desktop_instructions", None)
            body = (
                instructions(key_spec, cli.SHORTCUT_COMMAND)
                if callable(instructions)
                else f"Bind {key_spec} to '{cli.SHORTCUT_COMMAND}' in your desktop settings."
            )
            dialogs.info(self, "Set the shortcut in your desktop settings", body)
            return
        conflict = getattr(cli, "ShortcutConflict", None)
        try:
            summary = cli.install_cosmic_shortcut(
                key_spec, **({"replace": True} if replace else {})
            )
        except TypeError:  # pragma: no cover - an older cli without `replace`
            try:
                summary = cli.install_cosmic_shortcut(key_spec)
            except Exception as exc:  # noqa: BLE001
                dialogs.error(self, "Could not install the shortcut", str(exc))
                return
        except Exception as exc:  # noqa: BLE001
            # A key already bound to a different command is the one failure the
            # user can resolve, by taking the binding over.
            is_conflict = conflict is not None and isinstance(exc, conflict)
            if replace or not is_conflict:
                dialogs.error(self, "Could not install the shortcut", str(exc))
                return
            dialogs.confirm(
                self,
                "That key is already bound",
                f"{exc}\n\nReplace the existing binding with "
                f"'{cli.SHORTCUT_COMMAND}'?",
                confirm_label="Replace",
                on_confirm=lambda: self._do_install_shortcut(key_spec, replace=True),
            )
            return
        dialogs.info(self, "Global shortcut installed", str(summary))

    def open_palette(self) -> None:
        """Open the command palette (Ctrl+Shift+P)."""
        self.registry.refresh()
        self.palette = CommandPalette(self.registry)
        self.palette.open(self)

    def toast(self, message: str) -> None:
        """Show a transient message in the window."""
        self.toasts.add_toast(Adw.Toast(title=message, timeout=3))

    def show_storage_warning(self, message: str | None) -> None:
        """Show (or clear) the persistent banner about storage trouble.

        Driven by the application's 250 ms tick from ``store.last_io_error``:
        a failed fsync or heartbeat must be visible, because from then on the
        durability promise of section 4 of the spec no longer holds.
        """
        if not message:
            if self.banner.get_revealed():
                self.banner.set_revealed(False)
            return
        if self.banner.get_title() != message or not self.banner.get_revealed():
            self.banner.set_title(message)
            self.banner.set_revealed(True)

    # -- IPC -----------------------------------------------------------------

    def handle_ipc(self, request: dict[str, Any], payload: bytes | None) -> dict[str, Any] | None:
        """Serve one IPC request (ARCHITECTURE.md section 8).

        Runs on the GTK main thread.  An unknown command raises, which the
        server turns into ``{"ok": false, "error": ...}``.
        """
        cmd = request.get("cmd")
        token = request.get("activation_token") or None
        if cmd == "ping":
            return {"version": __version__}
        if cmd == "toggle":
            return {"visible": self.toggle_window(token)}
        if cmd == "show":
            self.show_window(token)
            return {"visible": True}
        if cmd == "hide":
            self.hide_window()
            return {"visible": False}
        if cmd == "insert":
            return self._ipc_insert(request, payload)
        if cmd == "attach":
            return self._ipc_attach(request, payload)
        if cmd == "attach_path":
            return self._ipc_attach_path(request)
        if cmd == "screenshot":
            self.capture.screenshot()
            return {"started": True}
        if cmd == "paste_clipboard":
            self.capture.paste_clipboard_after_show(token)
            return {"visible": True}
        if cmd == "status":
            return {
                "visible": self.is_visible(),
                "chars": len(self.store.text),
                "events": self.store.history.count,
                "version": __version__,
            }
        raise ValueError(f"unknown command {cmd!r}")

    def _ipc_insert(self, request: dict[str, Any], payload: bytes | None) -> dict[str, Any]:
        # A payload wins over the header field: large texts travel as bytes.
        if payload is not None:
            text: object = payload.decode("utf-8", "replace")
        else:
            text = request.get("text")
        if not isinstance(text, str) or not text:
            raise ValueError("insert needs 'text' or a payload")
        where = request.get("where", "cursor")
        if where == "end":
            self.editor.insert_at_end(text)
        elif where == "cursor":
            self.editor.insert_at_cursor(text)
        else:
            raise ValueError(f"unknown insert target {where!r}")
        return {"chars": len(text)}

    def _ipc_attach(self, request: dict[str, Any], payload: bytes | None) -> dict[str, Any]:
        if payload is None:
            raise ValueError("attach needs a payload")
        kind = request.get("kind", "text")
        mime = request.get("mime")
        filename = request.get("filename")
        if kind == "text":
            attachment = self.attachments.create_text(
                payload, mime=mime or "text/plain", filename=filename
            )
        elif kind == "image":
            attachment = self.attachments.create_image(
                payload, mime=mime or "image/png", filename=filename
            )
        elif kind == "file":
            attachment = self.attachments.create_file(
                payload, mime=mime or "application/octet-stream", filename=filename
            )
        else:
            raise ValueError(f"unknown attachment kind {kind!r}")
        self.capture.insert_token(attachment)
        return {"token": attachment.token, "object_id": attachment.object_id}

    def _ipc_attach_path(self, request: dict[str, Any]) -> dict[str, Any]:
        raw = request.get("path")
        if not isinstance(raw, str) or not raw:
            raise ValueError("attach_path needs 'path'")
        path = Path(raw).expanduser()
        if not path.is_file():
            raise ValueError(f"{path} is not a file")
        attachment = self.attachments.create_from_path(path)
        self.capture.insert_token(attachment)
        return {"token": attachment.token, "object_id": attachment.object_id}

    # -- teardown ------------------------------------------------------------

    def shutdown(self) -> None:
        """Stop the window's timers (called before the store is closed)."""
        for timer in (self._timeline_timer, self._timeline_debounce):
            if timer:
                GLib.source_remove(timer)
        self._timeline_timer = 0
        self._timeline_debounce = 0
        self.editor.shutdown()
