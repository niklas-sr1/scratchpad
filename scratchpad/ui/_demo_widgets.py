"""Runnable demo of the four Agent D2 widgets, with fake data.

    python -m scratchpad.ui._demo_widgets

Opens one window containing a `TimelineWidget`, a `DiffView`, a
`LineNumberedTextView` and two `PreviewPane`s, fed from in-module stand-ins for
`History` and `AttachmentStore`.  Nothing here touches the real data directory.

Set `SCRATCHPAD_DEMO_AUTOQUIT=1` to make the window close itself after ~2
seconds, which is how the smoke test runs it.

The stand-in classes below double as documentation of exactly which parts of
the core contract the widgets rely on.
"""

from __future__ import annotations

import os
import random
import struct
import sys
import tempfile
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk  # noqa: E402

from scratchpad.ui.diffview import DiffView  # noqa: E402
from scratchpad.ui.preview import PreviewPane  # noqa: E402
from scratchpad.ui.textview_extras import LineNumberedTextView  # noqa: E402
from scratchpad.ui.timeline import TimelineWidget  # noqa: E402

NS = 1_000_000_000


# --------------------------------------------------------------------- stand-ins


@dataclass
class DemoSession:
    """Mirrors `scratchpad.core.history.Session`."""

    session_id: int
    start_wall_ns: int
    end_wall_ns: int
    clean_stop: bool = True
    first_seq: int = 0
    last_seq: int = 0


@dataclass
class DemoHistory:
    """Mirrors the parts of `History` that `TimelineWidget` uses."""

    _sessions: list[DemoSession]

    def sessions(self) -> list[DemoSession]:
        return list(self._sessions)

    def time_range(self) -> tuple[int, int]:
        return self._sessions[0].start_wall_ns, self._sessions[-1].end_wall_ns

    def activity(self, start_wall_ns: int, end_wall_ns: int, buckets: int) -> list[int]:
        rng = random.Random(1234)
        span = max(1, end_wall_ns - start_wall_ns)
        out: list[int] = []
        for i in range(buckets):
            middle = start_wall_ns + (i + 0.5) * span / buckets
            inside = any(s.start_wall_ns <= middle <= s.end_wall_ns for s in self._sessions)
            out.append(rng.randint(0, 40) if inside else 0)
        return out


@dataclass(frozen=True)
class DemoAttachment:
    """Mirrors `scratchpad.attachments.Attachment`."""

    object_id: int
    token: str
    kind: str
    mime: str
    sha256: str
    size: int
    created_wall_ns: int
    lines: int | None = None
    chars: int | None = None
    encoding: str | None = None
    width: int | None = None
    height: int | None = None
    filename: str | None = None


@dataclass(frozen=True)
class DemoState:
    """Mirrors `ResolutionState`: only `.name` is used by the widgets."""

    name: str


@dataclass(frozen=True)
class DemoResolution:
    """Mirrors `scratchpad.attachments.Resolution`."""

    state: DemoState
    token: str
    object_id: int | None = None
    attachment: DemoAttachment | None = None
    detail: str = ""


@dataclass
class DemoAttachmentStore:
    """Mirrors the two `AttachmentStore` methods the preview pane calls."""

    blobs: dict[int, bytes] = field(default_factory=dict)
    paths: dict[int, Path] = field(default_factory=dict)

    def read_blob(self, attachment: DemoAttachment) -> bytes:
        return self.blobs[attachment.object_id]

    def blob_path(self, attachment: DemoAttachment) -> Path:
        return self.paths[attachment.object_id]


# ------------------------------------------------------------------- fake data


def make_png(width: int, height: int) -> bytes:
    """A small PNG with a colour gradient, built with stdlib only."""
    rows = []
    for y in range(height):
        row = bytearray([0])
        for x in range(width):
            row += bytes(
                (
                    int(255 * x / max(1, width - 1)),
                    int(255 * y / max(1, height - 1)),
                    140,
                )
            )
        rows.append(bytes(row))
    raw = b"".join(rows)

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
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )


def make_history() -> DemoHistory:
    """Three sessions spread over the last three days."""
    now = int(time.time() * NS)
    day = 86400 * NS
    return DemoHistory(
        [
            DemoSession(1, now - 3 * day, now - 3 * day + 5 * 3600 * NS),
            DemoSession(2, now - 2 * day + 2 * 3600 * NS, now - 2 * day + 9 * 3600 * NS),
            DemoSession(3, now - 4 * 3600 * NS, now),
        ]
    )


def make_attachments() -> tuple[DemoAttachmentStore, dict[str, DemoResolution]]:
    """A text, an image, a file and a broken attachment."""
    now = int(time.time() * NS)
    store = DemoAttachmentStore()

    log_text = "\n".join(
        f"2026-09-14 18:{i // 60:02d}:{i % 60:02d}  can0  18DAF111  "
        f"[8] 03 41 0C {i % 256:02X} 00 00 00 00"
        for i in range(4000)
    )
    data = log_text.encode("utf-8")
    text_att = DemoAttachment(
        object_id=1,
        token="Ab3Fmx7QK9v2Rt8Nc4WpLd",
        kind="text",
        mime="text/plain",
        sha256="0" * 64,
        size=len(data),
        created_wall_ns=now - 3600 * NS,
        lines=log_text.count("\n") + 1,
        chars=len(log_text),
        encoding="utf-8",
    )
    store.blobs[1] = data

    png = make_png(320, 180)
    image_att = DemoAttachment(
        object_id=2,
        token="Zq7Lm2Xc8Pv1Nb5Td9Wk3F",
        kind="image",
        mime="image/png",
        sha256="1" * 64,
        size=len(png),
        created_wall_ns=now - 1800 * NS,
        width=320,
        height=180,
        filename="screenshot.png",
    )
    store.blobs[2] = png

    tmp = Path(tempfile.gettempdir()) / "scratchpad-demo-attachment.bin"
    tmp.write_bytes(b"demo file payload\n" * 1000)
    file_att = DemoAttachment(
        object_id=3,
        token="Qw9Er4Ty7Ui2Op5As8Df1G",
        kind="file",
        mime="application/pdf",
        sha256="2" * 64,
        size=tmp.stat().st_size,
        created_wall_ns=now - 7200 * NS,
        filename="invoice-2026-09.pdf",
    )
    store.blobs[3] = tmp.read_bytes()
    store.paths[3] = tmp

    resolutions = {
        "blank": DemoResolution(DemoState("ORDINARY"), "notatokenatallnotatoken"),
        "text": DemoResolution(DemoState("VALID"), text_att.token, 1, text_att),
        "image": DemoResolution(DemoState("VALID"), image_att.token, 2, image_att),
        "file": DemoResolution(DemoState("VALID"), file_att.token, 3, file_att),
        "missing": DemoResolution(
            DemoState("MISSING"),
            "Mm1Ss2Ii3Nn4Gg5Hh6Jj7K",
            4,
            None,
            "blob 3f9c... not found under attachments/blobs/3f",
        ),
    }
    return store, resolutions


OLD_TEXT = """- investigate CAN timeout
  Ab3Fmx7QK9v2Rt8Nc4WpLd
- call supplier about the wiring harness
- order:
  - 2x connector
  - 1x relay
"""

NEW_TEXT = """- investigate CAN timeout (root cause: terminator resistor)
  Ab3Fmx7QK9v2Rt8Nc4WpLd
- order:
  - 2x connector
  - 1x relay
  - 3x fuse
- write up the findings
"""


# ----------------------------------------------------------------------- window


class DemoWindow(Adw.ApplicationWindow):
    """One window showing every widget this agent owns."""

    def __init__(self, app: Adw.Application) -> None:
        super().__init__(application=app, title="Scratchpad widget demo")
        self.set_default_size(1180, 820)

        store, resolutions = make_attachments()
        self._store = store
        self._resolutions = resolutions

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title="Scratchpad widgets", subtitle="fake data"))
        toolbar.add_top_bar(header)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        root.set_margin_top(6)
        root.set_margin_bottom(6)

        self.timeline = TimelineWidget()
        self.timeline.set_history(make_history())
        self.timeline.connect("time-selected", self._on_time_selected)
        timeline_frame = Gtk.Frame()
        timeline_frame.set_margin_start(6)
        timeline_frame.set_margin_end(6)
        timeline_frame.set_child(self.timeline)
        root.append(timeline_frame)

        self.status = Gtk.Label(label="Click or drag the timeline; scroll to zoom.", xalign=0.0)
        self.status.add_css_class("dim-label")
        self.status.set_margin_start(10)
        root.append(self.status)

        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        paned.set_position(660)
        paned.set_vexpand(True)

        notebook = Gtk.Notebook()
        self.diff = DiffView()
        self.diff.set_texts(
            OLD_TEXT, NEW_TEXT, old_label="state at 14:02:11", new_label="current"
        )
        notebook.append_page(self.diff, Gtk.Label(label="DiffView"))

        self.plain = LineNumberedTextView(editable=True)
        self.plain.set_text(
            "LineNumberedTextView (editable here)\n"
            + "\n".join(f"line {i}: the quick brown fox jumps over the lazy dog" for i in range(300))
        )
        notebook.append_page(self.plain, Gtk.Label(label="LineNumberedTextView"))
        paned.set_start_child(notebook)

        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        sidebar.set_size_request(340, -1)

        self.cursor_pane = PreviewPane("Cursor")
        self.cursor_pane.show_resolution(resolutions["text"], store)
        self.hover_pane = PreviewPane("Hover")
        self.hover_pane.show_resolution(resolutions["image"], store)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        buttons.set_margin_start(8)
        buttons.set_margin_end(8)
        buttons.set_homogeneous(True)
        for name in ("blank", "image", "text", "file", "missing"):
            button = Gtk.Button(label=name)
            button.connect("clicked", self._on_preview_button, name)
            buttons.append(button)

        sidebar.append(self.cursor_pane)
        sidebar.append(Gtk.Separator())
        sidebar.append(buttons)
        sidebar.append(self.hover_pane)
        paned.set_end_child(sidebar)

        root.append(paned)
        toolbar.set_content(root)
        self.set_content(toolbar)

    def _on_time_selected(self, _timeline: TimelineWidget, wall_ns: int) -> None:
        moment = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(wall_ns / NS))
        start, end = self.timeline.get_visible_range()
        self.status.set_label(
            f"time-selected: {moment}  (visible span "
            f"{(end - start) / NS:.1f} s, {wall_ns} ns)"
        )

    def _on_preview_button(self, _button: Gtk.Button, name: str) -> None:
        self.hover_pane.show_resolution(self._resolutions[name], self._store)


class DemoApplication(Adw.Application):
    """Tiny Adw.Application wrapper so the demo behaves like the real app."""

    def __init__(self) -> None:
        super().__init__(application_id="dev.scratchpad.WidgetDemo")

    def do_activate(self) -> None:
        window = DemoWindow(self)
        window.present()
        if os.environ.get("SCRATCHPAD_DEMO_AUTOQUIT") == "1":
            GLib.timeout_add(2000, self._auto_quit)

    def _auto_quit(self) -> bool:
        print("demo: auto-quit")
        self.quit()
        return GLib.SOURCE_REMOVE


def main(argv: list[str] | None = None) -> int:
    """Run the demo application."""
    app = DemoApplication()
    return app.run(argv if argv is not None else sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
