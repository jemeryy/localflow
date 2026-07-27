"""transcribe_audio() — the one core entry point: audio in, TranscriptResult out."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import numpy as np

from .audio import TARGET_RATE
from .cleanup import CleanupEngine
from .hallucination import (
    collapse_repeats,
    drop_trailing_hallucinated_chunks,
    strip_hallucinations,
)
from .preprocess import AudioPreprocessor
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
    preprocess: AudioPreprocessor | None = None,
    filter_hallucinations: bool = True,
) -> TranscriptResult:
    """Run the core pipeline: (optional) preprocess -> (optional) VAD trim ->
    STT -> hallucination filter -> (optional) cleanup.

    Preprocessing (high-pass / denoise / normalize) runs first, on the full
    buffer, so the noise estimate can see the non-speech regions and both the
    VAD and Whisper receive the cleaned signal. If the VAD gate then finds no
    speech, returns a result with raw_text == "" and zero timings — callers
    must treat that as "produce no output" (FR-4).

    `filter_hallucinations` strips Whisper's trailing-silence artifacts (a
    detached trailing "You.", or a whole-utterance stock phrase) from the raw
    text before cleanup; a gated whole-utterance becomes "" (paste nothing).
    """
    audio = np.ascontiguousarray(audio_16k_mono, dtype=np.float32)

    if preprocess is not None:
        audio = preprocess.process(audio)

    if vad is not None:
        trimmed = vad.trim(audio)
        if trimmed is None:
            return TranscriptResult(
                raw_text="", clean_text=None, duration_s=0.0, stt_ms=0,
                cleanup_ms=None, device=stt.device, model=stt.model_name,
            )
        audio = trimmed

    duration_s = len(audio) / TARGET_RATE
    # Timestamps let us drop trailing silence hallucinations by their timing
    # (a stray word/phrase Whisper appends to the tail); only worth the ~2x
    # decode cost when the filter is on.
    raw_text, meta = stt.transcribe(audio, timestamps=filter_hallucinations)

    if filter_hallucinations and raw_text:
        original = raw_text
        chunks = meta.get("chunks")
        if chunks:
            raw_text = drop_trailing_hallucinated_chunks(chunks, duration_s)
        raw_text = collapse_repeats(raw_text)       # decode-loop repeats
        raw_text = strip_hallucinations(raw_text)   # whole-utterance backstop
        if raw_text != original:
            log.info("hallucination filter: %r -> %r", original, raw_text)

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
