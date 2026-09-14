"""Tests for the Agent D2 widgets.

`PreviewPane`, `TimelineWidget`, `LineNumberedTextView` and `DiffView` are
exercised against stand-in objects that implement only the attribute names from
`ARCHITECTURE.md` sections 6 and 10, so these tests do not depend on
`scratchpad.core.history` or `scratchpad.attachments` existing.

GTK needs a display.  The whole module is skipped when none is available.
"""

from __future__ import annotations

import re
import struct
import time
import zlib
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")

from gi.repository import Gdk, GLib, Gtk  # noqa: E402

if not Gtk.init_check() or Gdk.Display.get_default() is None:  # pragma: no cover
    pytest.skip("no display available for GTK tests", allow_module_level=True)

from scratchpad.ui.diffview import DiffView  # noqa: E402
from scratchpad.ui.preview import MISSING_MESSAGE, PreviewPane  # noqa: E402
from scratchpad.ui.textview_extras import LineNumberedTextView  # noqa: E402
from scratchpad.ui.timeline import TimelineWidget  # noqa: E402

NS = 1_000_000_000
TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")


def pump(iterations: int = 20) -> None:
    """Run a few main loop iterations without blocking."""
    context = GLib.MainContext.default()
    for _ in range(iterations):
        if not context.pending():
            break
        context.iteration(False)


def pump_until(predicate, timeout: float = 1.0) -> bool:
    """Iterate the main loop until `predicate()` is true or `timeout` elapses."""
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


# --------------------------------------------------------------- stand-in data


class Kind(StrEnum):
    """Stand-in for `AttachmentKind` (compared via str()/.value)."""

    TEXT = "text"
    IMAGE = "image"
    FILE = "file"


@dataclass(frozen=True)
class State:
    """Stand-in for `ResolutionState` (compared via .name)."""

    name: str


ORDINARY = State("ORDINARY")
VALID = State("VALID")
MISSING = State("MISSING")


@dataclass(frozen=True)
class Att:
    """Stand-in for `Attachment`."""

    object_id: int
    token: str
    kind: object
    mime: str
    sha256: str = "0" * 64
    size: int = 0
    created_wall_ns: int = 1_757_000_000 * NS
    lines: int | None = None
    chars: int | None = None
    encoding: str | None = None
    width: int | None = None
    height: int | None = None
    filename: str | None = None


@dataclass(frozen=True)
class Res:
    """Stand-in for `Resolution`."""

    state: State
    token: str
    object_id: int | None = None
    attachment: Att | None = None
    detail: str = ""


@dataclass
class Store:
    """Stand-in for `AttachmentStore` counting blob reads."""

    blobs: dict[int, bytes] = field(default_factory=dict)
    paths: dict[int, Path] = field(default_factory=dict)
    reads: int = 0

    def read_blob(self, att: Att) -> bytes:
        self.reads += 1
        return self.blobs[att.object_id]

    def blob_path(self, att: Att) -> Path:
        return self.paths[att.object_id]


@dataclass
class Session:
    """Stand-in for `core.history.Session`."""

    session_id: int
    start_wall_ns: int
    end_wall_ns: int
    clean_stop: bool = True
    first_seq: int = 0
    last_seq: int = 0


@dataclass
class FakeHistory:
    """Stand-in for `core.history.History` counting activity() calls."""

    _sessions: list[Session]
    activity_calls: int = 0

    def sessions(self) -> list[Session]:
        return list(self._sessions)

    def time_range(self) -> tuple[int, int]:
        if not self._sessions:
            return (0, 0)
        return self._sessions[0].start_wall_ns, self._sessions[-1].end_wall_ns

    def activity(self, start: int, end: int, buckets: int) -> list[int]:
        self.activity_calls += 1
        return [i % 7 for i in range(buckets)]


def make_png(width: int, height: int) -> bytes:
    """A minimal valid RGB PNG."""
    raw = b"".join(b"\x00" + bytes((200, 40, 60)) * width for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


@pytest.fixture
def store(tmp_path: Path) -> Store:
    """A store holding one text, one image and one file attachment."""
    blob = tmp_path / "file.bin"
    blob.write_bytes(b"x" * 4096)
    return Store(
        blobs={
            1: b"alpha\nBETA\ngamma\ndelta beta\n",
            2: make_png(4, 3),
            3: blob.read_bytes(),
        },
        paths={3: blob},
    )


@pytest.fixture
def text_res() -> Res:
    att = Att(
        object_id=1,
        token="T" * 22,
        kind=Kind.TEXT,
        mime="text/plain",
        size=28,
        lines=18_431,
        chars=1_203_004,
        encoding="utf-8",
    )
    return Res(VALID, att.token, 1, att)


@pytest.fixture
def image_res() -> Res:
    att = Att(
        object_id=2,
        token="I" * 22,
        kind=Kind.IMAGE,
        mime="image/png",
        size=100,
        width=4,
        height=3,
        filename="shot.png",
    )
    return Res(VALID, att.token, 2, att)


@pytest.fixture
def file_res() -> Res:
    att = Att(
        object_id=3,
        token="F" * 22,
        kind=Kind.FILE,
        mime="application/pdf",
        size=4096,
        filename="invoice.pdf",
    )
    return Res(VALID, att.token, 3, att)


# ------------------------------------------------------ LineNumberedTextView


def test_set_text_and_get_text() -> None:
    view = LineNumberedTextView()
    view.set_text("one\ntwo\nthree")
    assert view.get_text() == "one\ntwo\nthree"
    assert view.get_line_count() == 3
    assert view.buffer.get_char_count() == len("one\ntwo\nthree")


def test_editable_defaults_to_false() -> None:
    view = LineNumberedTextView()
    assert view.get_editable() is False
    view.set_editable(True)
    assert view.get_editable() is True
    assert view.text_view.get_cursor_visible() is True
    assert view.text_view.get_wrap_mode() == Gtk.WrapMode.NONE


def test_search_forward_is_case_insensitive_and_selects() -> None:
    view = LineNumberedTextView()
    view.set_text("alpha\nBETA\ngamma\ndelta beta\n")
    assert view.search("beta") is True
    start, end = view.buffer.get_selection_bounds()
    assert (start.get_offset(), end.get_offset()) == (6, 10)
    assert view.get_selected_text() == "BETA"


def test_search_forward_advances_then_wraps() -> None:
    view = LineNumberedTextView()
    view.set_text("alpha\nBETA\ngamma\ndelta beta\n")
    assert view.search("beta") is True
    assert view.search("beta") is True
    start, _end = view.buffer.get_selection_bounds()
    assert start.get_offset() == 23  # the second occurrence
    assert view.search("beta") is True  # wraps back to the first
    start, _end = view.buffer.get_selection_bounds()
    assert start.get_offset() == 6


def test_search_backward_wraps() -> None:
    view = LineNumberedTextView()
    view.set_text("alpha\nBETA\ngamma\ndelta beta\n")
    assert view.search("beta", forward=False) is True
    start, _end = view.buffer.get_selection_bounds()
    assert start.get_offset() == 23  # wrapped to the last occurrence
    assert view.search("beta", forward=False) is True
    start, _end = view.buffer.get_selection_bounds()
    assert start.get_offset() == 6


def test_search_misses_and_empty_needle() -> None:
    view = LineNumberedTextView()
    view.set_text("alpha\nbeta\n")
    assert view.search("") is False
    assert view.search("nothing here") is False


def test_scroll_helpers_move_the_cursor() -> None:
    view = LineNumberedTextView()
    view.set_text("\n".join(str(i) for i in range(500)))
    view.scroll_to_end()
    assert view.buffer.get_iter_at_mark(view.buffer.get_insert()).get_offset() == (
        view.buffer.get_char_count()
    )
    view.scroll_to_start()
    assert view.buffer.get_iter_at_mark(view.buffer.get_insert()).get_offset() == 0
    view.scroll_to_line(42)
    assert view.buffer.get_iter_at_mark(view.buffer.get_insert()).get_line() == 42


def test_gutter_width_grows_with_line_count() -> None:
    view = LineNumberedTextView()
    view.set_text("a\nb\n")
    narrow = view.gutter.get_content_width()
    view.set_text("\n".join(str(i) for i in range(10_000)))
    assert view.gutter.get_content_width() > narrow


def test_widgets_draw_inside_a_real_window(store: Store, text_res: Res) -> None:
    """Realize every widget once and make sure drawing raises nothing."""
    window = Gtk.Window()
    window.set_default_size(700, 500)
    window.set_decorated(False)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

    view = LineNumberedTextView()
    view.set_text("\n".join(f"line {i}" for i in range(400)))
    box.append(view)

    timeline = TimelineWidget()
    now = int(time.time() * NS)
    timeline.set_history(FakeHistory([Session(1, now - 3600 * NS, now)]))
    timeline.set_selected(now - 1800 * NS)
    box.append(timeline)

    pane = PreviewPane("Cursor")
    pane.show_resolution(text_res, store)
    box.append(pane)

    diff = DiffView()
    diff.set_texts("a\nb\n", "a\nc\n")
    box.append(diff)

    window.set_child(box)
    window.present()
    pump(200)
    view.gutter.queue_draw()
    timeline.queue_draw()
    pump(200)
    assert timeline.get_width() > 0
    window.destroy()
    pump()


# -------------------------------------------------------------- PreviewPane


def test_preview_blank_for_none_and_ordinary() -> None:
    pane = PreviewPane("Hover")
    assert pane.get_title() == "Hover"
    assert pane.current_page_name() == "blank"
    pane.show_resolution(None, None)
    assert pane.current_page_name() == "blank"
    pane.show_resolution(Res(ORDINARY, "z" * 22), None)
    assert pane.current_page_name() == "blank"
    assert pane.current_token() is None


def test_preview_text_page(store: Store, text_res: Res) -> None:
    pane = PreviewPane("Cursor")
    pane.show_resolution(text_res, store)
    assert pane.current_page_name() == "text"
    assert pane.text_view.get_text() == "alpha\nBETA\ngamma\ndelta beta\n"
    meta = pane._text_meta.get_label()
    assert meta.startswith("18,431 lines, 1,203,004 chars, created ")
    assert "utf-8" in meta


def test_preview_text_search_entry_selects(store: Store, text_res: Res) -> None:
    pane = PreviewPane("Cursor")
    pane.show_resolution(text_res, store)
    pane.search_entry.set_text("gamma")
    assert pump_until(lambda: pane.text_view.get_selected_text() == "gamma")
    # Enter searches forward, Shift+Enter backward; both wrap.
    pane.search_entry.set_text("beta")
    assert pump_until(lambda: pane.text_view.get_selected_text().lower() == "beta")
    first = pane.text_view.buffer.get_selection_bounds()[0].get_offset()
    pane.search_entry.emit("activate")
    second = pane.text_view.buffer.get_selection_bounds()[0].get_offset()
    assert second != first
    pane._run_search(forward=False)
    assert pane.text_view.buffer.get_selection_bounds()[0].get_offset() == first


def test_preview_first_last_buttons(store: Store, text_res: Res) -> None:
    pane = PreviewPane("Cursor")
    pane.show_resolution(text_res, store)
    pane._last_button.emit("clicked")
    buffer = pane.text_view.buffer
    assert buffer.get_iter_at_mark(buffer.get_insert()).get_offset() == buffer.get_char_count()
    pane._first_button.emit("clicked")
    assert buffer.get_iter_at_mark(buffer.get_insert()).get_offset() == 0


def test_preview_image_page(store: Store, image_res: Res) -> None:
    pane = PreviewPane("Hover")
    pane.show_resolution(image_res, store)
    assert pane.current_page_name() == "image"
    assert pane.picture.get_paintable() is not None
    assert pane.picture.get_content_fit() == Gtk.ContentFit.CONTAIN
    meta = pane._image_meta.get_label()
    assert meta.startswith("PNG 4x3, created ")
    assert TIMESTAMP_RE.search(meta)


def test_preview_file_page(store: Store, file_res: Res) -> None:
    pane = PreviewPane("Hover")
    pane.show_resolution(file_res, store)
    assert pane.current_page_name() == "file"
    assert pane._file_name.get_label() == "invoice.pdf"
    meta = pane._file_meta.get_label()
    assert "application/pdf" in meta
    assert "4.0 KB" in meta
    assert pane.open_button.get_sensitive() is True
    assert pane.open_button.get_label() == "Open externally"


def test_preview_file_without_blob_disables_open(file_res: Res) -> None:
    empty = Store(blobs={}, paths={})
    pane = PreviewPane("Hover")
    pane.show_resolution(file_res, empty)
    assert pane.current_page_name() == "file"
    assert pane.open_button.get_sensitive() is False


def test_preview_missing_page_uses_exact_wording() -> None:
    pane = PreviewPane("Cursor")
    res = Res(MISSING, "M" * 22, 9, None, "blob 3f9c not found")
    pane.show_resolution(res, None)
    assert pane.current_page_name() == "missing"
    assert pane._missing_label.get_label() == MISSING_MESSAGE
    assert pane._missing_label.get_label() == (
        "Valid local attachment reference, but the backing object is unavailable or corrupt."
    )
    assert pane._missing_detail.get_label() == "blob 3f9c not found"


def test_preview_unreadable_blob_falls_back_to_missing(image_res: Res) -> None:
    pane = PreviewPane("Hover")
    pane.show_resolution(image_res, Store(blobs={2: b"not an image"}))
    assert pane.current_page_name() == "missing"
    assert "decoded" in pane._missing_detail.get_label()


def test_preview_caches_the_last_token(store: Store, text_res: Res, image_res: Res) -> None:
    pane = PreviewPane("Cursor")
    pane.show_resolution(text_res, store)
    assert store.reads == 1
    pane.show_resolution(text_res, store)
    pane.show_resolution(text_res, store)
    assert store.reads == 1  # same token: no reload
    pane.show_resolution(image_res, store)
    assert store.reads == 2
    pane.show_resolution(text_res, store)
    assert store.reads == 3


def test_preview_clear_and_title() -> None:
    pane = PreviewPane("Cursor")
    pane.set_title("Hover")
    assert pane.get_title() == "Hover"
    pane.clear()
    assert pane.current_page_name() == "blank"


def test_preview_accepts_plain_string_kinds(store: Store) -> None:
    att = Att(object_id=1, token="S" * 22, kind="text", mime="text/plain", size=28)
    pane = PreviewPane("Cursor")
    pane.show_resolution(Res(VALID, att.token, 1, att), store)
    assert pane.current_page_name() == "text"


def test_preview_truncates_huge_text(monkeypatch: pytest.MonkeyPatch) -> None:
    from scratchpad.ui import preview as preview_module

    monkeypatch.setattr(preview_module, "MAX_PREVIEW_BYTES", 32)
    att = Att(object_id=1, token="L" * 22, kind=Kind.TEXT, mime="text/plain", size=1000)
    pane = PreviewPane("Cursor")
    pane.show_resolution(Res(VALID, att.token, 1, att), Store(blobs={1: b"y" * 1000}))
    assert pane.current_page_name() == "text"
    assert "preview truncated" in pane.text_view.get_text()
    assert "(preview truncated)" in pane._text_meta.get_label()


# ----------------------------------------------------------- TimelineWidget


@pytest.fixture
def history() -> FakeHistory:
    base = 1_757_000_000 * NS
    return FakeHistory(
        [
            Session(1, base, base + 3600 * NS),
            Session(2, base + 7200 * NS, base + 10800 * NS),
        ]
    )


def test_timeline_defaults_to_padded_full_range(history: FakeHistory) -> None:
    timeline = TimelineWidget()
    timeline.set_history(history)
    start, end = timeline.get_visible_range()
    data_start, data_end = history.time_range()
    pad = int((data_end - data_start) * 0.02)
    assert start == data_start - pad
    assert end == data_end + pad
    assert timeline.has_data() is True


def test_timeline_layout_geometry(history: FakeHistory) -> None:
    timeline = TimelineWidget()
    timeline.set_history(history)
    lay = timeline._layout(600, 88)
    assert lay["plot_x"] == 10.0
    assert lay["plot_w"] == 580.0
    assert lay["buckets"] == float(580 // 3)
    assert lay["track_y"] > lay["ticks_y"]
    assert lay["heat_y"] > lay["track_y"]
    assert lay["heat_y"] + lay["heat_h"] <= 88
    assert lay["span_ns"] == float(lay["end_ns"] - lay["start_ns"])


def test_timeline_time_to_x_round_trip(history: FakeHistory) -> None:
    timeline = TimelineWidget()
    timeline.set_history(history)
    start, end = timeline.get_visible_range()
    assert timeline._time_to_x(start, 600) == pytest.approx(10.0)
    assert timeline._time_to_x(end, 600) == pytest.approx(590.0)
    middle = (start + end) // 2
    assert timeline._time_to_x(middle, 600) == pytest.approx(300.0, abs=1.0)
    for x in (10.0, 123.5, 300.0, 589.0):
        assert timeline._time_to_x(timeline._x_to_time(x, 600), 600) == pytest.approx(x, abs=0.01)


def test_timeline_set_visible_range_and_invalid_input(history: FakeHistory) -> None:
    timeline = TimelineWidget()
    timeline.set_history(history)
    timeline.set_visible_range(1000 * NS, 2000 * NS)
    assert timeline.get_visible_range() == (1000 * NS, 2000 * NS)
    timeline.set_visible_range(5000 * NS, 5000 * NS)  # degenerate: widened
    start, end = timeline.get_visible_range()
    assert end > start
    timeline.reset_view()
    assert timeline.get_visible_range()[0] < history.time_range()[0]


def test_timeline_zoom_keeps_the_anchor_time(history: FakeHistory) -> None:
    timeline = TimelineWidget()
    timeline.set_history(history)
    before = timeline.get_visible_range()
    anchor = timeline._x_to_time(200.0, 600)
    timeline._zoom_at(200.0, 0.5)
    after = timeline.get_visible_range()
    assert (after[1] - after[0]) == pytest.approx((before[1] - before[0]) * 0.5, rel=1e-6)
    assert timeline._x_to_time(200.0, 600) == pytest.approx(anchor, abs=(after[1] - after[0]) / 500)


def test_timeline_zoom_is_clamped(history: FakeHistory) -> None:
    timeline = TimelineWidget()
    timeline.set_history(history)
    for _ in range(200):
        timeline._zoom_at(300.0, 0.5)
    start, end = timeline.get_visible_range()
    assert end - start >= 1_000_000
    for _ in range(200):
        timeline._zoom_at(300.0, 2.0)
    start, end = timeline.get_visible_range()
    data_span = history.time_range()[1] - history.time_range()[0]
    assert end - start <= max(data_span * 8, 3600 * NS) + 2


def test_timeline_pan_preserves_span(history: FakeHistory) -> None:
    timeline = TimelineWidget()
    timeline.set_history(history)
    start, end = timeline.get_visible_range()
    timeline._on_pan_begin(None, 0.0, 0.0)
    timeline._on_pan_update(None, -100.0, 0.0)
    new_start, new_end = timeline.get_visible_range()
    assert new_end - new_start == pytest.approx(end - start, rel=1e-9)
    assert new_start > start


def test_timeline_scrub_emits_time_selected(history: FakeHistory) -> None:
    timeline = TimelineWidget()
    timeline.set_history(history)
    seen: list[int] = []
    timeline.connect("time-selected", lambda _w, value: seen.append(value))
    timeline._scrub_to_x(300.0, force=True)
    assert len(seen) == 1
    assert seen[0] == pytest.approx(timeline._x_to_time(300.0), abs=2)
    assert timeline.get_selected() == seen[0]


def test_timeline_scrub_is_throttled(history: FakeHistory) -> None:
    timeline = TimelineWidget()
    timeline.set_history(history)
    seen: list[int] = []
    timeline.connect("time-selected", lambda _w, value: seen.append(value))
    timeline._scrub_to_x(100.0, force=True)
    for x in range(101, 140):
        timeline._scrub_to_x(float(x))
    assert len(seen) == 1  # the drag updates were throttled away
    assert timeline.get_selected() == pytest.approx(timeline._x_to_time(139.0), abs=2)
    timeline._scrub_to_x(139.0, force=True)  # release always emits
    assert len(seen) == 2


def test_timeline_scrub_clamps_to_the_visible_range(history: FakeHistory) -> None:
    timeline = TimelineWidget()
    timeline.set_history(history)
    start, end = timeline.get_visible_range()
    timeline._scrub_to_x(-500.0, force=True)
    assert timeline.get_selected() == start
    timeline._scrub_to_x(5000.0, force=True)
    assert timeline.get_selected() == end


def test_timeline_keyboard_navigation(history: FakeHistory) -> None:
    timeline = TimelineWidget()
    timeline.set_history(history)
    seen: list[int] = []
    timeline.connect("time-selected", lambda _w, value: seen.append(value))
    start, end = timeline.get_visible_range()
    span = end - start

    assert timeline._on_key_pressed(None, Gdk.KEY_Home, 0, 0) is True
    assert seen[-1] == start
    assert timeline._on_key_pressed(None, Gdk.KEY_End, 0, 0) is True
    assert seen[-1] == end
    assert timeline._on_key_pressed(None, Gdk.KEY_Left, 0, 0) is True
    assert seen[-1] == pytest.approx(end - span * 0.01, abs=2)
    assert timeline._on_key_pressed(None, Gdk.KEY_Right, 0, 0) is True
    assert seen[-1] == end
    assert timeline._on_key_pressed(None, Gdk.KEY_space, 0, 0) is False


def test_timeline_set_selected_does_not_emit(history: FakeHistory) -> None:
    timeline = TimelineWidget()
    timeline.set_history(history)
    seen: list[int] = []
    timeline.connect("time-selected", lambda _w, value: seen.append(value))
    timeline.set_selected(history.time_range()[0])
    assert seen == []
    assert timeline.get_selected() == history.time_range()[0]
    timeline.set_selected(None)
    assert timeline.get_selected() is None


def test_timeline_activity_is_bucketed_and_cached(history: FakeHistory) -> None:
    timeline = TimelineWidget()
    timeline.set_history(history)
    lay = timeline._layout(600, 88)
    values = timeline._activity(lay)
    assert len(values) == int(lay["buckets"])
    assert history.activity_calls == 1
    timeline._activity(lay)
    assert history.activity_calls == 1  # cached
    timeline.set_visible_range(timeline.get_visible_range()[0], timeline.get_visible_range()[1] - NS)
    timeline._activity(timeline._layout(600, 88))
    assert history.activity_calls == 2


def test_timeline_survives_empty_history() -> None:
    timeline = TimelineWidget()
    timeline.set_history(FakeHistory([]))
    assert timeline.has_data() is False
    start, end = timeline.get_visible_range()
    assert end > start
    timeline.queue_draw()
    timeline.refresh()
    pump()


def test_timeline_survives_no_history_and_broken_history() -> None:
    class Broken:
        def sessions(self):
            raise RuntimeError("no log")

        def time_range(self):
            raise RuntimeError("no log")

        def activity(self, start, end, buckets):
            raise RuntimeError("no log")

    timeline = TimelineWidget()
    timeline.queue_draw()  # never had a history at all
    timeline.set_history(Broken())
    assert timeline.has_data() is False
    assert timeline._activity(timeline._layout(600, 88)) == []
    timeline.queue_draw()
    pump()


def test_timeline_single_instant_history() -> None:
    instant = 1_757_000_000 * NS
    timeline = TimelineWidget()
    timeline.set_history(FakeHistory([Session(1, instant, instant)]))
    start, end = timeline.get_visible_range()
    assert start < instant < end
    assert timeline.has_data() is True
    lay = timeline._layout(600, 88)
    assert lay["span_ns"] > 0
    timeline.queue_draw()
    pump()


def test_timeline_signal_argument_is_int64(history: FakeHistory) -> None:
    timeline = TimelineWidget()
    timeline.set_history(history)
    seen: list[int] = []
    timeline.connect("time-selected", lambda _w, value: seen.append(value))
    big = 1_900_000_000 * NS  # > 2**31 nanoseconds by far
    timeline.set_visible_range(big, big + 10 * NS)
    timeline._scrub_to_x(300.0, force=True)
    assert seen and seen[0] > 2**31
    assert isinstance(seen[0], int)


# ------------------------------------------------------------------ DiffView


def test_diff_summary_and_body() -> None:
    diff = DiffView()
    old = "a\nb\nc\n"
    new = "a\nB\nc\nd\n"
    diff.set_texts(old, new, old_label="state at 14:02", new_label="current")
    assert diff.get_summary() == "+2 / -1 lines"
    assert diff.counts() == (2, 1)
    body = diff.get_diff_text()
    assert "@@" in body
    assert "+B" in body
    assert "-b" in body
    assert "--- state at 14:02" in body
    assert "+++ current" in body
    assert "state at 14:02" in diff.labels_label.get_label()


def test_diff_tags_are_applied() -> None:
    diff = DiffView()
    diff.set_texts("a\nb\n", "a\nc\n")
    buffer = diff.view.buffer
    table = buffer.get_tag_table()
    add_tag = table.lookup("diff-add")
    del_tag = table.lookup("diff-del")
    hunk_tag = table.lookup("diff-hunk")
    assert add_tag and del_tag and hunk_tag

    lines = diff.get_diff_text().split("\n")
    seen = {"add": False, "del": False, "hunk": False}
    for index, line in enumerate(lines):
        it = buffer.get_iter_at_line(index)
        it = it[1] if isinstance(it, tuple) else it
        if line.startswith("+") and not line.startswith("+++"):
            seen["add"] = seen["add"] or it.has_tag(add_tag)
        elif line.startswith("-") and not line.startswith("---"):
            seen["del"] = seen["del"] or it.has_tag(del_tag)
        elif line.startswith("@@"):
            seen["hunk"] = seen["hunk"] or it.has_tag(hunk_tag)
    assert all(seen.values()), seen


def test_diff_identical_texts() -> None:
    diff = DiffView()
    diff.set_texts("same\ntext\n", "same\ntext\n")
    assert diff.get_summary() == "+0 / -0 lines"
    assert diff.get_diff_text() == "(no differences)"
    assert diff.counts() == (0, 0)


def test_diff_clear() -> None:
    diff = DiffView()
    diff.set_texts("a\n", "b\n")
    diff.clear()
    assert diff.get_summary() == "+0 / -0 lines"
    assert diff.get_diff_text() == ""
    assert diff.view.get_editable() is False


def test_diff_large_inputs_are_fast() -> None:
    diff = DiffView()
    old = "\n".join(f"line {i}" for i in range(5000))
    new = "\n".join(f"line {i}" if i % 500 else f"line {i} changed" for i in range(5000))
    started = time.monotonic()
    diff.set_texts(old, new)
    assert time.monotonic() - started < 5.0
    assert diff.counts()[0] >= 10


# ----------------------------------------------------------------- demo module


def test_demo_module_builds_fake_data() -> None:
    from scratchpad.ui import _demo_widgets as demo

    history = demo.make_history()
    assert len(history.sessions()) == 3
    start, end = history.time_range()
    assert end > start
    assert len(history.activity(start, end, 50)) == 50

    store, resolutions = demo.make_attachments()
    assert set(resolutions) == {"blank", "text", "image", "file", "missing"}
    pane = PreviewPane("Demo")
    pane.show_resolution(resolutions["image"], store)
    assert pane.current_page_name() == "image"
    pane.show_resolution(resolutions["file"], store)
    assert pane.current_page_name() == "file"
    pane.show_resolution(resolutions["missing"], store)
    assert pane.current_page_name() == "missing"


def test_timeline_auto_range_has_a_minimum_span() -> None:
    """A brand new log spans microseconds; the view must still be readable."""
    base = 1_757_000_000 * NS
    timeline = TimelineWidget()
    timeline.set_history(FakeHistory([Session(1, base, base + 500_000)]))
    start, end = timeline.get_visible_range()
    assert end - start >= NS
    assert start < base < end


def test_timeline_tick_granularity_adapts() -> None:
    timeline = TimelineWidget()
    assert timeline._tick_step(10 * NS, 600.0) <= 5 * NS
    assert timeline._tick_step(600 * NS, 600.0) >= 60 * NS
    assert timeline._tick_step(3 * 86400 * NS, 600.0) >= 3600 * NS
    assert timeline._tick_format(NS // 2).endswith("%f")
    assert timeline._tick_format(30 * NS) == "%H:%M:%S"
    assert timeline._tick_format(600 * NS) == "%H:%M"
    assert timeline._tick_format(2 * 86400 * NS) == "%Y-%m-%d"


def test_preview_image_metadata_falls_back_to_texture_size(store: Store) -> None:
    att = Att(object_id=2, token="N" * 22, kind=Kind.IMAGE, mime="image/png", size=100)
    pane = PreviewPane("Hover")
    pane.show_resolution(Res(VALID, att.token, 2, att), store)
    assert pane.current_page_name() == "image"
    assert pane._image_meta.get_label().startswith("PNG 4x3, created ")
