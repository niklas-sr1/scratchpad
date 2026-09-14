"""Capture flows: clipboard, attachments and screenshots.

Three explicit verbs, never one clever heuristic (spec sections 13, 29, 30):

``paste_normal``          text goes inline; an image-only clipboard becomes an
                          image attachment (there is no plain text to insert);
                          a text paste above the configured thresholds asks
                          "Insert inline" / "Store as attachment" first.
``paste_as_attachment``   always stores, never asks.
``screenshot``            xdg-desktop-portal, ``cosmic-screenshot`` as fallback.

Ctrl+V does not reach GTK's own paste: the editor stops the ``paste-clipboard``
signal and calls :meth:`CaptureController.paste_normal` instead, so the
guardrail cannot be bypassed by habit.

Every stored attachment ends with its token inserted at the cursor by
``ScratchpadEditor.insert_token``, which pads the token with a space wherever a
neighbouring alphanumeric character would otherwise swallow it into a longer run
(spec section 20).
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Callable
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")

from gi.repository import Gdk, Gio, GLib, GObject, Gtk  # noqa: E402

from scratchpad.ui import dialogs  # noqa: E402

__all__ = ["CaptureController", "PORTAL_BUS_NAME", "request_object_path"]

log = logging.getLogger(__name__)

PORTAL_BUS_NAME = "org.freedesktop.portal.Desktop"
PORTAL_OBJECT_PATH = "/org/freedesktop/portal/desktop"
PORTAL_SCREENSHOT_IFACE = "org.freedesktop.portal.Screenshot"
PORTAL_REQUEST_IFACE = "org.freedesktop.portal.Request"

#: Fallback capture tool on this desktop.
COSMIC_SCREENSHOT = "cosmic-screenshot"


def request_object_path(unique_name: str, handle_token: str) -> str:
    """The object path the portal will use for a request.

    ``:1.234`` plus token ``scratchpad_17`` becomes
    ``/org/freedesktop/portal/desktop/request/1_234/scratchpad_17``.  Computing
    it lets us subscribe to the response *before* the call is made, which closes
    the race where the portal answers faster than we can subscribe.
    """
    sender = unique_name.removeprefix(":").replace(".", "_")
    return f"{PORTAL_OBJECT_PATH}/request/{sender}/{handle_token}"


class CaptureController:
    """Clipboard and screenshot flows for one window."""

    def __init__(self, window, editor, attachments, config) -> None:
        self.window = window
        self.editor = editor
        self.attachments = attachments
        self.config = config
        self._bus: Gio.DBusConnection | None = None

    # -- helpers -------------------------------------------------------------

    @property
    def clipboard(self) -> Gdk.Clipboard:
        """The display clipboard of this window."""
        return self.window.get_clipboard()

    def _toast(self, message: str) -> None:
        toast = getattr(self.window, "toast", None)
        if callable(toast):
            toast(message)
        else:  # pragma: no cover - the window always has one
            log.info("%s", message)

    def _error(self, heading: str, body: str) -> None:
        dialogs.error(self.window, heading, body)

    def insert_token(self, attachment) -> str:
        """Insert an attachment's token at the cursor and report it."""
        self.editor.insert_token(attachment.token)
        self.editor.invalidate_resolution_cache(attachment.token)
        return attachment.token

    # -- clipboard -----------------------------------------------------------

    def _formats(self) -> Gdk.ContentFormats:
        return self.clipboard.get_formats()

    def _has_text(self) -> bool:
        formats = self._formats()
        return bool(
            formats.contain_gtype(GObject.TYPE_STRING)
            or formats.contain_mime_type("text/plain;charset=utf-8")
            or formats.contain_mime_type("text/plain")
            or formats.contain_mime_type("UTF8_STRING")
        )

    def _has_image(self) -> bool:
        formats = self._formats()
        return bool(
            formats.contain_gtype(Gdk.Texture.__gtype__)
            or formats.contain_mime_type("image/png")
            or formats.contain_mime_type("image/jpeg")
        )

    def paste_normal(self) -> None:
        """Ctrl+V: inline for text, an image attachment for an image clipboard."""
        if self._has_text():
            self._read_text(self._handle_normal_text)
        elif self._has_image():
            self._read_texture(self._store_texture)
        else:
            self._toast("The clipboard holds nothing that can be pasted.")

    def paste_as_attachment(self) -> None:
        """Ctrl+Shift+V: always store, never ask."""
        if self._has_text():
            self._read_text(self._store_text)
        elif self._has_image():
            self._read_texture(self._store_texture)
        else:
            self._toast("The clipboard holds nothing that can be attached.")

    def attach_clipboard_image(self) -> None:
        """Store the clipboard image, whatever else the clipboard also offers."""
        if not self._has_image():
            self._toast("The clipboard does not hold an image.")
            return
        self._read_texture(self._store_texture)

    def paste_clipboard_after_show(self, activation_token: str | None = None) -> None:
        """The ``paste_clipboard`` IPC command: show the window, then paste."""
        self.window.show_window(activation_token)
        GLib.idle_add(lambda: (self.paste_normal(), False)[1])

    def _read_text(self, then: Callable[[str], None]) -> None:
        def done(clipboard: Gdk.Clipboard, result: Gio.AsyncResult) -> None:
            try:
                text = clipboard.read_text_finish(result)
            except GLib.Error as exc:
                self._error("Clipboard unavailable", f"Could not read text: {exc.message}")
                return
            if not text:
                self._toast("The clipboard is empty.")
                return
            then(text)

        self.clipboard.read_text_async(None, done)

    def _read_texture(self, then: Callable[[Gdk.Texture], None]) -> None:
        def done(clipboard: Gdk.Clipboard, result: Gio.AsyncResult) -> None:
            try:
                texture = clipboard.read_texture_finish(result)
            except GLib.Error as exc:
                self._error("Clipboard unavailable", f"Could not read the image: {exc.message}")
                return
            if texture is None:
                self._toast("The clipboard does not hold an image.")
                return
            then(texture)

        self.clipboard.read_texture_async(None, done)

    def _handle_normal_text(self, text: str) -> None:
        lines = text.count("\n") + 1
        chars = len(text)
        limit_lines = int(getattr(self.config, "large_paste_threshold_lines", 2000))
        limit_chars = int(getattr(self.config, "large_paste_threshold_chars", 200000))
        if lines > limit_lines or chars > limit_chars:
            dialogs.large_paste(
                self.window,
                lines=lines,
                chars=chars,
                on_inline=lambda: self.editor.insert_at_cursor(text),
                on_attachment=lambda: self._store_text(text),
            )
            return
        self.editor.insert_at_cursor(text)

    def _store_text(self, text: str) -> None:
        try:
            attachment = self.attachments.create_text(text.encode("utf-8"), mime="text/plain")
        except Exception as exc:  # noqa: BLE001
            log.exception("storing a text attachment failed")
            self._error("Could not store the attachment", str(exc))
            return
        self.insert_token(attachment)
        self._toast(f"Stored {attachment.lines or 0:,} lines as an attachment.")

    def _store_texture(self, texture: Gdk.Texture) -> None:
        try:
            data = texture.save_to_png_bytes().get_data() or b""
        except Exception as exc:  # noqa: BLE001
            log.exception("encoding the clipboard image failed")
            self._error("Could not store the image", str(exc))
            return
        self._store_image_bytes(data, filename=None)

    def _store_image_bytes(self, data: bytes, *, filename: str | None) -> None:
        try:
            attachment = self.attachments.create_image(
                data, mime="image/png", filename=filename
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("storing an image attachment failed")
            self._error("Could not store the image", str(exc))
            return
        self.insert_token(attachment)
        size = f"{attachment.width}x{attachment.height}" if attachment.width else "image"
        self._toast(f"Stored a {size} image as an attachment.")

    # -- files ---------------------------------------------------------------

    def attach_file(self) -> None:
        """Pick a file and store it as an attachment."""
        dialog = Gtk.FileDialog()
        dialog.set_title("Attach a file")

        def done(source: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
            try:
                gfile = source.open_finish(result)
            except GLib.Error as exc:
                if exc.code != Gtk.DialogError.DISMISSED:
                    self._error("Could not open the file", exc.message)
                return
            if gfile is None:
                return
            path = gfile.get_path()
            if path is None:
                self._error("Unsupported location", "Only local files can be attached.")
                return
            self.attach_path(Path(path))

        dialog.open(self.window, None, done)

    def attach_path(self, path: Path) -> object | None:
        """Store an existing file as an attachment and insert its token."""
        try:
            attachment = self.attachments.create_from_path(Path(path))
        except Exception as exc:  # noqa: BLE001
            log.exception("attaching %s failed", path)
            self._error("Could not attach the file", str(exc))
            return None
        self.insert_token(attachment)
        self._toast(f"Attached {Path(path).name}.")
        return attachment

    # -- screenshots ---------------------------------------------------------

    def screenshot(self, *, then: Callable[[object], None] | None = None) -> None:
        """Capture a screenshot into an attachment and insert its token.

        Tries ``org.freedesktop.portal.Screenshot`` with ``interactive: true``
        (which is what lets the user pick a region), and falls back to the
        COSMIC command line tool when the portal is unavailable or fails.  The
        user's screenshot file is never deleted.
        """
        try:
            self._screenshot_portal(then)
        except GLib.Error as exc:
            log.warning("screenshot portal unavailable (%s); falling back", exc.message)
            self._screenshot_fallback(then)
        except Exception:  # noqa: BLE001
            log.exception("screenshot portal call failed; falling back")
            self._screenshot_fallback(then)

    def _connection(self) -> Gio.DBusConnection:
        if self._bus is None:
            self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        return self._bus

    def _screenshot_portal(self, then: Callable[[object], None] | None) -> None:
        bus = self._connection()
        unique = bus.get_unique_name()
        if not unique:
            raise RuntimeError("the session bus connection has no unique name")
        handle_token = f"scratchpad_{secrets.randbits(32):08x}"
        path = request_object_path(unique, handle_token)

        state: dict[str, object] = {"done": False}

        def on_response(
            _connection: Gio.DBusConnection, _sender: str, _path: str, _iface: str,
            _signal: str, parameters: GLib.Variant,
        ) -> None:
            if state["done"]:
                return
            state["done"] = True
            subscription = state.get("subscription")
            if isinstance(subscription, int):
                bus.signal_unsubscribe(subscription)
            code, results = parameters.unpack()
            if code != 0:
                log.info("screenshot cancelled (portal response %s)", code)
                return
            uri = results.get("uri") if isinstance(results, dict) else None
            if not uri:
                self._error("Screenshot failed", "The portal returned no image.")
                return
            self._store_screenshot_uri(str(uri), then)

        state["subscription"] = bus.signal_subscribe(
            PORTAL_BUS_NAME, PORTAL_REQUEST_IFACE, "Response", path, None,
            Gio.DBusSignalFlags.NONE, on_response,
        )

        options = {
            "handle_token": GLib.Variant("s", handle_token),
            "interactive": GLib.Variant("b", True),
            "modal": GLib.Variant("b", True),
        }

        def on_call_done(source: Gio.DBusConnection, result: Gio.AsyncResult) -> None:
            try:
                source.call_finish(result)
            except GLib.Error as exc:
                if state["done"]:
                    return
                state["done"] = True
                subscription = state.get("subscription")
                if isinstance(subscription, int):
                    bus.signal_unsubscribe(subscription)
                log.warning("portal Screenshot call failed (%s); falling back", exc.message)
                self._screenshot_fallback(then)

        bus.call(
            PORTAL_BUS_NAME, PORTAL_OBJECT_PATH, PORTAL_SCREENSHOT_IFACE, "Screenshot",
            GLib.Variant("(sa{sv})", ("", options)), GLib.VariantType("(o)"),
            Gio.DBusCallFlags.NONE, 60_000, None, on_call_done,
        )

    def _store_screenshot_uri(self, uri: str, then: Callable[[object], None] | None) -> None:
        try:
            gfile = Gio.File.new_for_uri(uri)
            ok, data, _etag = gfile.load_contents(None)
            if not ok:
                raise RuntimeError(f"could not read {uri}")
        except Exception as exc:  # noqa: BLE001
            log.exception("reading the screenshot at %s failed", uri)
            self._error("Screenshot failed", str(exc))
            return
        name = Path(Gio.File.new_for_uri(uri).get_parse_name()).name or "screenshot.png"
        try:
            attachment = self.attachments.create_image(
                bytes(data), mime="image/png", filename=name
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("storing the screenshot failed")
            self._error("Screenshot failed", str(exc))
            return
        self.window.show_window(None)
        self.insert_token(attachment)
        self._toast("Screenshot attached.")
        if then is not None:
            then(attachment)

    def _screenshot_fallback(self, then: Callable[[object], None] | None) -> None:
        try:
            process = Gio.Subprocess.new(
                [COSMIC_SCREENSHOT, "--interactive=true", "--notify=false"],
                Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE,
            )
        except GLib.Error as exc:
            self._error(
                "Screenshot failed",
                "Neither the desktop portal nor "
                f"{COSMIC_SCREENSHOT} could be used: {exc.message}",
            )
            return

        def done(source: Gio.Subprocess, result: Gio.AsyncResult) -> None:
            try:
                ok, stdout, stderr = source.communicate_utf8_finish(result)
            except GLib.Error as exc:
                self._error("Screenshot failed", exc.message)
                return
            if not ok or source.get_exit_status() != 0:
                detail = (stderr or "").strip() or "the capture was cancelled"
                log.info("cosmic-screenshot: %s", detail)
                return
            path = (stdout or "").strip().splitlines()
            candidate = path[-1].strip() if path else ""
            if not candidate:
                self._toast("The screenshot tool did not report a file.")
                return
            if candidate.startswith("file://"):
                self._store_screenshot_uri(candidate, then)
            else:
                self._store_screenshot_uri(Gio.File.new_for_path(candidate).get_uri(), then)

        process.communicate_utf8_async(None, None, done)
