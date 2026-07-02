"""Mic capture via sounddevice. Fixed-duration for M2; push-to-talk lands in M3."""

from __future__ import annotations

import logging

import numpy as np
import sounddevice as sd

log = logging.getLogger("localflow.desktop.capture")

MAX_UTTERANCE_S = 60.0  # FR-3 safety cap


def record_audio(
    seconds: float, input_device: int | str | None = None
) -> tuple[np.ndarray, int]:
    """Blocking fixed-length capture at the device's native rate (FR-3).

    Returns (float32 mono 1-D array, sample_rate). Feed through
    localflow.core.ensure_16k_mono before transcription — capturing at the
    native rate and resampling ourselves is deterministic across drivers,
    unlike asking WASAPI for 16kHz directly.
    """
    if seconds > MAX_UTTERANCE_S:
        log.warning("capping recording at %.0fs (asked %.0fs)", MAX_UTTERANCE_S, seconds)
        seconds = MAX_UTTERANCE_S

    info = sd.query_devices(input_device, kind="input")
    rate = int(info["default_samplerate"])
    log.info("recording %.1fs from %r @ %dHz", seconds, info["name"], rate)

    frames = int(seconds * rate)
    buf = sd.rec(frames, samplerate=rate, channels=1, dtype="float32",
                 device=input_device)
    sd.wait()
    return buf[:, 0].copy(), rate
