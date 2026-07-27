"""Tier 1 (VAD-segmented streaming): choose where to cut a long recording.

Pure logic only — no I/O, no Tk, no model, no desktop imports (the core seam,
PRD §3). The desktop Segmenter feeds Silero speech spans in; this returns a
sample index to cut the buffer at, always inside a silence gap so neither the
emitted segment nor the retained tail splits a word. Keeping it pure is what
lets the cut boundary be unit-tested without a mic or a model.
"""

from __future__ import annotations

from .audio import TARGET_RATE


def find_cut_point(
    speech_spans: list[tuple[int, int]],
    total_samples: int,
    sample_rate: int = TARGET_RATE,
    min_lead_s: float = 5.0,
    min_gap_s: float = 0.6,
    edge_guard_s: float = 1.0,
) -> int | None:
    """Return a sample index to split a held recording at, or None if there is
    no safe silence yet.

    `speech_spans` are (start, end) sample indices from a VAD, sorted and
    non-overlapping (Silero delivers them that way). The cut lands at the
    MIDPOINT of a silence gap so both sides keep a little padding around the
    speech. The LAST qualifying gap wins, which maximizes how much complete
    speech the emitted segment carries while still leaving the ongoing tail.

    A gap qualifies when:
    - it is at least `min_gap_s` long (a real pause, not a within-word dip),
    - its midpoint is at least `min_lead_s` in (don't emit a tiny segment),
    - its midpoint is at least `edge_guard_s` before the live edge (the very
      end of the buffer may be speech in progress — never cut there).

    The leading silence before the first span never qualifies (nothing to emit
    before it). All-silence input returns None (the VAD gate handles it).
    """
    if total_samples <= 0 or not speech_spans:
        return None
    min_lead = int(min_lead_s * sample_rate)
    min_gap = int(min_gap_s * sample_rate)
    ceiling = total_samples - int(edge_guard_s * sample_rate)

    # Silence gaps that are safe to cut in: the internal gaps between adjacent
    # speech spans, plus the trailing gap after the last span up to the end.
    gaps: list[tuple[int, int]] = []
    prev_end = speech_spans[0][1]
    for start, end in speech_spans[1:]:
        gaps.append((prev_end, start))
        prev_end = max(prev_end, end)
    gaps.append((prev_end, total_samples))

    best: int | None = None
    for gap_start, gap_end in gaps:
        if gap_end - gap_start < min_gap:
            continue
        mid = (gap_start + gap_end) // 2
        if mid < min_lead or mid > ceiling:
            continue
        best = mid  # gaps are in order, so the last qualifying one wins
    return best
