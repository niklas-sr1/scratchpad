"""The history timeline.

`TimelineWidget` is a cairo-drawn `Gtk.DrawingArea` that shows *time*, not
commits (see `Design Specification.md` section 6):

    ticks     local-time labels with adaptive granularity (ms .. days)
    track     solid bars where a session was running, hatched grey in between
    heat      edit-activity density from `History.activity()`
    marker    the currently selected instant, with its local timestamp

Interaction:

    primary button click / drag   scrub, emitting `time-selected` (throttled to
                                  ~30 Hz while dragging, always on release)
    scroll wheel                  zoom around the pointer
    middle button drag            pan
    Left / Right                  step by 1% of the visible span
    Home / End                    jump to the ends of the visible range

The widget never touches the store; the window connects `time-selected` to
`History.reconstruct_at()`.  `History` is duck-typed (`sessions()`,
`time_range()`, `activity(start, end, buckets)`) so it can be tested with
stand-ins, and every call into it is guarded: a broken or empty history draws
"No history yet" instead of raising.
"""

from __future__ import annotations

import logging
import math
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")

from gi.repository import Gdk, GObject, Gtk, Pango, PangoCairo  # noqa: E402

if TYPE_CHECKING:  # pragma: no cover - typing only
    from scratchpad.core.history import History

__all__ = ["TimelineWidget"]

log = logging.getLogger(__name__)

NS = 1_000_000_000
MIN_SPAN_NS = 1_000_000  # 1 ms: the deepest useful zoom
MIN_AUTO_SPAN_NS = 1_000_000_000  # a fresh log spans microseconds; show a second
DEFAULT_PAD_FRACTION = 0.02
EMIT_INTERVAL_S = 1.0 / 30.0
PIXELS_PER_BUCKET = 3
PAD_X = 10.0

#: Tick ladder in nanoseconds, from 1 ms to a year.
_TICK_STEPS: tuple[int, ...] = (
    1_000_000,
    5_000_000,
    10_000_000,
    50_000_000,
    100_000_000,
    500_000_000,
    1 * NS,
    2 * NS,
    5 * NS,
    10 * NS,
    15 * NS,
    30 * NS,
    60 * NS,
    2 * 60 * NS,
    5 * 60 * NS,
    10 * 60 * NS,
    15 * 60 * NS,
    30 * 60 * NS,
    3600 * NS,
    2 * 3600 * NS,
    3 * 3600 * NS,
    6 * 3600 * NS,
    12 * 3600 * NS,
    86400 * NS,
    2 * 86400 * NS,
    7 * 86400 * NS,
    14 * 86400 * NS,
    30 * 86400 * NS,
    90 * 86400 * NS,
    365 * 86400 * NS,
)


def _local_datetime(wall_ns: int) -> datetime:
    """Local `datetime` for a wall-clock nanosecond timestamp."""
    return datetime.fromtimestamp(wall_ns / NS)


def _utc_offset_ns(wall_ns: int) -> int:
    """Local UTC offset (including DST) at `wall_ns`, in nanoseconds."""
    try:
        offset = datetime.fromtimestamp(wall_ns / NS).astimezone().utcoffset()
    except (OverflowError, OSError, ValueError):
        return 0
    return int(offset.total_seconds() * NS) if offset else 0


def _clamp(value: float, low: float, high: float) -> float:
    return low if value < low else (high if value > high else value)


class TimelineWidget(Gtk.DrawingArea):
    """A zoomable, scrubbable wall-clock timeline of the scratchpad history.

    Public API (consumed by `scratchpad.ui.window`):
        set_history(history)              attach a `History` and reset the view
        refresh()                         re-read sessions/time range/activity
        set_selected(wall_ns | None)      move the marker without emitting
        get_selected() -> int | None
        get_visible_range() -> (start_ns, end_ns)
        set_visible_range(start_ns, end_ns)
        reset_view()                      back to the full range with padding
        signal "time-selected" (int64 wall_ns)
    """

    __gsignals__ = {
        "time-selected": (GObject.SignalFlags.RUN_FIRST, None, (GObject.TYPE_INT64,)),
    }

    def __init__(self) -> None:
        super().__init__()
        self.add_css_class("timeline")
        self.set_content_height(88)
        self.set_hexpand(True)
        self.set_focusable(True)
        self.set_can_focus(True)

        self._history: Any | None = None
        self._sessions: list[Any] = []
        self._data_start: int | None = None
        self._data_end: int | None = None
        now = int(time.time() * NS)
        self._visible_start: int = now - 60 * NS
        self._visible_end: int = now + 60 * NS
        self._user_ranged = False
        self._selected: int | None = None
        self._activity_cache: tuple[tuple[int, int, int], list[int]] | None = None
        self._pointer_x: float | None = None
        self._last_emit = 0.0
        self._pan_origin: tuple[int, int] | None = None
        self._drag_x = 0.0

        self.set_draw_func(self._on_draw)
        self._install_controllers()

    # ------------------------------------------------------------ controllers

    def _install_controllers(self) -> None:
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        motion.connect("leave", self._on_leave)
        self.add_controller(motion)

        scroll = Gtk.EventControllerScroll.new(
            Gtk.EventControllerScrollFlags.VERTICAL | Gtk.EventControllerScrollFlags.DISCRETE
        )
        scroll.connect("scroll", self._on_scroll)
        self.add_controller(scroll)

        scrub = Gtk.GestureDrag()
        scrub.set_button(Gdk.BUTTON_PRIMARY)
        scrub.connect("drag-begin", self._on_scrub_begin)
        scrub.connect("drag-update", self._on_scrub_update)
        scrub.connect("drag-end", self._on_scrub_end)
        self.add_controller(scrub)

        pan = Gtk.GestureDrag()
        pan.set_button(Gdk.BUTTON_MIDDLE)
        pan.connect("drag-begin", self._on_pan_begin)
        pan.connect("drag-update", self._on_pan_update)
        pan.connect("drag-end", lambda *_a: setattr(self, "_pan_origin", None))
        self.add_controller(pan)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key_pressed)
        self.add_controller(keys)

    # ------------------------------------------------------------- public API

    def set_history(self, history: "History | None") -> None:
        """Attach a history object and reset the visible range to its full span."""
        self._history = history
        self._user_ranged = False
        self._selected = None
        self.refresh()

    def refresh(self) -> None:
        """Re-read sessions, time range and activity; redraw.

        Called after new events were appended.  A range the user zoomed or
        panned to is kept; an automatic range follows the data.
        """
        sessions: list[Any] = []
        data_start: int | None = None
        data_end: int | None = None
        if self._history is not None:
            try:
                sessions = list(self._history.sessions())
            except Exception as exc:  # a broken history must not break the UI
                log.warning("timeline: sessions() failed: %s", exc)
            try:
                rng = self._history.time_range()
                if rng is not None:
                    data_start, data_end = int(rng[0]), int(rng[1])
            except Exception as exc:
                log.warning("timeline: time_range() failed: %s", exc)
        if (data_start is None or data_end is None) and sessions:
            starts = [int(s.start_wall_ns) for s in sessions]
            ends = [int(s.end_wall_ns) for s in sessions]
            data_start, data_end = min(starts), max(ends)

        self._sessions = sessions
        self._data_start = data_start
        self._data_end = data_end
        self._activity_cache = None
        if not self._user_ranged:
            self._reset_visible_range()
        self.queue_draw()

    def reset_view(self) -> None:
        """Zoom back out to the full history range (with 2% padding)."""
        self._user_ranged = False
        self._reset_visible_range()
        self.queue_draw()

    def set_selected(self, wall_ns: int | None) -> None:
        """Move the selection marker without emitting `time-selected`."""
        self._selected = None if wall_ns is None else int(wall_ns)
        self.queue_draw()

    def get_selected(self) -> int | None:
        """The selected instant, or None."""
        return self._selected

    def get_visible_range(self) -> tuple[int, int]:
        """The currently visible (start_ns, end_ns)."""
        return self._visible_start, self._visible_end

    def set_visible_range(self, start_ns: int, end_ns: int) -> None:
        """Set the visible range; the widget stops following new data."""
        self._set_range(int(start_ns), int(end_ns), user=True)

    def has_data(self) -> bool:
        """Whether anything can be drawn (any session or a known instant)."""
        if self._sessions:
            return True
        if self._data_start is None or self._data_end is None:
            return False
        if self._data_end > self._data_start:
            return True
        return self._data_start > 0  # a single known instant

    # -------------------------------------------------------------- range math

    def _max_span(self) -> int:
        span = 0
        if self._data_start is not None and self._data_end is not None:
            span = max(0, self._data_end - self._data_start)
        return max(span * 8, 3600 * NS)

    def _set_range(self, start_ns: int, end_ns: int, *, user: bool) -> None:
        if end_ns <= start_ns:
            end_ns = start_ns + MIN_SPAN_NS
        span = end_ns - start_ns
        max_span = self._max_span()
        if span < MIN_SPAN_NS:
            centre = (start_ns + end_ns) // 2
            start_ns, end_ns = centre - MIN_SPAN_NS // 2, centre + MIN_SPAN_NS // 2
        elif span > max_span:
            centre = (start_ns + end_ns) // 2
            start_ns, end_ns = centre - max_span // 2, centre + max_span // 2
        if (start_ns, end_ns) != (self._visible_start, self._visible_end):
            self._activity_cache = None
        self._visible_start, self._visible_end = int(start_ns), int(end_ns)
        if user:
            self._user_ranged = True
        self.queue_draw()

    def _reset_visible_range(self) -> None:
        start, end = self._data_start, self._data_end
        if start is None or end is None:
            now = int(time.time() * NS)
            self._visible_start, self._visible_end = now - 60 * NS, now + 60 * NS
            self._activity_cache = None
            return
        if end <= start:  # a single instant
            self._visible_start, self._visible_end = start - NS, end + NS
        else:
            pad = int((end - start) * DEFAULT_PAD_FRACTION) or 1
            self._visible_start, self._visible_end = start - pad, end + pad
        if self._visible_end - self._visible_start < MIN_AUTO_SPAN_NS:
            centre = (self._visible_start + self._visible_end) // 2
            half = MIN_AUTO_SPAN_NS // 2
            self._visible_start, self._visible_end = centre - half, centre + half
        self._activity_cache = None

    # --------------------------------------------------------------- geometry

    def _width(self) -> int:
        """Current width in pixels, with a sane fallback before allocation."""
        for value in (self.get_width(), self.get_content_width()):
            if value and value > 0:
                return int(value)
        return 600

    def _height(self) -> int:
        """Current height in pixels, with a sane fallback before allocation."""
        for value in (self.get_height(), self.get_content_height()):
            if value and value > 0:
                return int(value)
        return 88

    def _layout(self, width: int | None = None, height: int | None = None) -> dict[str, float]:
        """Pure geometry + time mapping for a given size.

        Returns a dict with the plot rectangle, the y bands (ticks, track,
        heat), the visible time range and the number of activity buckets.
        This is the function the tests use instead of rendering.
        """
        w = float(width if width is not None else self._width())
        h = float(height if height is not None else self._height())
        plot_x = PAD_X
        plot_w = max(1.0, w - 2 * PAD_X)

        inner = max(h - 8.0, 14.0)
        ticks_h = max(10.0, min(15.0, inner * 0.22))
        heat_h = max(6.0, min(14.0, inner * 0.20))
        track_h = max(8.0, inner - ticks_h - heat_h - 8.0)
        ticks_y = 4.0
        track_y = ticks_y + ticks_h + 4.0
        heat_y = track_y + track_h + 4.0

        start_ns = float(self._visible_start)
        end_ns = float(self._visible_end)
        span_ns = max(1.0, end_ns - start_ns)
        return {
            "width": w,
            "height": h,
            "plot_x": plot_x,
            "plot_w": plot_w,
            "ticks_y": ticks_y,
            "ticks_h": ticks_h,
            "track_y": track_y,
            "track_h": track_h,
            "heat_y": heat_y,
            "heat_h": heat_h,
            "start_ns": start_ns,
            "end_ns": end_ns,
            "span_ns": span_ns,
            "buckets": float(max(1, int(plot_w // PIXELS_PER_BUCKET))),
            "px_per_ns": plot_w / span_ns,
        }

    def _time_to_x(self, wall_ns: int | float, width: int | None = None) -> float:
        """Map a wall-clock timestamp to an x coordinate."""
        lay = self._layout(width)
        return lay["plot_x"] + (float(wall_ns) - lay["start_ns"]) * lay["px_per_ns"]

    def _x_to_time(self, x: float, width: int | None = None) -> int:
        """Map an x coordinate back to a wall-clock timestamp (pure inverse)."""
        lay = self._layout(width)
        return int(lay["start_ns"] + (float(x) - lay["plot_x"]) / lay["px_per_ns"])

    def _clamp_visible(self, wall_ns: int) -> int:
        return int(_clamp(float(wall_ns), float(self._visible_start), float(self._visible_end)))

    def _zoom_at(self, x: float, factor: float) -> None:
        """Zoom by `factor` (<1 zooms in) keeping the instant under `x` fixed."""
        anchor = self._x_to_time(x)
        start, end = self._visible_start, self._visible_end
        span = max(1, end - start)
        new_span = int(_clamp(span * factor, MIN_SPAN_NS, float(self._max_span())))
        frac = _clamp((anchor - start) / span, 0.0, 1.0)
        new_start = int(anchor - frac * new_span)
        self._set_range(new_start, new_start + new_span, user=True)

    # -------------------------------------------------------------- activity

    def _activity(self, lay: dict[str, float]) -> list[int]:
        start = int(lay["start_ns"])
        end = int(lay["end_ns"])
        buckets = int(lay["buckets"])
        key = (start, end, buckets)
        if self._activity_cache is not None and self._activity_cache[0] == key:
            return self._activity_cache[1]
        values: list[int] = []
        if self._history is not None:
            try:
                values = [int(v) for v in self._history.activity(start, end, buckets)]
            except Exception as exc:
                log.warning("timeline: activity() failed: %s", exc)
                values = []
        self._activity_cache = (key, values)
        return values

    # -------------------------------------------------------------- selection

    def _scrub_to_x(self, x: float, *, force: bool = False) -> None:
        wall_ns = self._clamp_visible(self._x_to_time(x))
        self._select_and_emit(wall_ns, force=force)

    def _select_and_emit(self, wall_ns: int, *, force: bool) -> None:
        self._selected = int(wall_ns)
        self.queue_draw()
        now = time.monotonic()
        if force or (now - self._last_emit) >= EMIT_INTERVAL_S:
            self._last_emit = now
            self.emit("time-selected", int(wall_ns))

    # -------------------------------------------------------------- callbacks

    def _on_motion(self, _c: Gtk.EventControllerMotion, x: float, _y: float) -> None:
        self._pointer_x = x

    def _on_leave(self, _c: Gtk.EventControllerMotion) -> None:
        self._pointer_x = None

    def _on_scroll(self, _c: Gtk.EventControllerScroll, _dx: float, dy: float) -> bool:
        if dy == 0.0:
            return False
        lay = self._layout()
        x = self._pointer_x if self._pointer_x is not None else lay["plot_x"] + lay["plot_w"] / 2
        self._zoom_at(x, 0.85 ** (-dy))
        return True

    def _on_scrub_begin(self, gesture: Gtk.GestureDrag, x: float, _y: float) -> None:
        self.grab_focus()
        self._drag_x = x
        self._scrub_to_x(x, force=True)

    def _on_scrub_update(self, gesture: Gtk.GestureDrag, offset_x: float, _oy: float) -> None:
        ok, start_x, _start_y = gesture.get_start_point()
        base = start_x if ok else self._drag_x
        self._scrub_to_x(base + offset_x)

    def _on_scrub_end(self, gesture: Gtk.GestureDrag, offset_x: float, _oy: float) -> None:
        ok, start_x, _start_y = gesture.get_start_point()
        base = start_x if ok else self._drag_x
        self._scrub_to_x(base + offset_x, force=True)

    def _on_pan_begin(self, _gesture: Gtk.GestureDrag, _x: float, _y: float) -> None:
        self._pan_origin = (self._visible_start, self._visible_end)

    def _on_pan_update(self, _gesture: Gtk.GestureDrag, offset_x: float, _oy: float) -> None:
        if self._pan_origin is None:
            return
        start, end = self._pan_origin
        lay = self._layout()
        delta = int(-offset_x / lay["px_per_ns"])
        self._set_range(start + delta, end + delta, user=True)

    def _on_key_pressed(
        self, _c: Gtk.EventControllerKey, keyval: int, _keycode: int, _state: Gdk.ModifierType
    ) -> bool:
        span = max(1, self._visible_end - self._visible_start)
        step = max(1, int(span * 0.01))
        current = self._selected
        if current is None:
            current = self._visible_start + span // 2
        if keyval in (Gdk.KEY_Left, Gdk.KEY_KP_Left):
            self._select_and_emit(self._clamp_visible(current - step), force=True)
            return True
        if keyval in (Gdk.KEY_Right, Gdk.KEY_KP_Right):
            self._select_and_emit(self._clamp_visible(current + step), force=True)
            return True
        if keyval in (Gdk.KEY_Home, Gdk.KEY_KP_Home):
            self._select_and_emit(self._visible_start, force=True)
            return True
        if keyval in (Gdk.KEY_End, Gdk.KEY_KP_End):
            self._select_and_emit(self._visible_end, force=True)
            return True
        return False

    # --------------------------------------------------------------- painting

    def _is_dark(self) -> bool:
        """Whether libadwaita is currently in a dark color scheme."""
        try:
            import gi as _gi

            _gi.require_version("Adw", "1")
            from gi.repository import Adw

            return bool(Adw.StyleManager.get_default().get_dark())
        except Exception:
            return False

    def _foreground(self) -> tuple[float, float, float, float]:
        """The text color to draw with, sanity-checked against the theme.

        Before the widget is inside a window, CSS is not resolved and
        `get_color()` answers plain white, which would be invisible on a light
        background.  In that case (and whenever the resolved color contradicts
        the color scheme) fall back to a readable default.
        """
        dark = self._is_dark()
        try:
            color: Gdk.RGBA = self.get_color()
            luminance = 0.299 * color.red + 0.587 * color.green + 0.114 * color.blue
            plausible = (luminance > 0.4) if dark else (luminance < 0.6)
            if color.alpha > 0.05 and plausible:
                return (color.red, color.green, color.blue, color.alpha)
        except Exception:  # pragma: no cover - defensive
            pass
        return (0.95, 0.95, 0.95, 1.0) if dark else (0.11, 0.11, 0.11, 0.9)

    def _palette(self) -> dict[str, tuple[float, float, float, float]]:
        """Theme-aware colors derived from the widget's own text color."""
        red, green, blue, alpha = self._foreground()
        base = (red, green, blue)
        accent = self._accent_rgb()

        def shade(a: float) -> tuple[float, float, float, float]:
            return (base[0], base[1], base[2], min(1.0, alpha * a))

        return {
            "text": shade(0.85),
            "text_dim": shade(0.55),
            "track_bg": shade(0.06),
            "gap_fill": shade(0.05),
            "gap_line": shade(0.14),
            "session": (accent[0], accent[1], accent[2], 0.85),
            "session_edge": (accent[0], accent[1], accent[2], 1.0),
            "heat_bg": shade(0.05),
            "heat": shade(1.0),
            "marker": (0.90, 0.35, 0.22, 0.95),
            "marker_text": (1.0, 1.0, 1.0, 1.0),
        }

    def _accent_rgb(self) -> tuple[float, float, float]:
        try:
            import gi as _gi

            _gi.require_version("Adw", "1")
            from gi.repository import Adw

            rgba = Adw.StyleManager.get_default().get_accent_color_rgba()
            return (rgba.red, rgba.green, rgba.blue)
        except Exception:
            return (0.21, 0.52, 0.89)

    def _on_draw(self, _area: Gtk.DrawingArea, cr: Any, width: int, height: int) -> None:
        if width <= 0 or height <= 0:
            return
        pal = self._palette()
        lay = self._layout(width, height)
        if not self.has_data():
            self._draw_centered_text(cr, lay, pal, "No history yet")
            return
        self._draw_track(cr, lay, pal)
        self._draw_heat(cr, lay, pal)
        self._draw_ticks(cr, lay, pal)
        self._draw_marker(cr, lay, pal)

    def _layout_for_text(self, cr: Any, text: str, *, scale: float = 1.0) -> Pango.Layout:
        layout = PangoCairo.create_layout(cr)
        desc = self.get_pango_context().get_font_description()
        desc = desc.copy() if desc is not None else Pango.FontDescription.from_string("Sans 10")
        size = desc.get_size() or (10 * Pango.SCALE)
        desc.set_size(int(size * scale))
        layout.set_font_description(desc)
        layout.set_text(text, -1)
        return layout

    def _draw_centered_text(
        self, cr: Any, lay: dict[str, float], pal: dict, text: str
    ) -> None:
        layout = self._layout_for_text(cr, text)
        tw, th = layout.get_pixel_size()
        cr.set_source_rgba(*pal["text_dim"])
        cr.move_to((lay["width"] - tw) / 2.0, (lay["height"] - th) / 2.0)
        PangoCairo.show_layout(cr, layout)

    def _draw_track(self, cr: Any, lay: dict[str, float], pal: dict) -> None:
        x, w = lay["plot_x"], lay["plot_w"]
        y, h = lay["track_y"], lay["track_h"]

        # The whole track is "not running" until a session paints over it.
        cr.save()
        cr.rectangle(x, y, w, h)
        cr.clip()
        cr.set_source_rgba(*pal["gap_fill"])
        cr.paint()
        cr.set_source_rgba(*pal["gap_line"])
        cr.set_line_width(1.0)
        step = 7.0
        hx = x - h
        while hx < x + w + h:
            cr.move_to(hx, y + h)
            cr.line_to(hx + h, y)
            hx += step
        cr.stroke()
        cr.restore()

        for session in self._sessions:
            try:
                s = int(session.start_wall_ns)
                e = int(session.end_wall_ns)
            except Exception:  # pragma: no cover - defensive
                continue
            if e < lay["start_ns"] or s > lay["end_ns"]:
                continue
            x0 = _clamp(self._time_to_x(s, int(lay["width"])), x, x + w)
            x1 = _clamp(self._time_to_x(max(e, s), int(lay["width"])), x, x + w)
            bar_w = max(2.0, x1 - x0)
            if x0 + bar_w > x + w:
                x0 = max(x, x + w - bar_w)
            cr.set_source_rgba(*pal["session"])
            cr.rectangle(x0, y, bar_w, h)
            cr.fill()

        cr.set_source_rgba(*pal["gap_line"])
        cr.set_line_width(1.0)
        cr.rectangle(x + 0.5, y + 0.5, w - 1.0, h - 1.0)
        cr.stroke()

    def _draw_heat(self, cr: Any, lay: dict[str, float], pal: dict) -> None:
        x, w = lay["plot_x"], lay["plot_w"]
        y, h = lay["heat_y"], lay["heat_h"]
        cr.set_source_rgba(*pal["heat_bg"])
        cr.rectangle(x, y, w, h)
        cr.fill()

        values = self._activity(lay)
        if not values:
            return
        peak = max(values)
        if peak <= 0:
            return
        cell = w / float(len(values))
        r, g, b, _a = pal["heat"]
        for i, value in enumerate(values):
            if value <= 0:
                continue
            intensity = (value / peak) ** 0.6
            cr.set_source_rgba(r, g, b, 0.18 + 0.72 * intensity)
            cr.rectangle(x + i * cell, y, max(1.0, cell), h)
            cr.fill()

    def _tick_step(self, span_ns: float, plot_w: float) -> int:
        max_ticks = max(2, int(plot_w // 90))
        for step in _TICK_STEPS:
            if span_ns / step <= max_ticks:
                return step
        return _TICK_STEPS[-1]

    def _tick_format(self, step: int) -> str:
        if step < NS:
            return "%H:%M:%S.%f"
        if step < 60 * NS:
            return "%H:%M:%S"
        if step < 86400 * NS:
            return "%H:%M"
        return "%Y-%m-%d"

    def _draw_ticks(self, cr: Any, lay: dict[str, float], pal: dict) -> None:
        span = lay["span_ns"]
        step = self._tick_step(span, lay["plot_w"])
        fmt = self._tick_format(step)
        offset = _utc_offset_ns(int(lay["start_ns"] + span / 2))
        first = int(math.ceil((lay["start_ns"] + offset) / step) * step - offset)

        cr.set_line_width(1.0)
        previous_day: str | None = None
        label_right = -1e9  # right edge of the last drawn label, to avoid overlap
        t = first
        guard = 0
        while t <= lay["end_ns"] and guard < 512:
            guard += 1
            x = self._time_to_x(t, int(lay["width"]))
            dt = _local_datetime(t)
            label = dt.strftime(fmt)
            if fmt.endswith(".%f"):
                label = label[:-3]  # milliseconds are enough
            day = dt.strftime("%Y-%m-%d")
            if step < 86400 * NS and day != previous_day:
                label = f"{dt.strftime('%b %d')} {label}"
            previous_day = day

            cr.set_source_rgba(*pal["gap_line"])
            cr.move_to(x + 0.5, lay["ticks_y"] + lay["ticks_h"] - 2)
            cr.line_to(x + 0.5, lay["track_y"])
            cr.stroke()

            layout = self._layout_for_text(cr, label, scale=0.8)
            tw, th = layout.get_pixel_size()
            tx = _clamp(x - tw / 2.0, 1.0, max(1.0, lay["width"] - tw - 1.0))
            if tx >= label_right + 6.0:
                cr.set_source_rgba(*pal["text_dim"])
                cr.move_to(tx, lay["ticks_y"] + max(0.0, (lay["ticks_h"] - th) / 2.0))
                PangoCairo.show_layout(cr, layout)
                label_right = tx + tw
            t += step

    def _draw_marker(self, cr: Any, lay: dict[str, float], pal: dict) -> None:
        if self._selected is None:
            return
        if not (lay["start_ns"] <= self._selected <= lay["end_ns"]):
            return
        x = self._time_to_x(self._selected, int(lay["width"]))
        top = lay["ticks_y"] + lay["ticks_h"]
        bottom = lay["heat_y"] + lay["heat_h"]
        cr.set_source_rgba(*pal["marker"])
        cr.set_line_width(2.0)
        cr.move_to(x, top)
        cr.line_to(x, bottom)
        cr.stroke()
        cr.arc(x, top, 3.0, 0, 2 * math.pi)
        cr.fill()

        label = _local_datetime(self._selected).strftime("%Y-%m-%d %H:%M:%S")
        layout = self._layout_for_text(cr, label, scale=0.8)
        tw, th = layout.get_pixel_size()
        bx = x + 6.0
        if bx + tw + 8.0 > lay["width"]:
            bx = x - tw - 14.0
        bx = max(1.0, bx)
        by = max(0.0, bottom - th - 2.0)
        cr.set_source_rgba(*pal["marker"])
        cr.rectangle(bx - 3.0, by - 1.0, tw + 6.0, th + 2.0)
        cr.fill()
        cr.set_source_rgba(*pal["marker_text"])
        cr.move_to(bx, by)
        PangoCairo.show_layout(cr, layout)
