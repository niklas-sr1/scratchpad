"""Layout helpers for the main window.

GTK4 toplevels follow the *natural* size of their content: when a child starts
asking for more (a preview showing a 1400x900 image, a panel with a long row of
buttons becoming visible) the whole window grows to match. For a drop-down
scratchpad that is wrong: the window size belongs to the user and the
compositor, and panes must divide the space they are given.

`NaturalClamp` is a single-child container whose natural size equals its
minimum size. The child is still allocated everything the clamp receives, so a
`Gtk.Picture` with `can-shrink` scales into the pane and a `Gtk.FlowBox` wraps
its buttons to the available width; the child simply never asks for more.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402


class NaturalClamp(Gtk.Widget):
    """A single-child container that reports natural size == minimum size.

    Use it around content whose natural size changes at runtime but must not
    resize the window: preview pages, collapsible panels.
    """

    __gtype_name__ = "ScratchpadNaturalClamp"

    def __init__(self, child: Gtk.Widget | None = None) -> None:
        super().__init__()
        self._child: Gtk.Widget | None = None
        if child is not None:
            self.set_child(child)

    # ---------------------------------------------------------------- child

    def set_child(self, child: Gtk.Widget | None) -> None:
        """Replace the wrapped child (``None`` empties the clamp)."""
        if self._child is child:
            return
        if self._child is not None:
            self._child.unparent()
        self._child = child
        if child is not None:
            child.set_parent(self)
        self.queue_resize()

    def get_child(self) -> Gtk.Widget | None:
        """The wrapped child, if any."""
        return self._child

    # ---------------------------------------------------------- GTK vfuncs

    def do_get_request_mode(self) -> Gtk.SizeRequestMode:  # noqa: D401
        # Height follows width (a wrapping row of buttons gets taller when
        # narrow); width is never derived from height, see do_measure.
        return Gtk.SizeRequestMode.HEIGHT_FOR_WIDTH

    def do_measure(
        self, orientation: Gtk.Orientation, for_size: int
    ) -> tuple[int, int, int, int]:
        child = self._child
        if child is None or not child.get_visible():
            return (0, 0, -1, -1)
        if orientation == Gtk.Orientation.HORIZONTAL:
            # Never let a height constraint widen us.  GTK answers
            # "width for height H" for a height-for-width child by searching
            # for the smallest width whose minimum height fits H; for a
            # wrapping Gtk.FlowBox in a short panel that is the full single
            # row (over 1500 px here), which then becomes the window's minimum
            # width and resizes the whole window.  The child wraps instead.
            for_size = -1
        minimum, _natural, min_baseline, _nat_baseline = child.measure(orientation, for_size)
        # Natural equals minimum: the baseline that goes with the natural size
        # is therefore the minimum's baseline too.
        return (minimum, minimum, min_baseline, min_baseline)

    def do_size_allocate(self, width: int, height: int, baseline: int) -> None:
        child = self._child
        if child is not None and child.get_visible():
            child.allocate(width, height, baseline, None)

    def do_dispose(self) -> None:
        if self._child is not None:
            self._child.unparent()
            self._child = None
        Gtk.Widget.do_dispose(self)
