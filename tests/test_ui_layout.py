"""Regression tests for the window layout fixes (`scratchpad.ui.layout`).

Two behaviours the user hit on first use:

1. Showing or hiding the history panel changed the window size dramatically.
   GTK answered "width for height" for the wrapping button row by returning
   the width of the *unwrapped* single row, which became the window minimum.
2. A populated preview pane (a large `Gtk.Picture`) grew to its natural size,
   so the two panes no longer split the sidebar 50/50 and the window widened.

Both are fixed by `NaturalClamp` (natural == minimum, never width-for-height)
around the preview stack and the history panel, plus a homogeneous sidebar.

GTK needs a display.  The whole module is skipped when none is available.
"""

from __future__ import annotations

from itertools import count
from pathlib import Path
from types import SimpleNamespace

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")

from gi.repository import Gdk, GLib, Gtk  # noqa: E402

if not Gtk.init_check() or Gdk.Display.get_default() is None:  # pragma: no cover
    pytest.skip("no display available for GTK tests", allow_module_level=True)

from scratchpad.attachments import AttachmentStore  # noqa: E402
from scratchpad.config import Config  # noqa: E402
from scratchpad.core.store import ScratchpadStore  # noqa: E402
from scratchpad.tokens import TokenCodec, load_or_create_secret  # noqa: E402
from scratchpad.ui.layout import NaturalClamp  # noqa: E402
from tests.test_ui_app import _application  # noqa: E402
from tests.test_ui_widgets import make_png  # noqa: E402

H = Gtk.Orientation.HORIZONTAL
V = Gtk.Orientation.VERTICAL

# Wide enough that a single unwrapped row is unmistakably "too wide".
BUTTON_COUNT = 24

# One registered application per window: a D-Bus object path may be exported
# only once per process.
_APP_IDS = count()


def _minimum(widget: Gtk.Widget, orientation: Gtk.Orientation, for_size: int = -1) -> int:
    return widget.measure(orientation, for_size)[0]


def _natural(widget: Gtk.Widget, orientation: Gtk.Orientation, for_size: int = -1) -> int:
    return widget.measure(orientation, for_size)[1]


def _wrapping_row() -> Gtk.FlowBox:
    """A FlowBox of many buttons: tall when narrow, very wide when unwrapped."""
    box = Gtk.FlowBox()
    box.set_selection_mode(Gtk.SelectionMode.NONE)
    box.set_min_children_per_line(1)
    box.set_max_children_per_line(BUTTON_COUNT)
    for i in range(BUTTON_COUNT):
        box.append(Gtk.Button(label=f"Action number {i}"))
    return box


def _host(child: Gtk.Widget) -> Gtk.Window:
    """Put ``child`` in a never-presented window so CSS styling resolves."""
    window = Gtk.Window()
    window.set_child(child)
    return window


# ----------------------------------------------------------------- NaturalClamp


class TestNaturalClamp:
    def test_request_mode_is_height_for_width(self) -> None:
        clamp = NaturalClamp(Gtk.Label(label="x"))
        assert clamp.get_request_mode() == Gtk.SizeRequestMode.HEIGHT_FOR_WIDTH

    def test_empty_or_hidden_child_measures_zero(self) -> None:
        clamp = NaturalClamp()
        assert clamp.measure(H, -1)[:2] == (0, 0)
        assert clamp.measure(V, -1)[:2] == (0, 0)

        label = Gtk.Label(label="hidden")
        label.set_visible(False)
        clamp.set_child(label)
        assert clamp.measure(H, -1)[:2] == (0, 0)
        assert clamp.get_child() is label

    def test_natural_equals_minimum_in_both_orientations(self) -> None:
        picture = Gtk.Picture()
        picture.set_can_shrink(True)
        picture.set_paintable(
            Gdk.Texture.new_from_bytes(GLib.Bytes.new(make_png(1400, 900)))
        )
        clamp = NaturalClamp(picture)
        window = _host(clamp)
        try:
            # The picture itself wants its full size ...
            assert _natural(picture, H) >= 1400
            # ... the clamp does not pass that on.
            for orientation in (H, V):
                minimum, natural, _, _ = clamp.measure(orientation, -1)
                assert natural == minimum, orientation
            assert _natural(clamp, H) < 1400
        finally:
            window.set_child(None)

    def test_width_is_never_derived_from_height(self) -> None:
        """A short height must not make the clamp as wide as the unwrapped row."""
        row = _wrapping_row()
        clamp = NaturalClamp(row)
        window = _host(clamp)
        try:
            unconstrained = _minimum(clamp, H)
            single_row = _natural(row, H)
            assert single_row > 4 * unconstrained, (single_row, unconstrained)
            # Ask for the width that fits a very short height, the query that
            # used to return the full single row.
            for height in (1, 40, 120):
                assert _minimum(clamp, H, height) == unconstrained, height
                assert _natural(clamp, H, height) == unconstrained, height
            # Height still follows width: narrow means taller.
            assert _minimum(clamp, V, unconstrained) > _minimum(clamp, V, single_row)
        finally:
            window.set_child(None)

    def test_replacing_child_reparents(self) -> None:
        first = Gtk.Label(label="first")
        second = Gtk.Label(label="second")
        clamp = NaturalClamp(first)
        assert first.get_parent() is clamp
        clamp.set_child(second)
        assert first.get_parent() is None
        assert second.get_parent() is clamp
        clamp.set_child(None)
        assert second.get_parent() is None
        assert clamp.get_child() is None


# ---------------------------------------------------------------- main window


@pytest.fixture
def ui(tmp_path: Path):
    """A window on a real store in ``tmp_path`` plus one large image attachment."""
    gi.require_version("Adw", "1")
    from gi.repository import Adw

    from scratchpad import paths
    from scratchpad.ui.window import ScratchpadWindow

    Adw.init()
    data_dir = tmp_path / "data"
    store = ScratchpadStore.open(data_dir, Config(), app_version="test")
    codec = TokenCodec(load_or_create_secret(paths.secret_key(data_dir)))
    attachments = AttachmentStore(data_dir, codec)
    app = _application(f"dev.scratchpad.TestLayout{next(_APP_IDS)}")
    window = ScratchpadWindow(
        app, store=store, config=Config(), attachments=attachments, codec=codec
    )
    image = attachments.create_image(make_png(1400, 900), mime="image/png")
    try:
        yield SimpleNamespace(
            window=window,
            attachments=attachments,
            image=attachments.resolve(image.token),
            config=Config(),
        )
    finally:
        window.shutdown()
        attachments.close()
        if not store.closed:
            store.close()


class TestWindowLayout:
    def test_sidebar_splits_panes_evenly(self, ui) -> None:
        assert ui.window.sidebar.get_homogeneous() is True

    def test_history_container_follows_panel_visibility(self, ui) -> None:
        window = ui.window
        assert window.history_container.get_child() is window.history_panel
        assert window.history_container.get_visible() is False
        window.history_button.set_active(True)
        assert window.history_panel.get_visible() is True
        assert window.history_container.get_visible() is True
        window.history_button.set_active(False)
        assert window.history_container.get_visible() is False

    def test_showing_history_does_not_widen_the_window(self, ui) -> None:
        window = ui.window
        hidden = window.main_paned.measure(H, -1)[:2]
        window.history_button.set_active(True)

        # The unwrapped history button row is far wider than the window; the
        # panel must wrap into whatever width it gets instead.
        single_row = _natural(window.history_buttons, H)
        shown = window.main_paned.measure(H, -1)[:2]
        assert shown[0] < ui.config.width, (shown, single_row)
        assert shown[1] < ui.config.width, (shown, single_row)
        assert shown[0] <= hidden[0] + 50, (hidden, shown)

        # The query GTK used to blow up: width for a short paned height.
        for height in (200, 300, int(ui.config.height)):
            width_for_height = window.main_paned.measure(H, height)[:2]
            assert width_for_height[0] < ui.config.width, (height, width_for_height)
            assert width_for_height[1] < ui.config.width, (height, width_for_height)

    def test_populated_pane_does_not_change_sidebar_size(self, ui) -> None:
        window = ui.window
        before = window.sidebar.measure(H, -1)[:2]

        window.cursor_pane.show_resolution(ui.image, ui.attachments)
        assert window.cursor_pane.current_page_name() == "image"
        assert _natural(window.cursor_pane.picture, H) >= 1400
        one = window.sidebar.measure(H, -1)[:2]
        assert one == before

        window.hover_pane.show_resolution(ui.image, ui.attachments)
        both = window.sidebar.measure(H, -1)[:2]
        assert both == before

        window.cursor_pane.clear()
        assert window.sidebar.measure(H, -1)[:2] == before

    def test_populated_pane_requests_fit_the_sidebar(self, ui) -> None:
        """Neither frame asks for more than the sidebar's fixed width request.

        The homogeneous sidebar can only split 50/50 when no pane forces it
        wider; a 1400 px picture must stay hidden behind the clamp.
        """
        window = ui.window
        sidebar_request = window.sidebar.get_size_request()[0]
        frames = [window.cursor_pane.get_parent(), window.hover_pane.get_parent()]
        window.cursor_pane.show_resolution(ui.image, ui.attachments)
        window.hover_pane.show_resolution(ui.image, ui.attachments)
        for frame in frames:
            minimum, natural, _, _ = frame.measure(H, -1)
            assert natural < sidebar_request, (minimum, natural, sidebar_request)
            # And a short frame (each pane gets half the sidebar height) must
            # not ask for a wider allocation than an unconstrained one.
            assert frame.measure(H, 150)[1] <= natural
