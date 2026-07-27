"""Tier 1: VAD-segmented streaming transcription.

While the hotkey is held, this background thread watches the live recording;
once it passes the target length it uses a PRIVATE Silero VAD to find a recent
silence, cuts the buffer there, and hands the completed segment to the worker
to transcribe DURING the hold. Cutting at silence means the per-segment texts
join cleanly with no overlap-stitching, and the end-of-dictation wait is bounded
to roughly one final segment instead of the whole buffer.

Short dictations (under the target) never trigger a cut and behave exactly as
before — a single segment produced on release. Set segment_seconds to 0 to turn
segmentation off entirely.

Threading: this thread only ever READS the recorder (snapshot / buffered_frames)
and calls the app's `emit` callback, which does the drain+enqueue atomically
under the app's produce lock so a cut can never race the on-release final. The
VAD here is a separate instance from the worker's, so the two ONNX sessions are
never driven concurrently.
"""

from __future__ import annotations

import logging
import threading

from localflow.core import ensure_16k_mono, find_cut_point
from localflow.core.audio import TARGET_RATE

log = logging.getLogger("localflow.desktop.segmenter")

POLL_INTERVAL_S = 1.0
# If no silence is found this far past the target, force a cut anyway (accept a
# rare mid-word split) so a continuous monologue can't grow the buffer forever.
HARD_EXTRA_S = 15.0


class Segmenter:
    """Polls a StreamRecorder while it records and emits mid-dictation segments
    at silence boundaries. `emit(cut_frame, dictation_id) -> bool` and
    `current_id() -> int` are supplied by the app; `vad` is a private VadGate
    (set after warmup)."""

    def __init__(self, recorder, vad, emit, current_id, segment_seconds,
                 poll_interval_s: float = POLL_INTERVAL_S,
                 hard_extra_s: float = HARD_EXTRA_S):
        self.recorder = recorder
        self.vad = vad
        self.emit = emit
        self.current_id = current_id
        self.segment_seconds = float(segment_seconds)
        self.poll_interval_s = poll_interval_s
        self.hard_extra_s = hard_extra_s
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="localflow-segmenter")

    def set_segment_seconds(self, seconds: float) -> None:
        """Live-apply the target segment length (settings 'Apply'). 0 turns
        segmentation off; takes effect on the next poll."""
        self.segment_seconds = float(seconds)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self.poll_interval_s):
            try:
                self._tick()
            except Exception:
                log.exception("segmenter pass failed — dictation continues "
                              "unsegmented")

    def _tick(self) -> None:
        if self.segment_seconds <= 0 or self.vad is None:
            return
        if not self.recorder.recording:
            return
        rate = self.recorder.rate
        if self.recorder.buffered_frames() < self.segment_seconds * rate:
            return
        dictation_id = self.current_id()
        cut_native = self._find_cut(rate)
        if cut_native is None:
            return
        if self.emit(cut_native, dictation_id):
            log.info("segment cut at %.1fs — transcribing while still held",
                     cut_native / rate)

    def _find_cut(self, rate: int) -> int | None:
        """Native-rate frame index to cut at, or None to wait for more audio.
        Snapshot -> resample to 16k -> VAD spans -> pure find_cut_point, mapped
        back to the recorder's native rate."""
        snap = self.recorder.snapshot()      # native-rate mono float32
        if len(snap) == 0:
            return None
        audio16 = ensure_16k_mono(snap, rate)
        spans = self.vad.speech_spans(audio16)
        cut16 = find_cut_point(spans, len(audio16), sample_rate=TARGET_RATE)
        if cut16 is None:
            # No silence to cut at. Only force a mid-word cut once the buffer
            # has run well past the target, so a normal brief pause still gets
            # a clean boundary on the next poll.
            if len(snap) / rate < self.segment_seconds + self.hard_extra_s:
                return None
            cut16 = int(self.segment_seconds * TARGET_RATE)
        cut_native = int(cut16 / TARGET_RATE * rate)
        if cut_native <= 0 or cut_native >= len(snap):
            return None
        return cut_native
