"""Monotonic and wall clock anchoring.

Ordering and deltas use a monotonic clock that **keeps counting while the
machine is suspended** (``CLOCK_BOOTTIME``); real timestamps come from
``time.time_ns`` written into anchor records.  A wall time is derived from an
anchor as::

    wall = anchor_wall_ns + (record_mono_ns - anchor_mono_ns)

``time.monotonic_ns`` (``CLOCK_MONOTONIC``) stops during a suspend, so with it a
laptop lid closed for three hours produced a log in which the resume looked like
the next keystroke: one continuous running span on the timeline, and every
record written between the resume and the next WALL_ANCHOR carrying a wall time
three hours in the past.  ``CLOCK_BOOTTIME`` is the same clock plus the
suspended time, which is exactly the "how much real time passed" that a timeline
needs.  Where it is unavailable (non-Linux) the code falls back to
``time.monotonic_ns`` and :data:`MONOTONIC_CLOCK_NAME` says so.

The log re-anchors with every heartbeat and whenever the two clocks drift apart
by more than :data:`DEFAULT_DRIFT_THRESHOLD_NS`, so the derived error stays below
roughly one second even across suspend/resume.  A gap in a running session -- a
suspend, or a machine so busy that no record was written for minutes -- is
additionally split out of the session by
:meth:`scratchpad.core.history.History.sessions`.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

NS_PER_SECOND = 1_000_000_000
#: Emit a WALL_ANCHOR once wall and monotonic clock disagree by more than this.
DEFAULT_DRIFT_THRESHOLD_NS = NS_PER_SECOND


def _pick_monotonic() -> tuple[str, Callable[[], int]]:
    """The best available suspend-aware monotonic source."""
    boottime = getattr(time, "CLOCK_BOOTTIME", None)
    if boottime is not None:
        try:
            time.clock_gettime_ns(boottime)
        except (OSError, ValueError):  # pragma: no cover - exotic kernels
            pass
        else:
            return "CLOCK_BOOTTIME", lambda: time.clock_gettime_ns(boottime)
    return "CLOCK_MONOTONIC", time.monotonic_ns


#: Name of the monotonic clock actually in use (diagnostics and tests).
MONOTONIC_CLOCK_NAME, _monotonic_ns = _pick_monotonic()


def monotonic_ns() -> int:
    """Monotonic nanoseconds including time spent suspended, where available."""
    return _monotonic_ns()


class Clock:
    """The system clocks.  Injectable so tests can fabricate timelines."""

    def wall_ns(self) -> int:
        """Nanoseconds since the Unix epoch (may jump)."""
        return time.time_ns()

    def mono_ns(self) -> int:
        """Nanoseconds from an arbitrary origin; never decreases, counts suspend."""
        return _monotonic_ns()


SYSTEM_CLOCK = Clock()


@dataclass(slots=True)
class ManualClock(Clock):
    """A clock under test control.  Both hands move independently."""

    wall: int = 0
    mono: int = 0

    def wall_ns(self) -> int:
        return self.wall

    def mono_ns(self) -> int:
        return self.mono

    def advance(self, ns: int) -> None:
        """Move both clocks forward by ``ns`` (the normal case)."""
        self.wall += ns
        self.mono += ns

    def advance_seconds(self, seconds: float) -> None:
        self.advance(int(seconds * NS_PER_SECOND))

    def step_wall(self, ns: int) -> None:
        """Move only the wall clock (an NTP step); monotonic stays put."""
        self.wall += ns


def derive_wall_ns(anchor_wall_ns: int, anchor_mono_ns: int, mono_ns: int) -> int:
    """Wall time of a record given the anchor in effect for it."""
    return anchor_wall_ns + (mono_ns - anchor_mono_ns)


@dataclass(slots=True)
class Anchor:
    """A (wall, monotonic) pair, as carried by SESSION_START and WALL_ANCHOR."""

    wall_ns: int = 0
    mono_ns: int = 0

    def derive(self, mono_ns: int) -> int:
        return self.wall_ns + (mono_ns - self.mono_ns)


@dataclass(slots=True)
class SessionClock:
    """Tracks the anchor of the running session and when to re-anchor.

    The store owns one of these; :meth:`drift_ns` is cheap enough to call on
    every keystroke.
    """

    clock: Clock = field(default=SYSTEM_CLOCK)
    drift_threshold_ns: int = DEFAULT_DRIFT_THRESHOLD_NS
    anchor: Anchor = field(default_factory=Anchor)

    def start(self) -> Anchor:
        """Take a fresh anchor from the system clocks and return it."""
        self.anchor = Anchor(self.clock.wall_ns(), self.clock.mono_ns())
        return self.anchor

    def mono_ns(self) -> int:
        return self.clock.mono_ns()

    def wall_ns(self) -> int:
        return self.clock.wall_ns()

    def derive(self, mono_ns: int) -> int:
        """Wall time for a record taken at ``mono_ns``."""
        return self.anchor.derive(mono_ns)

    def drift_ns(self, mono_ns: int | None = None, wall_ns: int | None = None) -> int:
        """Signed difference between the true wall clock and the derived one."""
        mono = self.clock.mono_ns() if mono_ns is None else mono_ns
        wall = self.clock.wall_ns() if wall_ns is None else wall_ns
        return wall - self.anchor.derive(mono)

    def needs_anchor(self, mono_ns: int | None = None, wall_ns: int | None = None) -> bool:
        """True when the drift exceeds the threshold and a WALL_ANCHOR is due."""
        return abs(self.drift_ns(mono_ns, wall_ns)) > self.drift_threshold_ns

    def reanchor(self, mono_ns: int | None = None, wall_ns: int | None = None) -> Anchor:
        """Adopt a new anchor (the caller writes a WALL_ANCHOR record)."""
        mono = self.clock.mono_ns() if mono_ns is None else mono_ns
        wall = self.clock.wall_ns() if wall_ns is None else wall_ns
        self.anchor = Anchor(wall, mono)
        return self.anchor
