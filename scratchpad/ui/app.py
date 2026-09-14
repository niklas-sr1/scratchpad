"""The application object: process lifetime, single instance, IPC and timers.

Start-up order matters and is deliberate:

1. load the configuration,
2. open the store, which takes the data directory's ``flock``.  If another
   instance holds it, this process is not the scratchpad -- it asks the running
   one to show itself and exits successfully,
3. only then create the GTK application, the attachment store and the IPC
   server.

The process outlives its window.  Closing the window hides it (spec section 3:
the scratchpad behaves like a drop-down terminal), and only the explicit *Quit*
command, SIGINT or SIGTERM end the process.  Every one of those paths goes
through :meth:`ScratchpadApplication.do_shutdown`, which closes the store (which
writes SESSION_STOP, a checkpoint and ``current.txt``), the attachment database
and the socket.

Timers, per the store's own contract: ``tick()`` every 250 ms for fsync
batching and deferred checkpoints, ``heartbeat()`` every
``config.heartbeat_seconds`` so a crashed session's end time is known within
that bound.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
from collections.abc import MutableMapping

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, GLib  # noqa: E402

from scratchpad import __version__, ipc, paths  # noqa: E402
from scratchpad.attachments import AttachmentStore  # noqa: E402
from scratchpad.config import Config  # noqa: E402
from scratchpad.core.store import ScratchpadStore, StoreLockedError  # noqa: E402
from scratchpad.tokens import TokenCodec, load_or_create_secret  # noqa: E402
from scratchpad.ui.window import ScratchpadWindow  # noqa: E402

__all__ = [
    "ScratchpadApplication",
    "main",
    "APPLICATION_ID",
    "ACTIVATION_TOKEN_ENV",
    "pop_activation_token",
]

log = logging.getLogger(__name__)

APPLICATION_ID = "dev.scratchpad.Scratchpad"

#: Period of the store's periodic work, in milliseconds (see ``store.tick``).
TICK_INTERVAL_MS = 250

#: How the CLI hands this process the activation token of the invocation that
#: started it.  ``XDG_ACTIVATION_TOKEN`` itself must not be inherited by a long
#: lived process (it is single use and goes stale), and the autostarting
#: ``toggle`` does not resend its request once the GUI is up, so the token of
#: that very first global-shortcut press would otherwise be lost -- and a
#: ``present()`` without one only makes the window demand attention on Wayland.
ACTIVATION_TOKEN_ENV = "SCRATCHPAD_ACTIVATION_TOKEN"


def pop_activation_token(environ: MutableMapping[str, str]) -> str | None:
    """Take :data:`ACTIVATION_TOKEN_ENV` out of ``environ``.

    The token is single use, so it is removed whatever its value was; an empty
    one is reported as ``None``.
    """
    token = environ.pop(ACTIVATION_TOKEN_ENV, None)
    return token or None


class ScratchpadApplication(Adw.Application):
    """One running scratchpad: the window, the IPC server and the timers."""

    def __init__(self, *, store: ScratchpadStore, config: Config, data_dir) -> None:
        super().__init__(
            application_id=APPLICATION_ID,
            flags=Gio.ApplicationFlags.NON_UNIQUE,
        )
        self.store = store
        self.config = config
        self.data_dir = data_dir
        self.codec: TokenCodec | None = None
        self.attachments: AttachmentStore | None = None
        self.server: ipc.IpcServer | None = None
        self.window: ScratchpadWindow | None = None
        self._sources: list[int] = []
        self._signal_sources: dict[int, int] = {}
        self._shut_down = False
        self._tick_error: str | None = None
        self._storage_warning: str | None = None

    # -- lifecycle -----------------------------------------------------------

    def do_startup(self) -> None:
        """Create the non-GTK resources and start serving IPC."""
        Adw.Application.do_startup(self)
        # The window is hidden, not destroyed, so GTK must not quit for us.
        self.hold()

        secret = load_or_create_secret(paths.secret_key(self.data_dir))
        self.codec = TokenCodec(secret)
        self.attachments = AttachmentStore(self.data_dir, self.codec)

        self._start_server()
        self._start_timers()
        self._install_signal_handlers()

    def do_activate(self) -> None:
        """Show the window, creating it on the first activation.

        A cold start carries the activation token of the invocation that spawned
        us in :data:`ACTIVATION_TOKEN_ENV`; it is consumed here so that the very
        first presentation is focused rather than merely urgent.
        """
        if self.window is None:
            self.window = ScratchpadWindow(
                self,
                store=self.store,
                config=self.config,
                attachments=self.attachments,
                codec=self.codec,
            )
        self.window.show_window(pop_activation_token(os.environ))

    def do_shutdown(self) -> None:
        """Release everything exactly once, in the reverse order of startup."""
        if not self._shut_down:
            self._shut_down = True
            for source in self._sources:
                GLib.source_remove(source)
            self._sources.clear()
            if self.window is not None:
                try:
                    self.window.shutdown()
                except Exception:  # noqa: BLE001
                    log.exception("window shutdown failed")
            if self.server is not None:
                self.server.close()
                self.server = None
            try:
                self.store.close()
            except Exception:  # noqa: BLE001 - never lose the rest of the teardown
                log.exception("closing the store failed")
            if self.attachments is not None:
                try:
                    self.attachments.close()
                except Exception:  # noqa: BLE001
                    log.exception("closing the attachment store failed")
                self.attachments = None
        Adw.Application.do_shutdown(self)

    # -- wiring --------------------------------------------------------------

    def _start_server(self) -> None:
        socket_path = ipc.runtime_socket_path()
        try:
            self.server = ipc.IpcServer(socket_path, self._handle_ipc)
        except ipc.IpcError as exc:
            log.error("IPC is unavailable (%s); the CLI cannot reach this instance", exc)
            self.server = None
            return
        self._sources.append(
            GLib.io_add_watch(
                self.server.fileno(), GLib.IO_IN, lambda *_a: self.server.handle_ready()
            )
        )
        log.info("listening on %s", socket_path)

    def _start_timers(self) -> None:
        self._sources.append(GLib.timeout_add(TICK_INTERVAL_MS, self._on_tick))
        seconds = max(1, int(self.config.heartbeat_seconds))
        self._sources.append(GLib.timeout_add_seconds(seconds, self._on_heartbeat))

    def _on_tick(self) -> bool:
        try:
            self.store.tick()
        except Exception as exc:  # noqa: BLE001 - a failed flush must not stop the timer
            log.exception("store tick failed")
            self._tick_error = str(exc)
        else:
            self._tick_error = None
        try:
            self._poll_storage_error()
        except Exception:  # noqa: BLE001 - losing this source stops all periodic work
            log.exception("polling the storage error state failed")
        return GLib.SOURCE_CONTINUE

    def _on_heartbeat(self) -> bool:
        try:
            self.store.heartbeat()
        except Exception as exc:  # noqa: BLE001
            log.exception("heartbeat failed")
            self._tick_error = str(exc)
        return GLib.SOURCE_CONTINUE

    def _poll_storage_error(self) -> None:
        """Surface ``store.last_io_error`` (if the store has one) as a banner.

        A failed fsync means the durability promise no longer holds, so it must
        be visible rather than only logged.
        """
        if self.window is None:
            return
        message = _format_io_error(getattr(self.store, "last_io_error", None))
        if message is None and self._tick_error:
            message = f"Storage error: {self._tick_error}"
        if message == self._storage_warning:
            return
        self._storage_warning = message
        self.window.show_storage_warning(message)

    def _install_signal_handlers(self) -> None:
        for number in (signal.SIGINT, signal.SIGTERM):
            source = GLib.unix_signal_add(
                GLib.PRIORITY_DEFAULT, number, self._on_signal, number
            )
            self._signal_sources[number] = source
            self._sources.append(source)

    def _on_signal(self, number: int) -> bool:
        log.info("received %s; shutting down", signal.Signals(number).name)
        # The source removes itself by returning SOURCE_REMOVE, so forget its id
        # before do_shutdown tries to remove it a second time.
        source = self._signal_sources.pop(number, None)
        if source is not None and source in self._sources:
            self._sources.remove(source)
        self.quit()
        return GLib.SOURCE_REMOVE

    # -- IPC -----------------------------------------------------------------

    def _handle_ipc(self, request: dict, payload: bytes | None) -> dict | None:
        """Dispatch an IPC request on the GTK main thread."""
        if self.window is None:
            self.activate()
        if self.window is None:  # pragma: no cover - activate always builds one
            raise RuntimeError("the window is not available")
        return self.window.handle_ipc(request, payload)


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #

def _format_io_error(error: object) -> str | None:
    """Render ``store.last_io_error`` as a one line warning, whatever shape it has.

    The store may expose it as ``None``, a string, an exception, an object with
    a ``message`` attribute or a ``(timestamp, message)`` tuple; all of those
    turn into the same banner text here.
    """
    if error is None:
        return None
    message: object = error
    operation = ""
    if isinstance(error, tuple) and error:
        message = error[-1]
    elif hasattr(error, "message"):
        message = getattr(error, "message")
        operation = str(getattr(error, "operation", "") or "")
    text = str(message).strip()
    if not text:
        return None
    where = f" during {operation}" if operation else ""
    return f"Storage error{where}: {text}. Edits may not be durable; check the disk."


def _configure_logging() -> None:
    level = logging.DEBUG if os.environ.get("SCRATCHPAD_DEBUG") else logging.INFO
    logging.basicConfig(
        level=level, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )


def _show_running_instance() -> bool:
    """Ask an already running instance to show itself.  True when it answered."""
    request: dict[str, object] = {"cmd": "show"}
    activation = os.environ.get("XDG_ACTIVATION_TOKEN")
    if activation:
        request["activation_token"] = activation
    try:
        response = ipc.IpcClient().request(request)
    except ipc.IpcError as exc:
        log.debug("no running instance answered: %s", exc)
        return False
    return bool(response.get("ok"))


def main() -> int:
    """Run the GUI.  Returns the process exit status."""
    _configure_logging()
    GLib.set_prgname(APPLICATION_ID)
    GLib.set_application_name("Scratchpad")

    config = Config.load()
    data_dir = paths.data_dir()
    try:
        store = ScratchpadStore.open(data_dir, config, app_version=__version__)
    except StoreLockedError:
        if _show_running_instance():
            return 0
        print(
            f"scratchpad: {data_dir} is locked by another instance that does not "
            "answer on the IPC socket; not starting a second one.",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:  # noqa: BLE001 - report instead of a traceback
        print(f"scratchpad: cannot open {data_dir}: {exc}", file=sys.stderr)
        return 1

    app = ScratchpadApplication(store=store, config=config, data_dir=data_dir)
    try:
        return app.run([sys.argv[0] if sys.argv else "scratchpad"])
    finally:
        if not store.closed:  # pragma: no cover - do_shutdown normally did it
            store.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
