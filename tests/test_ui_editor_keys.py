"""Editor keys under an input method, and the application's cold start details.

Three things are pinned down here:

* the editor's CAPTURE phase key controller must step aside while an input
  method is composing (ibus/fcitx candidate lists, dead keys, Compose), because
  Enter, Tab and Escape belong to the IM then and not to bullet continuation,
  indentation or "hide the window",
* the activation token handed to a cold started GUI through the environment
  (:data:`scratchpad.ui.app.ACTIVATION_TOKEN_ENV`) is consumed exactly once,
* the 250 ms tick source survives anything its callback raises; losing it would
  silently stop fsync batching, heartbeats and checkpoints for good.

The preedit state is tracked through ``GtkTextView::preedit-changed``, the only
public handle on it (the text view keeps its ``GtkIMContext`` private), so the
tests drive that signal directly -- there is no way to make a real input method
compose from a test process.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from scratchpad.config import Config
from scratchpad.core.store import ScratchpadStore

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


@pytest.fixture
def gtk_store(tmp_path: Path):
    """A real store in a temp directory, closed after the test."""
    store = ScratchpadStore.open(tmp_path / "data", Config(), app_version="test")
    yield store
    if not store.closed:
        store.close()


@pytest.fixture
def editor(gtk_store):
    from scratchpad.ui.editor import ScratchpadEditor

    return ScratchpadEditor(gtk_store, Config())


def _press(editor, keyval, state=None) -> bool:
    """Invoke the key controller's handler the way GTK would."""
    from gi.repository import Gdk

    if state is None:
        state = Gdk.ModifierType(0)
    return editor._on_key_pressed(None, keyval, 0, state)


# --------------------------------------------------------------------------- #
# input method / preedit
# --------------------------------------------------------------------------- #

@needs_display
def test_gtk_text_view_still_reports_its_preedit() -> None:
    """The whole approach rests on this signal; fail loudly if GTK drops it."""
    from gi.repository import GObject, Gtk

    Gtk.TextView()  # make sure the class is initialized before looking signals up
    assert GObject.signal_lookup("preedit-changed", Gtk.TextView) != 0


@needs_display
def test_preedit_hands_the_im_keys_back_to_the_text_view(editor) -> None:
    """While a composition is on screen Enter/Tab/Escape are the IM's keys."""
    from gi.repository import Gdk

    editor.insert_at_cursor("- item")
    hidden: list[bool] = []
    editor.on_escape = lambda: hidden.append(True)

    editor.text_view.emit("preedit-changed", "あ")  # a pending CJK candidate
    assert editor._preedit_active is True

    for keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_ISO_Enter,
                   Gdk.KEY_Tab, Gdk.KEY_KP_Tab, Gdk.KEY_ISO_Left_Tab,
                   Gdk.KEY_Escape):
        assert _press(editor, keyval) is False, Gdk.keyval_name(keyval)
    assert _press(editor, Gdk.KEY_Return, Gdk.ModifierType.SHIFT_MASK) is False

    assert editor.get_text() == "- item"  # nothing was inserted, indented or removed
    assert hidden == []                   # and the window stayed on screen


@needs_display
def test_the_keys_come_back_when_the_composition_ends(editor) -> None:
    """An empty preedit string means committed or cancelled: we own them again."""
    from gi.repository import Gdk

    editor.insert_at_cursor("- item")
    editor.text_view.emit("preedit-changed", "あ")
    editor.text_view.emit("preedit-changed", "")
    assert editor._preedit_active is False

    assert _press(editor, Gdk.KEY_Return) is True
    assert editor.get_text() == "- item\n- "      # bullet continuation is back
    assert _press(editor, Gdk.KEY_Tab) is True
    assert editor.get_text() == "- item\n  - "    # and so is indentation

    hidden: list[bool] = []
    editor.on_escape = lambda: hidden.append(True)
    assert _press(editor, Gdk.KEY_Escape) is True
    assert hidden == [True]


@needs_display
def test_a_preedit_does_not_disable_the_editor_shortcuts(editor) -> None:
    """Only the keys an input method actually uses are handed back."""
    from gi.repository import Gdk

    editor.insert_at_cursor("- first\n- second")
    editor.text_view.emit("preedit-changed", "あ")

    assert _press(editor, Gdk.KEY_Up, Gdk.ModifierType.ALT_MASK) is True
    assert editor.get_text() == "- second\n- first"
    assert _press(editor, Gdk.KEY_k,
                  Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK) is True
    assert editor.get_text() == "- first"  # the moved line, which the cursor followed


@needs_display
def test_focus_loss_clears_a_stale_preedit_flag(editor) -> None:
    """A flag that outlived its composition would wedge Enter, Tab and Escape."""
    from gi.repository import Gdk, Gtk

    editor.text_view.emit("preedit-changed", "あ")
    assert editor._preedit_active is True

    controllers = [
        c for c in editor.text_view.observe_controllers()
        if isinstance(c, Gtk.EventControllerFocus)
    ]
    assert controllers, "the editor installs a focus controller"
    for controller in controllers:
        controller.emit("leave")

    assert editor._preedit_active is False
    assert _press(editor, Gdk.KEY_Return) is True


@needs_display
def test_the_key_controller_captures_ahead_of_the_text_view(editor) -> None:
    """GtkTextView eats Return and Tab itself, so ours has to run first."""
    from gi.repository import Gtk

    phases = [
        c.get_propagation_phase()
        for c in editor.text_view.observe_controllers()
        if isinstance(c, Gtk.EventControllerKey)
    ]
    assert Gtk.PropagationPhase.CAPTURE in phases


# --------------------------------------------------------------------------- #
# cold start activation token
# --------------------------------------------------------------------------- #

def test_pop_activation_token_consumes_the_variable() -> None:
    from scratchpad.ui.app import ACTIVATION_TOKEN_ENV, pop_activation_token

    environ = {ACTIVATION_TOKEN_ENV: "wayland-token-42", "OTHER": "kept"}
    assert pop_activation_token(environ) == "wayland-token-42"
    assert environ == {"OTHER": "kept"}          # single use: taken, not copied
    assert pop_activation_token(environ) is None


@pytest.mark.parametrize("environ", [{}, {"SCRATCHPAD_ACTIVATION_TOKEN": ""}])
def test_pop_activation_token_without_a_usable_token(environ: dict[str, str]) -> None:
    from scratchpad.ui.app import ACTIVATION_TOKEN_ENV, pop_activation_token

    assert pop_activation_token(environ) is None
    assert ACTIVATION_TOKEN_ENV not in environ


def _application() -> object:
    from scratchpad.ui.app import ScratchpadApplication

    return ScratchpadApplication(
        store=SimpleNamespace(tick=lambda: None, closed=False),
        config=SimpleNamespace(heartbeat_seconds=5),
        data_dir="/nonexistent",
    )


def test_do_activate_forwards_the_cold_start_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """The first press of the global shortcut must present a focused window."""
    from scratchpad.ui.app import ACTIVATION_TOKEN_ENV, ScratchpadApplication

    shown: list[str | None] = []
    app = _application()
    app.window = SimpleNamespace(show_window=shown.append)

    monkeypatch.setenv(ACTIVATION_TOKEN_ENV, "wayland-token-42")
    ScratchpadApplication.do_activate(app)
    assert shown == ["wayland-token-42"]
    assert ACTIVATION_TOKEN_ENV not in os.environ  # not reused on the next activation

    ScratchpadApplication.do_activate(app)
    assert shown == ["wayland-token-42", None]


def test_do_activate_without_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    from scratchpad.ui.app import ACTIVATION_TOKEN_ENV, ScratchpadApplication

    monkeypatch.delenv(ACTIVATION_TOKEN_ENV, raising=False)
    shown: list[str | None] = []
    app = _application()
    app.window = SimpleNamespace(show_window=shown.append)
    ScratchpadApplication.do_activate(app)
    assert shown == [None]


# --------------------------------------------------------------------------- #
# the tick source must never die
# --------------------------------------------------------------------------- #

def test_the_tick_survives_a_failing_storage_poll() -> None:
    """Returning anything but True would drop fsync batching and checkpoints."""
    from gi.repository import GLib

    from scratchpad.ui.app import ScratchpadApplication

    ticks: list[int] = []
    app = _application()
    app.store = SimpleNamespace(tick=lambda: ticks.append(1))
    app._poll_storage_error = lambda: (_ for _ in ()).throw(RuntimeError("banner boom"))

    for _ in range(3):
        assert ScratchpadApplication._on_tick(app) is GLib.SOURCE_CONTINUE
    assert ticks == [1, 1, 1]  # the store keeps being ticked


def test_the_tick_survives_a_failing_store() -> None:
    from gi.repository import GLib

    from scratchpad.ui.app import ScratchpadApplication

    polls: list[int] = []
    app = _application()
    app.store = SimpleNamespace(tick=lambda: (_ for _ in ()).throw(OSError("no space")))
    app._poll_storage_error = lambda: polls.append(1)

    assert ScratchpadApplication._on_tick(app) is GLib.SOURCE_CONTINUE
    assert app._tick_error == "no space"
    assert polls == [1]  # the banner is still polled, so the failure becomes visible
