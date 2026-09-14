"""Tests for the application shell (Agent D1): app, editor, commands, palette.

Three levels:

* a subprocess smoke test that really starts ``scratchpad gui``, drives it over
  IPC and checks the event log afterwards,
* in-process GTK tests of the editor's mutation interception against a real
  ``ScratchpadStore``,
* pure-function tests of the line editing helpers, which need no display.

Everything that needs GTK is skipped when no display is available; the pure
helper tests still run.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from scratchpad import __version__, ipc
from scratchpad.config import Config
from scratchpad.core.ops import OpKind
from scratchpad.core.store import ScratchpadStore
from scratchpad.ui.editor import (
    DEFAULT_INDENT_WIDTH,
    apply_edit,
    block_span,
    bullet_block_range,
    continuation_for_line,
    empty_bullet_body,
    indent_lines,
    move_lines,
    outdent_lines,
    pad_token,
)

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _display_available() -> bool:
    try:
        import gi

        gi.require_version("Gtk", "4.0")
        gi.require_version("Gdk", "4.0")
        from gi.repository import Gdk, Gtk
    except Exception:  # pragma: no cover - PyGObject missing
        return False
    return bool(Gtk.init_check() and Gdk.Display.get_default() is not None)


needs_display = pytest.mark.skipif(
    not _display_available(), reason="no display available for GTK tests"
)


def _gui_env(tmp_path: Path) -> tuple[dict[str, str], Path, Path]:
    """Environment that keeps a GUI process entirely inside ``tmp_path``."""
    data = tmp_path / "data"
    run = tmp_path / "run"
    data.mkdir(exist_ok=True)
    run.mkdir(exist_ok=True)
    env = dict(os.environ)
    env["SCRATCHPAD_DATA_DIR"] = str(data)
    env["XDG_RUNTIME_DIR"] = str(run)
    env["XDG_CONFIG_HOME"] = str(tmp_path / "config")
    env.pop("SCRATCHPAD_DEBUG", None)
    return env, data, run / "scratchpad" / "ipc.sock"


def _wait_for(predicate, timeout: float = 25.0, interval: float = 0.1) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


#: Deadlines for the subprocess smoke test.  They are generous on purpose: the
#: test must fail when the GUI is broken, not when the machine is busy running
#: the rest of the suite in parallel.  Nothing waits a fixed amount of time;
#: every wait polls a condition and gives up only at its deadline.
GUI_START_TIMEOUT = 30.0
GUI_REQUEST_TIMEOUT = 30.0
GUI_EXIT_TIMEOUT = 30.0


def _wait_for_ping(socket_path: Path, process: subprocess.Popen, timeout: float) -> None:
    """Block until the GUI answers ``ping``; fail with a diagnosis otherwise.

    Polls with a real request instead of watching for the socket file: the file
    exists a moment before the server accepts on it, and an unanswered socket is
    exactly the failure this test exists to catch.
    """
    deadline = time.monotonic() + timeout
    while True:
        if process.poll() is not None:
            pytest.fail(
                f"the GUI exited with status {process.returncode} during start-up\n"
                f"{_gui_output(process)}"
            )
        if ipc.is_server_alive(socket_path, timeout=2.0):
            return
        if time.monotonic() >= deadline:
            pytest.fail(
                f"the GUI did not answer a ping on {socket_path} within {timeout:g}s\n"
                f"{_gui_output(process)}"
            )
        time.sleep(0.05)


def _gui_output(process: subprocess.Popen) -> str:
    """Whatever the GUI printed so far (it logs to a file, never to a pipe)."""
    path = getattr(process, "_log_path", None)
    if path is None:  # pragma: no cover - only for a differently started process
        return ""
    try:
        return Path(path).read_text("utf-8", "replace")
    except OSError:  # pragma: no cover
        return ""


def _application(app_id: str):
    """A registered Adw.Application, so windows may be attached to it."""
    import gi

    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gio

    Adw.init()
    app = Adw.Application(application_id=app_id, flags=Gio.ApplicationFlags.NON_UNIQUE)
    app.register(None)   # emits ::startup, which windows require
    return app


def _accel(spec: str) -> list[str]:
    """Normalise an accelerator the way GTK stores it."""
    from gi.repository import Gtk

    ok, keyval, mods = Gtk.accelerator_parse(spec)
    assert ok, spec
    return [Gtk.accelerator_name(keyval, mods)]


# --------------------------------------------------------------------------- #
# (a) subprocess smoke test
# --------------------------------------------------------------------------- #

@needs_display
def test_gui_starts_serves_ipc_and_shuts_down_cleanly(tmp_path: Path) -> None:
    """Start the real GUI, drive it over IPC, SIGTERM it, inspect the log."""
    env, data_dir, socket_path = _gui_env(tmp_path)
    if len(os.fsencode(str(socket_path))) > 100:  # pragma: no cover - long tmpdir
        pytest.skip(f"socket path {socket_path} is too long for AF_UNIX")

    # The log goes to a file, not to a pipe: a pipe nobody drains fills up at
    # 64 KB and blocks the GUI, which would look exactly like a start-up hang.
    log_path = tmp_path / "gui.log"
    log_file = log_path.open("wb")
    process = subprocess.Popen(
        [sys.executable, "-m", "scratchpad", "gui"],
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    process._log_path = log_path  # type: ignore[attr-defined]
    log_file.close()
    try:
        _wait_for_ping(socket_path, process, GUI_START_TIMEOUT)

        client = ipc.IpcClient(socket_path)

        def request(payload_request, payload=None):
            return client.request(payload_request, payload, timeout=GUI_REQUEST_TIMEOUT)

        ping = request({"cmd": "ping"})
        assert ping["ok"] is True
        assert ping["version"] == __version__

        inserted = request({"cmd": "insert", "text": "hello"})
        assert inserted["ok"] is True

        attached = request(
            {"cmd": "attach", "kind": "text", "mime": "text/plain", "filename": "note.txt"},
            b"an attached payload\n",
        )
        assert attached["ok"] is True
        token = attached["token"]
        assert isinstance(token, str) and len(token) == 22
        assert token.isalnum() and token.isascii()
        assert isinstance(attached["object_id"], int)

        status = request({"cmd": "status"})
        assert status["ok"] is True
        assert status["visible"] is True
        assert status["chars"] >= len("hello") + 22
        assert status["events"] >= 3
        assert status["version"] == __version__

        process.send_signal(signal.SIGTERM)
        try:
            returncode = process.wait(timeout=GUI_EXIT_TIMEOUT)
        except subprocess.TimeoutExpired:
            pytest.fail(
                f"the GUI ignored SIGTERM for {GUI_EXIT_TIMEOUT:g}s\n{_gui_output(process)}"
            )
        assert returncode == 0, _gui_output(process)
    finally:
        if process.poll() is None:  # pragma: no cover - only on a failed assert
            process.kill()
            process.wait(timeout=10)

    # The data directory must now describe a complete, cleanly stopped session.
    store = ScratchpadStore.open(data_dir, Config(), app_version="test")
    try:
        assert "hello" in store.text
        assert token in store.text
        sessions = store.history.sessions()
        assert sessions, "no session was recorded"
        assert sessions[0].clean_stop is True
        assert sessions[0].last_seq > sessions[0].first_seq
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# (b) in-process editor interception
# --------------------------------------------------------------------------- #

@pytest.fixture
def gtk_store(tmp_path: Path):
    """A real store in a temp directory, closed after the test."""
    store = ScratchpadStore.open(tmp_path / "data", Config(), app_version="test")
    yield store
    if not store.closed:
        store.close()


def _logged_ops(store: ScratchpadStore) -> list[tuple[str, int, str]]:
    """Every document op in the log as ``(kind, pos, payload)``."""
    ops: list[tuple[str, int, str]] = []
    for event in store.history.events(0, store.history.count):
        if event.op is None:
            continue
        op = event.op
        payload = op.old_text if op.kind is OpKind.DELETE else op.text
        ops.append((op.kind.name, op.pos, payload))
    return ops


@needs_display
def test_editor_logs_every_buffer_mutation(gtk_store) -> None:
    """Typing, deleting and undoing all reach the store, in order."""
    from scratchpad.ui.editor import ScratchpadEditor

    editor = ScratchpadEditor(gtk_store, Config())
    buffer = editor.buffer

    for char in "hello":
        buffer.insert(buffer.get_end_iter(), char, -1)
    assert editor.get_text() == "hello"
    assert gtk_store.text == "hello"

    # insert in the middle
    buffer.insert(buffer.get_iter_at_offset(2), "XY", -1)
    assert gtk_store.text == "heXYllo"

    # delete a range
    buffer.delete(buffer.get_iter_at_offset(2), buffer.get_iter_at_offset(4))
    assert gtk_store.text == "hello"

    assert _logged_ops(gtk_store) == [
        ("INSERT", 0, "h"),
        ("INSERT", 1, "e"),
        ("INSERT", 2, "l"),
        ("INSERT", 3, "l"),
        ("INSERT", 4, "o"),
        ("INSERT", 2, "XY"),
        ("DELETE", 2, "XY"),
    ]

    # An undo is an ordinary mutation, not a rewind of history.
    buffer.undo()
    assert gtk_store.text == editor.get_text()
    assert _logged_ops(gtk_store)[-1] == ("INSERT", 2, "XY")
    assert gtk_store.text == "heXYllo"

    buffer.redo()
    assert gtk_store.text == editor.get_text() == "hello"
    assert _logged_ops(gtk_store)[-1] == ("DELETE", 2, "XY")


@needs_display
def test_editor_initial_load_and_replace_all(tmp_path: Path) -> None:
    """The initial load logs nothing; a restore logs exactly one REPLACE."""
    from scratchpad.ui.editor import ScratchpadEditor

    store = ScratchpadStore.open(tmp_path / "data", Config(), app_version="test")
    try:
        editor = ScratchpadEditor(store, Config())
        editor.insert_at_cursor("first text")
        store.close()

        reopened = ScratchpadStore.open(tmp_path / "data", Config(), app_version="test")
        try:
            before = len(_logged_ops(reopened))
            editor2 = ScratchpadEditor(reopened, Config())
            assert editor2.get_text() == "first text"
            assert len(_logged_ops(reopened)) == before, (
                "loading the document must not be logged again"
            )

            editor2.replace_all("a totally different document")
            assert reopened.text == "a totally different document"
            assert _logged_ops(reopened)[before:] == [
                ("REPLACE", 0, "a totally different document")
            ]

            # Clearing is a plain DELETE of everything (spec section 32).
            editor2.replace_all("")
            assert reopened.text == ""
            assert _logged_ops(reopened)[-1] == (
                "DELETE", 0, "a totally different document",
            )
        finally:
            if not reopened.closed:
                reopened.close()
    finally:
        if not store.closed:
            store.close()


@needs_display
def test_editor_commands_go_through_the_buffer(gtk_store) -> None:
    """Indent, move and bullet deletion are logged like typing."""
    from scratchpad.ui.editor import ScratchpadEditor

    editor = ScratchpadEditor(gtk_store, Config())
    editor.insert_at_cursor("- one\n- two\n")
    editor.select_range(0, 0)
    editor.indent()
    assert gtk_store.text == editor.get_text() == "  - one\n- two\n"

    editor.outdent()
    assert gtk_store.text == editor.get_text() == "- one\n- two\n"

    editor.select_range(0, 0)
    editor.move_line_down()
    assert editor.get_text() == "- two\n- one\n"
    assert gtk_store.text == editor.get_text()

    editor.insert_at_end("tail")
    assert editor.get_text() == "- two\n- one\ntail"
    assert gtk_store.text == editor.get_text()


@needs_display
def test_editor_key_bindings_do_what_the_contract_says(gtk_store) -> None:
    """Tab, Shift+Tab, Alt+Up/Down, Ctrl+Shift+K, Enter and Escape."""
    from gi.repository import Gdk

    from scratchpad.ui.editor import ScratchpadEditor

    editor = ScratchpadEditor(gtk_store, Config())
    none = Gdk.ModifierType(0)
    shift = Gdk.ModifierType.SHIFT_MASK
    ctrl_shift = Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK
    alt = Gdk.ModifierType.ALT_MASK

    def press(keyval, state=none) -> bool:
        return editor._on_key_pressed(None, keyval, 0, state)

    editor.insert_at_cursor("- first")
    assert press(Gdk.KEY_Return)
    assert editor.get_text() == "- first\n- "        # bullet continued

    assert press(Gdk.KEY_Return)
    assert editor.get_text() == "- first\n"          # empty bullet removed

    editor.insert_at_cursor("- second")
    assert press(Gdk.KEY_Tab)
    assert editor.get_text() == "- first\n  - second"
    assert press(Gdk.KEY_ISO_Left_Tab)
    assert editor.get_text() == "- first\n- second"

    assert press(Gdk.KEY_Up, alt)
    assert editor.get_text() == "- second\n- first"
    assert press(Gdk.KEY_Down, alt)
    assert editor.get_text() == "- first\n- second"

    assert press(Gdk.KEY_Return, shift)              # plain newline, no bullet
    assert editor.get_text() == "- first\n- second\n"

    assert press(Gdk.KEY_k, ctrl_shift)              # delete the current block
    assert editor.get_text() == "- first\n- second"

    hidden: list[bool] = []
    editor.on_escape = lambda: hidden.append(True)
    assert press(Gdk.KEY_Escape)
    assert hidden == [True]

    assert gtk_store.text == editor.get_text()


@needs_display
def test_editor_token_insertion_keeps_the_token_a_candidate(gtk_store) -> None:
    """A token inserted next to alphanumeric text is padded with a space."""
    from scratchpad.tokens import find_candidates
    from scratchpad.ui.editor import ScratchpadEditor

    editor = ScratchpadEditor(gtk_store, Config())
    token = "Ab3Fmx7QK9v2Rt8Nc4WpLd"
    editor.insert_at_cursor("log")          # alphanumeric: needs a separator
    editor.insert_token(token)
    text = editor.get_text()
    assert text == "log " + token
    assert [candidate for _s, _e, candidate in find_candidates(text)] == [token]
    assert gtk_store.text == text


@needs_display
def test_window_builds_and_serves_ipc(tmp_path: Path) -> None:
    """The window wires up, and its IPC handler answers every command."""
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw

    from scratchpad import paths
    from scratchpad.attachments import AttachmentStore
    from scratchpad.tokens import TokenCodec, load_or_create_secret
    from scratchpad.ui.window import MENU_STRUCTURE, ScratchpadWindow, format_history_header

    Adw.init()
    data_dir = tmp_path / "data"
    store = ScratchpadStore.open(data_dir, Config(), app_version="test")
    codec = TokenCodec(load_or_create_secret(paths.secret_key(data_dir)))
    attachments = AttachmentStore(data_dir, codec)
    app = _application("dev.scratchpad.Test")
    try:
        window = ScratchpadWindow(
            app, store=store, config=Config(), attachments=attachments, codec=codec
        )

        assert window.handle_ipc({"cmd": "ping"}, None)["version"] == __version__
        window.handle_ipc({"cmd": "insert", "text": "hello"}, None)
        assert store.text == "hello"
        window.handle_ipc({"cmd": "insert", "where": "end"}, b"appended")
        assert store.text == "hello\nappended"

        result = window.handle_ipc(
            {"cmd": "attach", "kind": "text", "filename": "x.txt"}, b"payload"
        )
        assert len(result["token"]) == 22
        assert result["token"] in store.text

        status = window.handle_ipc({"cmd": "status"}, None)
        assert status["chars"] == len(store.text)
        assert status["events"] == store.history.count

        with pytest.raises(ValueError):
            window.handle_ipc({"cmd": "nope"}, None)

        # every command named in the menu exists in the registry
        for _title, ids in MENU_STRUCTURE:
            for command_id in ids:
                if command_id is not None:
                    assert window.registry.get(command_id) is not None, command_id

        # the palette can find commands by fuzzy query
        from scratchpad.ui.palette import filter_commands

        matches = filter_commands(window.registry.all(), "redact")
        assert matches and matches[0].id == "redact-selection"

        assert format_history_header(None, -1) == "No historical state selected"
        assert format_history_header(1_757_000_000_000_000_000, 42).endswith("(event 42)")

        window.shutdown()
    finally:
        attachments.close()
        if not store.closed:
            store.close()


def _pump(timeout: float = 1.0) -> None:
    """Run pending main loop work (idle callbacks) without blocking."""
    from gi.repository import GLib

    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not context.iteration(False):
            break


@needs_display
def test_editor_survives_a_failing_store_and_catches_up(gtk_store, monkeypatch) -> None:
    """A rejected op keeps the typed text and resynchronises the store."""
    from scratchpad.ui.editor import ScratchpadEditor

    editor = ScratchpadEditor(gtk_store, Config())
    errors: list[str] = []
    editor.on_error = errors.append

    real_apply = gtk_store.apply
    calls = {"n": 0}

    def flaky(op):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("No space left on device")
        return real_apply(op)

    monkeypatch.setattr(gtk_store, "apply", flaky)
    editor.insert_at_cursor("typed while the disk was full")

    assert editor.get_text() == "typed while the disk was full", "the text must survive"
    assert errors and "No space left on device" in errors[0]

    _pump()
    assert gtk_store.text == editor.get_text(), "the store must catch up"


def test_storage_error_banner_text() -> None:
    """``store.last_io_error`` becomes one readable warning line."""
    from scratchpad.ui.app import _format_io_error

    class Failure:
        message = "fsync: Input/output error"
        operation = "tick"

    assert _format_io_error(None) is None
    text = _format_io_error(Failure())
    assert text is not None
    assert "fsync: Input/output error" in text
    assert "during tick" in text
    assert _format_io_error((123, "disk full")).startswith("Storage error: disk full")


@needs_display
def test_cursor_and_hover_panes_follow_the_token(tmp_path: Path) -> None:
    """Acceptance criteria 14 and 15: the panes update, the document does not."""
    from scratchpad import paths
    from scratchpad.attachments import AttachmentStore
    from scratchpad.tokens import TokenCodec, load_or_create_secret
    from scratchpad.ui.window import ScratchpadWindow

    data_dir = tmp_path / "data"
    store = ScratchpadStore.open(data_dir, Config(), app_version="test")
    codec = TokenCodec(load_or_create_secret(paths.secret_key(data_dir)))
    attachments = AttachmentStore(data_dir, codec)
    app = _application("dev.scratchpad.TestPanes")
    try:
        window = ScratchpadWindow(
            app, store=store, config=Config(), attachments=attachments, codec=codec
        )
        attachment = attachments.create_text(b"payload\n", mime="text/plain")
        window.editor.insert_at_cursor(f"see {attachment.token} for details")
        before = window.editor.get_text()

        index = before.index(attachment.token)
        window.editor.select_range(index + 3, index + 3)
        assert window.cursor_pane.current_token() == attachment.token

        window.editor.select_range(0, 0)
        assert window.cursor_pane.current_token() is None
        assert window.cursor_pane.current_page_name() == "blank"

        # hover resolves the same way, through the debounced path
        window.editor._hover_offset = index + 5
        window.editor._hover_tick()
        assert window.hover_pane.current_token() == attachment.token

        window.editor._hover_offset = 0
        window.editor._hover_tick()
        assert window.hover_pane.current_token() is None

        assert window.editor.get_text() == before, "previewing must not edit"
        window.shutdown()
    finally:
        attachments.close()
        if not store.closed:
            store.close()


@needs_display
def test_missing_redaction_module_explains_itself(tmp_path: Path, monkeypatch) -> None:
    """A missing history-rewrite module must degrade into a dialog, not a crash."""
    from scratchpad import paths
    from scratchpad.attachments import AttachmentStore
    from scratchpad.tokens import TokenCodec, load_or_create_secret
    from scratchpad.ui import window as window_mod
    from scratchpad.ui.window import ScratchpadWindow

    data_dir = tmp_path / "data"
    store = ScratchpadStore.open(data_dir, Config(), app_version="test")
    codec = TokenCodec(load_or_create_secret(paths.secret_key(data_dir)))
    attachments = AttachmentStore(data_dir, codec)
    app = _application("dev.scratchpad.TestRedact")
    shown: list[tuple[str, str]] = []
    monkeypatch.setattr(
        window_mod.dialogs, "error",
        lambda _parent, heading, body: shown.append((heading, body)),
    )
    try:
        window = ScratchpadWindow(
            app, store=store, config=Config(), attachments=attachments, codec=codec
        )
        assert window._redaction_module("no_such_module_here") is None
        assert shown and "unavailable" in shown[0][0].lower()

        # the storage banner is driven from outside and toggles cleanly
        window.show_storage_warning("Storage error: disk full")
        assert window.banner.get_revealed() is True
        assert "disk full" in window.banner.get_title()
        window.show_storage_warning(None)
        assert window.banner.get_revealed() is False
        window.shutdown()
    finally:
        attachments.close()
        if not store.closed:
            store.close()


def _tiny_png() -> bytes:
    """A valid 2x1 PNG, built without an image library."""
    import struct
    import zlib

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", 2, 1, 8, 2, 0, 0, 0)
    raw = b"\x00" + b"\xff\x00\x00" + b"\x00\xff\x00"
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


@needs_display
def test_screenshot_result_becomes_an_image_attachment(tmp_path: Path) -> None:
    """The half of the screenshot flow that does not need a human: file -> token."""
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gio

    from scratchpad import paths
    from scratchpad.attachments import AttachmentKind, AttachmentStore
    from scratchpad.tokens import TokenCodec, load_or_create_secret
    from scratchpad.ui.window import ScratchpadWindow

    Adw.init()
    data_dir = tmp_path / "data"
    store = ScratchpadStore.open(data_dir, Config(), app_version="test")
    codec = TokenCodec(load_or_create_secret(paths.secret_key(data_dir)))
    attachments = AttachmentStore(data_dir, codec)
    app = _application("dev.scratchpad.TestShot")
    try:
        window = ScratchpadWindow(
            app, store=store, config=Config(), attachments=attachments, codec=codec
        )
        shot = tmp_path / "capture.png"
        shot.write_bytes(_tiny_png())
        window.capture._store_screenshot_uri(Gio.File.new_for_path(str(shot)).get_uri(), None)

        stored = attachments.list_all()
        assert len(stored) == 1
        assert stored[0].kind is AttachmentKind.IMAGE
        assert (stored[0].width, stored[0].height) == (2, 1)
        assert stored[0].token in store.text
        assert store.text == window.editor.get_text()
        # The user's own screenshot file is never removed.
        assert shot.exists()
        window.shutdown()
    finally:
        attachments.close()
        if not store.closed:
            store.close()


@needs_display
def test_large_paste_asks_and_small_paste_does_not(tmp_path: Path, monkeypatch) -> None:
    """The guardrail triggers on the configured thresholds, not on a guess."""
    import gi

    gi.require_version("Adw", "1")
    from gi.repository import Adw

    from scratchpad import paths
    from scratchpad.attachments import AttachmentStore
    from scratchpad.tokens import TokenCodec, load_or_create_secret
    from scratchpad.ui import capture as capture_mod
    from scratchpad.ui.window import ScratchpadWindow

    Adw.init()
    data_dir = tmp_path / "data"
    store = ScratchpadStore.open(data_dir, Config(), app_version="test")
    codec = TokenCodec(load_or_create_secret(paths.secret_key(data_dir)))
    attachments = AttachmentStore(data_dir, codec)
    config = Config(large_paste_threshold_lines=3, large_paste_threshold_chars=1000)
    app = _application("dev.scratchpad.TestPaste")
    asked: list[tuple[int, int]] = []

    def fake_large_paste(_parent, *, lines, chars, on_inline, on_attachment):
        asked.append((lines, chars))
        on_attachment()

    monkeypatch.setattr(capture_mod.dialogs, "large_paste", fake_large_paste)
    try:
        window = ScratchpadWindow(
            app, store=store, config=config, attachments=attachments, codec=codec
        )
        window.capture._handle_normal_text("one\ntwo")
        assert asked == []
        assert window.editor.get_text() == "one\ntwo"

        window.capture._handle_normal_text("a\nb\nc\nd\ne")
        assert asked == [(5, 9)]
        stored = attachments.list_all()
        assert len(stored) == 1
        assert stored[0].lines == 5
        assert stored[0].token in store.text
        assert store.text == window.editor.get_text()
        window.shutdown()
    finally:
        attachments.close()
        if not store.closed:
            store.close()


@needs_display
def test_accelerators_are_registered_for_the_documented_commands(tmp_path: Path) -> None:
    """The shortcuts of ARCHITECTURE section 12 reach their actions."""
    import gi

    gi.require_version("Adw", "1")
    from gi.repository import Adw

    from scratchpad import paths
    from scratchpad.attachments import AttachmentStore
    from scratchpad.tokens import TokenCodec, load_or_create_secret
    from scratchpad.ui.window import ScratchpadWindow

    Adw.init()
    data_dir = tmp_path / "data"
    store = ScratchpadStore.open(data_dir, Config(), app_version="test")
    codec = TokenCodec(load_or_create_secret(paths.secret_key(data_dir)))
    attachments = AttachmentStore(data_dir, codec)
    app = _application("dev.scratchpad.TestAccel")
    try:
        window = ScratchpadWindow(
            app, store=store, config=Config(), attachments=attachments, codec=codec
        )
        expected = {
            "win.undo": "<Control>z",
            "win.redo": "<Control><Shift>z",
            "win.paste-as-attachment": "<Control><Shift>v",
            "win.attach-file": "<Control><Shift>a",
            "win.screenshot": "<Control><Shift>s",
            "win.toggle-history": "<Control>h",
            "win.command-palette": "<Control><Shift>p",
            "app.quit": "<Control>q",
        }
        for action, accel in expected.items():
            assert app.get_accels_for_action(action) == _accel(accel), action
        # Keys a widget owns must NOT become window accelerators.
        for action in ("win.indent", "win.outdent", "win.paste", "win.hide-window"):
            assert app.get_accels_for_action(action) == [], action
            assert window.registry.get(action.split(".", 1)[1]).shortcut

        # The palette can be opened and closed like the accelerator does it.
        window.registry.activate("command-palette")
        assert window.palette is not None
        window.palette.populate("palette")
        assert window.palette._rows[0].id == "command-palette"
        window.palette.close()
        window.shutdown()
    finally:
        attachments.close()
        if not store.closed:
            store.close()


@needs_display
def test_redaction_wording_is_the_sanctioned_one() -> None:
    """Spec section 26: the product language is fixed, and never 'secure erase'."""
    from scratchpad.ui import dialogs

    assert dialogs.REDACT_HEADING == "Remove permanently from scratchpad history"
    caveat = dialogs.REDACT_CAVEAT.lower()
    assert "rewrites" in caveat
    assert "backups" in caveat and "snapshots" in caveat
    assert "secure erase" not in caveat
    assert "secure erase" not in dialogs.REDACT_HEADING.lower()


# --------------------------------------------------------------------------- #
# (c) pure line editing helpers
# --------------------------------------------------------------------------- #

def test_continuation_keeps_indentation() -> None:
    assert continuation_for_line("hello", True) == ""
    assert continuation_for_line("    hello", True) == "    "
    assert continuation_for_line("\thello", True) == "\t"


def test_continuation_continues_bullets_only_when_enabled() -> None:
    assert continuation_for_line("- first thing", True) == "- "
    assert continuation_for_line("  - nested", True) == "  - "
    assert continuation_for_line("  - nested", False) == "  "
    # a dash that is not a bullet marker
    assert continuation_for_line("-no space", True) == ""


def test_empty_bullet_is_removed_instead_of_continued() -> None:
    assert continuation_for_line("  - ", True) == "  "
    assert empty_bullet_body("  - ", True) == (2, 4)
    assert empty_bullet_body("  -", True) == (2, 3)
    assert empty_bullet_body("  - text", True) is None
    assert empty_bullet_body("  - ", False) is None
    line = "  - "
    start, end = empty_bullet_body(line, True)
    assert line[:start] + line[end:] == "  "


def test_indent_and_outdent_round_trip() -> None:
    text = "- one\n- two\n- three\n"
    edit = indent_lines(text, 0, len(text) - 1, DEFAULT_INDENT_WIDTH)
    indented = apply_edit(text, edit)
    assert indented == "  - one\n  - two\n  - three\n"
    back = apply_edit(indented, outdent_lines(indented, 0, len(indented) - 1))
    assert back == text


def test_indent_single_line_moves_the_cursor_with_the_text() -> None:
    text = "alpha\nbeta"
    edit = indent_lines(text, 8, 8, 2)  # cursor inside "beta"
    assert apply_edit(text, edit) == "alpha\n  beta"
    assert edit.sel_start == edit.sel_end == 10


def test_outdent_stops_at_the_line_start() -> None:
    text = "    deep"
    edit = outdent_lines(text, 1, 1, 2)
    assert apply_edit(text, edit) == "  deep"
    assert edit.sel_start == 0  # the cursor sat inside the removed indent
    edit = outdent_lines("no indent", 3, 3, 2)
    assert apply_edit("no indent", edit) == "no indent"


def test_outdent_removes_a_tab_as_one_level() -> None:
    assert apply_edit("\tdeep", outdent_lines("\tdeep", 2, 2, 2)) == "deep"


def test_move_lines_up_and_down() -> None:
    text = "one\ntwo\nthree"
    edit = move_lines(text, 4, 4, -1)  # on "two"
    assert apply_edit(text, edit) == "two\none\nthree"
    assert edit.sel_start == edit.sel_end == 0

    edit = move_lines(text, 0, 0, 1)  # "one" down
    assert apply_edit(text, edit) == "two\none\nthree"
    assert edit.sel_start == 4

    assert move_lines(text, 0, 0, -1) is None
    assert move_lines(text, len(text), len(text), 1) is None


def test_move_lines_moves_a_whole_selected_block() -> None:
    text = "a\nb\nc\nd"
    edit = move_lines(text, 2, 5, 1)  # lines "b" and "c"
    assert apply_edit(text, edit) == "a\nd\nb\nc"


def test_block_span_covers_touched_lines_only() -> None:
    text = "one\ntwo\nthree"
    assert block_span(text, 0, 0) == (0, 3)
    assert block_span(text, 1, 5) == (0, 7)
    # a selection ending exactly at a line start does not drag that line in
    assert block_span(text, 0, 4) == (0, 3)


def test_bullet_block_range_takes_deeper_indented_lines() -> None:
    text = "- parent\n  - child\n  more\n- next\n"
    start, end = bullet_block_range(text, 0)
    assert text[start:end] == "- parent\n  - child\n  more\n"
    start, end = bullet_block_range(text, text.index("- next"))
    assert text[start:end] == "- next\n"


def test_bullet_block_range_on_the_last_line_removes_the_newline_in_front() -> None:
    text = "first\nsecond"
    start, end = bullet_block_range(text, len(text) - 1)
    assert text[:start] + text[end:] == "first"


# --------------------------------------------------------------------------- #
# (d) token insertion spacing
# --------------------------------------------------------------------------- #

TOKEN = "Ab3Fmx7QK9v2Rt8Nc4WpLd"


@pytest.mark.parametrize(
    "doc, offset, expected",
    [
        ("", 0, TOKEN),                       # empty document
        ("log: ", 5, TOKEN),                  # space in front already
        ("log:", 4, TOKEN),                   # ':' already breaks the run
        ("log", 3, " " + TOKEN),              # alphanumeric in front
        ("ab", 1, " " + TOKEN + " "),         # alphanumeric on both sides
        ("(x)", 2, " " + TOKEN),              # 'x' before, ')' after
        ("ab", 0, TOKEN + " "),               # alphanumeric after only
        ("- ", 2, TOKEN),                     # after a bullet marker
    ],
)
def test_pad_token_only_pads_where_a_run_would_form(doc, offset, expected) -> None:
    assert pad_token(doc, offset, TOKEN) == expected


def test_padded_token_is_still_a_single_candidate() -> None:
    from scratchpad.tokens import find_candidates

    doc = "prefix"
    padded = pad_token(doc, len(doc), TOKEN)
    result = doc + padded
    assert [candidate for _s, _e, candidate in find_candidates(result)] == [TOKEN]

    # without the padding it would vanish into a longer run
    assert find_candidates(doc + TOKEN) == []


def test_pad_token_ignores_non_ascii_letters() -> None:
    """Only ASCII alphanumerics can extend a token run (spec section 20)."""
    assert pad_token("ü", 1, TOKEN) == TOKEN
