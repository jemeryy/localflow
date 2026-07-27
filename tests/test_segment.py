"""Tier 1 cut-point finder (core/segment.py). Pure logic, no Tk/model/mic -
the whole point of keeping find_cut_point in core is that the silence-boundary
choice is unit-testable in isolation.
"""

from __future__ import annotations

from localflow.core.segment import find_cut_point

R = 16_000  # everything here is expressed in 16kHz samples


def _spans(*pairs):
    """(sec, sec) speech spans -> (sample, sample) tuples."""
    return [(int(a * R), int(b * R)) for a, b in pairs]


def test_no_spans_is_none():
    # all silence -> nothing to segment (the VAD gate handles empties anyway)
    assert find_cut_point([], 30 * R) is None
    assert find_cut_point([], 0) is None


def test_prefers_the_last_qualifying_silence():
    # three sentences with pauses; the LAST safe gap wins so the emitted
    # segment carries as much complete speech as possible.
    cut = find_cut_point(_spans((0, 5), (8, 13), (16, 20)), 23 * R)
    assert cut == (20 * R + 23 * R) // 2          # midpoint of the trailing gap


def test_cuts_at_internal_gap_midpoint():
    # one internal pause 10.0-11.0s, then talking continues to the live edge
    # (no trailing silence): cut in the middle of the pause, at 10.5s.
    cut = find_cut_point(_spans((0, 10), (11, 20)), 20 * R)
    assert cut == (10 * R + 11 * R) // 2          # 10.5s


def test_short_gap_is_ignored():
    # a 0.2s dip between words is below min_gap; fall back to the trailing gap
    cut = find_cut_point(_spans((0, 10), (10.2, 20)), 22 * R)
    assert cut is not None
    assert cut > 20 * R                           # landed in the trailing gap


def test_edge_guard_excludes_a_gap_at_the_live_edge():
    # only silence is the last 1.5s, whose midpoint sits inside the 1s edge
    # guard (the speaker may still be talking) -> wait, don't cut yet.
    assert find_cut_point(_spans((0, 10)), int(11.5 * R)) is None


def test_trailing_silence_past_the_edge_guard_is_cuttable():
    # a clear 4s pause after the last word -> safe to cut mid-pause
    cut = find_cut_point(_spans((0, 10)), 14 * R)
    assert cut == (10 * R + 14 * R) // 2          # 12s, well before the edge


def test_min_lead_blocks_a_too_early_cut():
    # the only gaps sit before min_lead_s (5s), so there is nothing worth
    # emitting yet.
    assert find_cut_point(_spans((0, 1), (2, 3)), 4 * R) is None


def test_leading_silence_never_qualifies():
    # silence before the first span has nothing to emit ahead of it; the only
    # usable gap is the trailing one.
    cut = find_cut_point(_spans((6, 10)), 14 * R)
    assert cut == (10 * R + 14 * R) // 2          # trailing gap, not the lead


def test_thresholds_scale_with_sample_rate():
    # same shape at 48kHz: a 2s internal gap at 10-12s -> cut at 11s.
    r = 48_000
    spans = [(0, 10 * r), (12 * r, 20 * r)]
    cut = find_cut_point(spans, 25 * r, sample_rate=r)
    assert cut is not None
    # the last gap is the trailing one (20-25s) -> midpoint 22.5s
    assert cut == (20 * r + 25 * r) // 2
