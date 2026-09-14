"""The clock sources and the anchor arithmetic.

The one thing worth testing about the system clock is *which* clock it is:
``CLOCK_MONOTONIC`` stops while the machine is suspended, and a scratchpad that
uses it turns a three hour suspend into a three hour lie on the timeline.
"""
from __future__ import annotations

import time

from scratchpad.core import clock as clock_mod
from scratchpad.core.clock import (
    Anchor,
    Clock,
    ManualClock,
    SessionClock,
    derive_wall_ns,
    monotonic_ns,
)

SECOND = 1_000_000_000


def test_the_monotonic_source_counts_time_spent_suspended() -> None:
    assert clock_mod.MONOTONIC_CLOCK_NAME == "CLOCK_BOOTTIME", "Linux has CLOCK_BOOTTIME"
    before = time.clock_gettime_ns(time.CLOCK_BOOTTIME)
    reading = monotonic_ns()
    after = time.clock_gettime_ns(time.CLOCK_BOOTTIME)
    assert before <= reading <= after
    # CLOCK_BOOTTIME is CLOCK_MONOTONIC plus the time spent suspended, so it is
    # never behind it (on a machine that never slept the two run together, and
    # the only difference is how long these two calls take).
    assert monotonic_ns() - time.monotonic_ns() > -1_000_000


def test_the_system_clock_uses_that_source() -> None:
    system = Clock()
    before = monotonic_ns()
    reading = system.mono_ns()
    after = monotonic_ns()
    assert before <= reading <= after
    assert abs(system.wall_ns() - time.time_ns()) < SECOND


def test_monotonic_never_goes_backwards() -> None:
    readings = [monotonic_ns() for _ in range(1000)]
    assert readings == sorted(readings)


def test_manual_clock_moves_both_hands_or_only_one() -> None:
    clock = ManualClock(wall=1000, mono=10)
    clock.advance(5)
    assert (clock.wall_ns(), clock.mono_ns()) == (1005, 15)
    clock.step_wall(-100)
    assert (clock.wall_ns(), clock.mono_ns()) == (905, 15)
    clock.advance_seconds(2)
    assert clock.mono_ns() == 15 + 2 * SECOND


def test_anchors_derive_wall_times() -> None:
    anchor = Anchor(wall_ns=1_000 * SECOND, mono_ns=5 * SECOND)
    assert anchor.derive(7 * SECOND) == 1_002 * SECOND
    assert derive_wall_ns(anchor.wall_ns, anchor.mono_ns, 7 * SECOND) == 1_002 * SECOND


def test_session_clock_notices_drift_and_re_anchors() -> None:
    clock = ManualClock(wall=1_000 * SECOND, mono=5 * SECOND)
    session = SessionClock(clock=clock)
    session.start()
    clock.advance(SECOND)
    assert session.drift_ns() == 0
    assert session.needs_anchor() is False

    clock.step_wall(3600 * SECOND)          # an NTP step, or a resume
    assert session.needs_anchor() is True
    session.reanchor()
    assert session.needs_anchor() is False
    assert session.derive(clock.mono_ns()) == clock.wall_ns()
