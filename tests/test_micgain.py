"""Auto mic gain policy (11 Jul). Only the pure recommendation is tested -
the pycaw endpoint wrapper is a thin COM call, exercised live.
"""

from __future__ import annotations

import ctypes
import time

from localflow.desktop import micgain
from localflow.desktop.micgain import recommend_volume


def test_quiet_dictation_raises_level_one_step():
    assert recommend_volume(peak=0.05, current=0.40) == 0.50


def test_step_caps_at_full_volume():
    assert recommend_volume(peak=0.05, current=0.95) == 1.0
    assert recommend_volume(peak=0.05, current=1.0) is None  # nothing left


def test_healthy_peak_changes_nothing():
    assert recommend_volume(peak=0.5, current=0.6) is None
    assert recommend_volume(peak=0.2, current=0.6) is None


def test_clipping_lowers_level_with_a_floor():
    assert recommend_volume(peak=1.0, current=0.80) == 0.70
    assert recommend_volume(peak=1.0, current=0.06) == 0.05  # floored
    assert recommend_volume(peak=1.0, current=0.05) is None  # at the floor


def test_dead_mic_never_cranks_the_slider():
    assert recommend_volume(peak=0.0, current=0.30) is None
    assert recommend_volume(peak=-1.0, current=0.30) is None


def test_cached_no_op_never_touches_com(monkeypatch):
    # Slider already at 100% + quiet peak = nothing to change: the cache must
    # short-circuit BEFORE any COM call (13 Jul crash mitigation).
    monkeypatch.setattr(micgain, "_cached_scalar", 1.0)
    monkeypatch.setattr(micgain, "_cached_at", time.monotonic())

    def boom():
        raise AssertionError("COM was touched")

    monkeypatch.setattr(micgain, "_endpoint_volume", boom)
    assert micgain.adjust_for_peak(0.08) is None


def test_invalidate_cache_forgets_the_level():
    micgain._cached_scalar = 1.0
    micgain.invalidate_cache()
    assert micgain._cached_scalar is None


def test_com_release_releases_once_then_zeroes():
    # The native-crash fix: after com_release, the pointer is NULL, so a
    # GC-run comtypes __del__ (if self: self.Release()) on ANY thread no-ops.
    calls = []

    class Ptr(ctypes.c_void_p):
        def Release(self):
            calls.append(1)

    p = Ptr(1234)
    micgain.com_release(p)
    assert calls == [1]
    assert not p                    # zeroed
    micgain.com_release(p)          # already NULL -> skipped
    micgain.com_release(None)       # tolerated
    assert calls == [1]


def test_com_release_swallows_a_failing_release():
    class Angry(ctypes.c_void_p):
        def Release(self):
            raise OSError("COM says no")

    micgain.com_release(Angry(1))   # must not raise
