"""Audio shaping helpers. Everything downstream expects 16kHz mono float32."""

from __future__ import annotations

import numpy as np

TARGET_RATE = 16_000


def ensure_16k_mono(audio: np.ndarray, sample_rate: int) -> np.ndarray:
    """Convert a PCM array to contiguous float32 mono @16kHz.

    Accepts 1-D (mono) or 2-D (frames, channels) arrays in integer or float
    dtypes — covers sounddevice capture now and decoded browser uploads in
    Phase 2. Resamples with soxr when the input rate differs.
    """
    if np.issubdtype(audio.dtype, np.integer):
        audio = audio.astype(np.float32) / float(np.iinfo(audio.dtype).max)

    if audio.ndim == 2:
        audio = audio.mean(axis=1)
    elif audio.ndim != 1:
        raise ValueError(f"expected 1-D or 2-D audio, got shape {audio.shape}")

    audio = audio.astype(np.float32, copy=False)

    if sample_rate != TARGET_RATE:
        import soxr

        audio = soxr.resample(audio, sample_rate, TARGET_RATE)

    return np.ascontiguousarray(audio, dtype=np.float32)
