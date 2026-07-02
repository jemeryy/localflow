"""VadGate — Silero VAD (ONNX on CPU) speech trim with context padding."""

from __future__ import annotations

import logging

import numpy as np
from silero_vad import get_speech_timestamps, load_silero_vad

from .audio import TARGET_RATE

log = logging.getLogger("localflow.core.vad")


class VadGate:
    """Trims a 16kHz mono buffer to its speech extent, or gates it to None.

    Returning None means "no speech here": the caller must produce NO text
    (FR-4 — Whisper hallucinates on non-speech input).

    Keeps `leading_pad_s` / `trailing_pad_s` of real audio context around the
    outermost speech frames. The trailing pad is load-bearing: the Arc GPU
    plugin drops unusually quiet final words when the clip ends hard on the
    last speech frame (docs/m1-results.md) — keep 0.3–0.5s.
    """

    def __init__(
        self,
        threshold: float = 0.5,
        min_speech_ms: int = 250,
        leading_pad_s: float = 0.2,
        trailing_pad_s: float = 0.5,
    ):
        self.threshold = threshold
        self.min_speech_ms = min_speech_ms
        self.leading_pad = int(leading_pad_s * TARGET_RATE)
        self.trailing_pad = int(trailing_pad_s * TARGET_RATE)
        # Weights ship inside the silero-vad wheel; ONNX runs on CPU in <1ms
        # per frame, leaving GPU/NPU free for STT. No network involved.
        self._model = load_silero_vad(onnx=True)

    def trim(self, audio_16k_mono: np.ndarray) -> np.ndarray | None:
        audio = np.ascontiguousarray(audio_16k_mono, dtype=np.float32)
        total_s = len(audio) / TARGET_RATE
        spans = get_speech_timestamps(
            audio,
            self._model,
            threshold=self.threshold,
            sampling_rate=TARGET_RATE,
            min_speech_duration_ms=self.min_speech_ms,
        )
        if not spans:
            log.info("VAD: no speech in %.2fs buffer — silence gate (FR-4)", total_s)
            return None
        start = max(0, spans[0]["start"] - self.leading_pad)
        end = min(len(audio), spans[-1]["end"] + self.trailing_pad)
        log.info(
            "VAD: %d speech span(s), %.2fs captured -> %.2fs trimmed",
            len(spans), total_s, (end - start) / TARGET_RATE,
        )
        return audio[start:end]
