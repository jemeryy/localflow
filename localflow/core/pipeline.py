"""transcribe_audio() — the one core entry point: audio in, TranscriptResult out."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import numpy as np

from .audio import TARGET_RATE
from .cleanup import CleanupEngine
from .stt import SttEngine
from .vad import VadGate

log = logging.getLogger("localflow.core")


@dataclass
class TranscriptResult:
    raw_text: str              # verbatim STT output; "" means silence-gated: paste nothing
    clean_text: str | None     # cleanup output, or None if cleanup disabled/failed
    duration_s: float          # length of the trimmed audio actually transcribed
    stt_ms: int                # STT compute time
    cleanup_ms: int | None
    device: str                # "GPU" | "NPU" | "CPU" actually used
    model: str                 # e.g. "whisper-turbo-ov"


def transcribe_audio(
    audio_16k_mono: np.ndarray,
    stt: SttEngine,
    cleanup: CleanupEngine | None = None,
    vad: VadGate | None = None,
) -> TranscriptResult:
    """Run the core pipeline: (optional) VAD trim -> STT -> (optional) cleanup.

    If the VAD gate finds no speech, returns a result with raw_text == "" and
    zero timings — callers must treat that as "produce no output" (FR-4).
    """
    audio = np.ascontiguousarray(audio_16k_mono, dtype=np.float32)

    if vad is not None:
        trimmed = vad.trim(audio)
        if trimmed is None:
            return TranscriptResult(
                raw_text="", clean_text=None, duration_s=0.0, stt_ms=0,
                cleanup_ms=None, device=stt.device, model=stt.model_name,
            )
        audio = trimmed

    duration_s = len(audio) / TARGET_RATE
    raw_text, meta = stt.transcribe(audio)

    clean_text: str | None = None
    cleanup_ms: int | None = None
    if cleanup is not None and raw_text:
        t0 = time.perf_counter()
        try:
            clean_text = cleanup.clean(raw_text)
            cleanup_ms = int((time.perf_counter() - t0) * 1000)
        except Exception:
            log.warning("cleanup failed — keeping raw text only", exc_info=True)
            clean_text = None
            cleanup_ms = None

    log.info(
        "utterance: %.2fs audio, stt=%dms on %s (%s)%s",
        duration_s, meta["stt_ms"], meta["device"], meta["model"],
        f", cleanup={cleanup_ms}ms" if cleanup_ms is not None else "",
    )
    return TranscriptResult(
        raw_text=raw_text,
        clean_text=clean_text,
        duration_s=round(duration_s, 3),
        stt_ms=meta["stt_ms"],
        cleanup_ms=cleanup_ms,
        device=meta["device"],
        model=meta["model"],
    )
