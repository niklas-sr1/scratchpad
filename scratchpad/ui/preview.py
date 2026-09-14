"""The attachment preview pane.

Two `PreviewPane` instances live in the main window's sidebar ("Cursor" and
"Hover", see `Design Specification.md` section 22).  A pane always shows its
title; its body is a `Gtk.Stack` that switches between five pages:

    blank    nothing (no token, or a token that does not authenticate)
    image    a `Gtk.Picture` plus "PNG 1280x720, created 2026-09-14 18:07"
    text     a `LineNumberedTextView` with search and first/last navigation
    file     metadata plus an "Open externally" button
    missing  the reference authenticates but the blob or metadata is gone

The pane only ever *reads* attachments; it never mutates the document or the
history.  Blobs are loaded lazily and the last shown token is cached, so
re-showing the same resolution (which happens constantly while the pointer
moves over the same token) does not re-read the blob.

This module deliberately duck-types `Resolution`, `Attachment` and
`AttachmentStore` (owned by `scratchpad.attachments`): only the attribute names
from the architecture contract are used, so the widget can be tested with
stand-in objects.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Pango", "1.0")

from gi.repository import Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from scratchpad.ui.textview_extras import LineNumberedTextView  # noqa: E402

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from scratchpad.attachments import Attachment, AttachmentStore, Resolution

__all__ = ["PreviewPane", "MISSING_MESSAGE", "MAX_PREVIEW_BYTES"]

log = logging.getLogger(__name__)

#: Shown verbatim for a MISSING resolution (spec section 16, state 3).
MISSING_MESSAGE = (
    "Valid local attachment reference, but the backing object is unavailable or corrupt."
)

#: Text attachments larger than this are truncated for display only.
MAX_PREVIEW_BYTES = 20 * 1024 * 1024

PAGE_BLANK = "blank"
PAGE_IMAGE = "image"
PAGE_TEXT = "text"
PAGE_FILE = "file"
PAGE_MISSING = "missing"


def _state_name(state: Any) -> str:
    """Name of a `ResolutionState` value, tolerating stand-ins."""
    return str(getattr(state, "name", state)).upper()


def _kind_name(kind: Any) -> str:
    """Name of an `AttachmentKind` value ("text", "image" or "file")."""
    value = getattr(kind, "value", None)
    if isinstance(value, str):
        return value.lower()
    return str(kind).lower()


def _format_wall_ns(wall_ns: int | None) -> str:
    """Format a wall-clock nanosecond timestamp as local "YYYY-MM-DD HH:MM"."""
    if not wall_ns:
        return "unknown"
    try:
        return datetime.fromtimestamp(wall_ns / 1_000_000_000).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return "unknown"


def _human_size(size: int | None) -> str:
    """Human readable byte count, e.g. "1.4 MB"."""
    if size is None:
        return "unknown size"
    value = float(size)
    if value < 1024:
        return f"{int(value)} B"
    for unit in ("KB", "MB", "GB", "TB"):
        value /= 1024.0
        if value < 1024.0:
            return f"{value:.1f} {unit}"
    return f"{value:.1f} PB"


def _mime_subtype(mime: str | None) -> str:
    """Upper-cased MIME subtype, e.g. "image/png" -> "PNG"."""
    if not mime:
        return "?"
    subtype = mime.split("/")[-1].strip()
    return subtype.upper() or "?"


class PreviewPane(Gtk.Box):
    """A titled, always-present preview area for one attachment token.

    Public API (consumed by `scratchpad.ui.window`):
        PreviewPane(title)
        show_resolution(res, attachments)   the only call the window needs
        clear()                             force the blank page
        set_title(title) / get_title()
        current_page_name() -> str          "blank"/"image"/"text"/"file"/"missing"
        current_token() -> str | None
        stack                               the `Gtk.Stack` (for tests)
        text_view                           the `LineNumberedTextView` of the text page
        picture                             the `Gtk.Picture` of the image page
    """

    def __init__(self, title: str) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add_css_class("preview-pane")
        self.set_vexpand(True)

        self._title = title
        self._cache_key: tuple[str, str] | None = None

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        header.set_margin_start(8)
        header.set_margin_end(8)
        header.set_margin_top(6)
        header.set_margin_bottom(4)
        self._title_label = Gtk.Label(label=title, xalign=0.0)
        self._title_label.add_css_class("heading")
        self._title_label.set_hexpand(True)
        self._token_label = Gtk.Label(label="", xalign=1.0)
        self._token_label.add_css_class("dim-label")
        self._token_label.add_css_class("monospace")
        self._token_label.set_ellipsize(Pango.EllipsizeMode.END)
        header.append(self._title_label)
        header.append(self._token_label)
        self.append(header)

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.NONE)
        # Request only the visible page's size: a blank pane must stay small.
        self.stack.set_hhomogeneous(False)
        self.stack.set_vhomogeneous(False)
        self.stack.set_vexpand(True)
        self.stack.set_hexpand(True)
        self.append(self.stack)

        self.stack.add_named(self._build_blank_page(), PAGE_BLANK)
        self.stack.add_named(self._build_image_page(), PAGE_IMAGE)
        self.stack.add_named(self._build_text_page(), PAGE_TEXT)
        self.stack.add_named(self._build_file_page(), PAGE_FILE)
        self.stack.add_named(self._build_missing_page(), PAGE_MISSING)
        self.stack.set_visible_child_name(PAGE_BLANK)

    # ------------------------------------------------------------- page setup

    def _build_blank_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        page.set_vexpand(True)
        return page

    def _build_image_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        page.set_margin_start(8)
        page.set_margin_end(8)
        page.set_margin_bottom(6)
        self.picture = Gtk.Picture()
        self.picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        self.picture.set_can_shrink(True)
        self.picture.set_vexpand(True)
        self.picture.set_hexpand(True)
        self._image_meta = Gtk.Label(label="", xalign=0.0)
        self._image_meta.add_css_class("dim-label")
        self._image_meta.add_css_class("caption")
        self._image_meta.set_wrap(True)
        page.append(self.picture)
        page.append(self._image_meta)
        return page

    def _build_text_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        page.set_margin_start(8)
        page.set_margin_end(8)
        page.set_margin_bottom(6)

        self._text_meta = Gtk.Label(label="", xalign=0.0)
        self._text_meta.add_css_class("dim-label")
        self._text_meta.add_css_class("caption")
        self._text_meta.set_wrap(True)
        page.append(self._text_meta)

        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text("Search (Enter: next, Shift+Enter: previous)")
        self.search_entry.set_hexpand(True)
        if hasattr(self.search_entry, "set_search_delay"):
            self.search_entry.set_search_delay(30)  # ms; the default 150 feels sluggish
        self.search_entry.connect("activate", self._on_search_activate)
        self.search_entry.connect("search-changed", self._on_search_changed)
        key = Gtk.EventControllerKey()
        key.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        key.connect("key-pressed", self._on_search_key)
        self.search_entry.add_controller(key)
        toolbar.append(self.search_entry)

        self._first_button = Gtk.Button.new_from_icon_name("go-top-symbolic")
        self._first_button.set_tooltip_text("First line")
        self._first_button.add_css_class("flat")
        self._first_button.connect("clicked", lambda *_a: self.text_view.scroll_to_start())
        toolbar.append(self._first_button)

        self._last_button = Gtk.Button.new_from_icon_name("go-bottom-symbolic")
        self._last_button.set_tooltip_text("Last line")
        self._last_button.add_css_class("flat")
        self._last_button.connect("clicked", lambda *_a: self.text_view.scroll_to_end())
        toolbar.append(self._last_button)
        page.append(toolbar)

        self.text_view = LineNumberedTextView(editable=False)
        self.text_view.set_vexpand(True)
        frame = Gtk.Frame()
        frame.set_child(self.text_view)
        frame.set_vexpand(True)
        frame.set_size_request(-1, 120)  # never squeeze a log down to two lines
        page.append(frame)
        return page

    def _build_file_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        page.set_margin_start(8)
        page.set_margin_end(8)
        page.set_margin_bottom(8)
        page.set_margin_top(4)

        self._file_name = Gtk.Label(label="", xalign=0.0)
        self._file_name.add_css_class("title-4")
        self._file_name.set_wrap(True)
        self._file_name.set_selectable(True)
        page.append(self._file_name)

        self._file_meta = Gtk.Label(label="", xalign=0.0)
        self._file_meta.add_css_class("dim-label")
        self._file_meta.set_wrap(True)
        self._file_meta.set_selectable(True)
        page.append(self._file_meta)

        self.open_button = Gtk.Button(label="Open externally")
        self.open_button.set_halign(Gtk.Align.START)
        self.open_button.connect("clicked", self._on_open_externally)
        page.append(self.open_button)

        self._file_error = Gtk.Label(label="", xalign=0.0)
        self._file_error.add_css_class("error")
        self._file_error.set_wrap(True)
        self._file_error.set_visible(False)
        page.append(self._file_error)
        return page

    def _build_missing_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        page.set_margin_start(8)
        page.set_margin_end(8)
        page.set_margin_top(8)
        page.set_margin_bottom(8)
        page.set_valign(Gtk.Align.START)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        icon = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
        icon.set_valign(Gtk.Align.START)
        icon.add_css_class("warning")
        row.append(icon)

        self._missing_label = Gtk.Label(label=MISSING_MESSAGE, xalign=0.0)
        self._missing_label.set_wrap(True)
        self._missing_label.set_hexpand(True)
        row.append(self._missing_label)
        page.append(row)

        self._missing_detail = Gtk.Label(label="", xalign=0.0)
        self._missing_detail.add_css_class("dim-label")
        self._missing_detail.add_css_class("caption")
        self._missing_detail.set_wrap(True)
        self._missing_detail.set_selectable(True)
        page.append(self._missing_detail)
        return page

    # ------------------------------------------------------------- public API

    def set_title(self, title: str) -> None:
        """Change the always-visible pane title."""
        self._title = title
        self._title_label.set_label(title)

    def get_title(self) -> str:
        """The pane title."""
        return self._title

    def current_page_name(self) -> str:
        """Name of the visible stack page ("blank", "image", ...)."""
        return self.stack.get_visible_child_name() or PAGE_BLANK

    def current_token(self) -> str | None:
        """Token currently previewed, or None when the pane is blank."""
        return self._cache_key[0] if self._cache_key else None

    def clear(self) -> None:
        """Show the blank body (the title stays visible)."""
        self._cache_key = None
        self._token_label.set_label("")
        self.picture.set_paintable(None)
        self.stack.set_visible_child_name(PAGE_BLANK)

    def show_resolution(
        self, res: "Resolution | None", attachments: "AttachmentStore | None"
    ) -> None:
        """Display `res`.

        None or an ORDINARY resolution blanks the body.  MISSING shows the
        warning page.  VALID dispatches on the attachment kind.  Showing the
        same token twice in a row is a no-op (the blob is not re-read).
        """
        if res is None:
            self.clear()
            return
        state = _state_name(getattr(res, "state", None))
        if state == "ORDINARY":
            self.clear()
            return

        token = str(getattr(res, "token", "") or "")
        key = (token, state)
        if key == self._cache_key and self.current_page_name() != PAGE_BLANK:
            return

        detail = str(getattr(res, "detail", "") or "")
        if state == "MISSING":
            self._cache_key = key
            self._show_missing(token, detail)
            return

        attachment = getattr(res, "attachment", None)
        if attachment is None or attachments is None:
            self._cache_key = key
            self._show_missing(token, detail or "No attachment metadata available.")
            return

        self._cache_key = key
        self._token_label.set_label(token)
        kind = _kind_name(getattr(attachment, "kind", ""))
        try:
            if kind == "image":
                self._show_image(attachment, attachments)
            elif kind == "text":
                self._show_text(attachment, attachments)
            else:
                self._show_file(attachment, attachments)
        except Exception as exc:  # blob unreadable / corrupt / unexpected metadata
            log.warning("preview failed for token %s: %s", token, exc)
            self._show_missing(token, f"{detail} {exc}".strip())

    # ------------------------------------------------------------ page fillers

    def _show_missing(self, token: str, detail: str) -> None:
        self._token_label.set_label(token)
        self._missing_label.set_label(MISSING_MESSAGE)
        self._missing_detail.set_label(detail)
        self._missing_detail.set_visible(bool(detail))
        self.picture.set_paintable(None)
        self.stack.set_visible_child_name(PAGE_MISSING)

    def _show_image(self, att: "Attachment", store: "AttachmentStore") -> None:
        data = store.read_blob(att)
        try:
            texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(data))
        except GLib.Error as exc:
            raise ValueError(f"image could not be decoded: {exc.message}") from exc
        self.picture.set_paintable(texture)

        width = getattr(att, "width", None) or texture.get_width()
        height = getattr(att, "height", None) or texture.get_height()
        created = _format_wall_ns(getattr(att, "created_wall_ns", None))
        self._image_meta.set_label(
            f"{_mime_subtype(getattr(att, 'mime', None))} {width}x{height}, created {created}"
        )
        self.stack.set_visible_child_name(PAGE_IMAGE)

    def _show_text(self, att: "Attachment", store: "AttachmentStore") -> None:
        data = store.read_blob(att)
        truncated = len(data) > MAX_PREVIEW_BYTES
        shown = data[:MAX_PREVIEW_BYTES] if truncated else data
        text = shown.decode("utf-8", errors="replace")
        if truncated:
            text += (
                f"\n\n[preview truncated at {_human_size(MAX_PREVIEW_BYTES)} "
                f"of {_human_size(len(data))}]\n"
            )
        self.text_view.set_text(text)

        lines = getattr(att, "lines", None)
        chars = getattr(att, "chars", None)
        if lines is None:
            lines = text.count("\n") + (1 if text else 0)
        if chars is None:
            chars = len(text)
        created = _format_wall_ns(getattr(att, "created_wall_ns", None))
        meta = f"{lines:,} lines, {chars:,} chars, created {created}"
        encoding = getattr(att, "encoding", None)
        if encoding:
            meta += f", {encoding}"
        if truncated:
            meta += " (preview truncated)"
        self._text_meta.set_label(meta)
        self.search_entry.set_text("")
        self.search_entry.remove_css_class("error")
        self.stack.set_visible_child_name(PAGE_TEXT)

    def _show_file(self, att: "Attachment", store: "AttachmentStore") -> None:
        filename = getattr(att, "filename", None) or "(unnamed file)"
        self._file_name.set_label(filename)
        mime = getattr(att, "mime", None) or "application/octet-stream"
        created = _format_wall_ns(getattr(att, "created_wall_ns", None))
        size = _human_size(getattr(att, "size", None))
        self._file_meta.set_label(f"{mime}\n{size}, created {created}")
        self._file_error.set_visible(False)
        self._file_error.set_label("")
        self._file_path = None
        try:
            self._file_path = store.blob_path(att)
        except Exception as exc:  # blob gone: keep metadata, disable the button
            log.info("blob path unavailable: %s", exc)
        self.open_button.set_sensitive(self._file_path is not None)
        self.stack.set_visible_child_name(PAGE_FILE)

    # -------------------------------------------------------------- callbacks

    def _on_open_externally(self, _button: Gtk.Button) -> None:
        path = getattr(self, "_file_path", None)
        if path is None:
            return
        uri = Gio.File.new_for_path(str(path)).get_uri()
        try:
            Gio.AppInfo.launch_default_for_uri(uri, None)
        except GLib.Error as exc:
            self._file_error.set_label(f"Could not open externally: {exc.message}")
            self._file_error.set_visible(True)

    def _on_search_activate(self, _entry: Gtk.SearchEntry) -> None:
        self._run_search(forward=True)

    def _on_search_changed(self, _entry: Gtk.SearchEntry) -> None:
        needle = self.search_entry.get_text()
        if not needle:
            self.search_entry.remove_css_class("error")
            return
        # Restart from the beginning of the current match so that typing
        # extends the match instead of skipping over occurrences.
        buf = self.text_view.buffer
        bounds = buf.get_selection_bounds()
        if bounds:
            buf.place_cursor(bounds[0])
        self._run_search(forward=True)

    def _on_search_key(
        self, _controller: Gtk.EventControllerKey, keyval: int, _keycode: int, state: Gdk.ModifierType
    ) -> bool:
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_ISO_Enter):
            if state & Gdk.ModifierType.SHIFT_MASK:
                self._run_search(forward=False)
                return True
        return False

    def _run_search(self, *, forward: bool) -> bool:
        needle = self.search_entry.get_text()
        found = self.text_view.search(needle, forward=forward) if needle else False
        if needle and not found:
            self.search_entry.add_css_class("error")
        else:
            self.search_entry.remove_css_class("error")
        return found
