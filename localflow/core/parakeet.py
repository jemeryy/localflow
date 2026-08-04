"""Parakeet TDT engine — the transducer alternative to Whisper (spike:
docs/parakeet-spike.md, 4 Aug 2026).

A transducer emits each token locked to a slice of audio frames, so the
Whisper failure modes this project has fought since July — decode loops,
silence hallucinations, dropped trailing words — are structurally impossible.
The A/B put it ahead of whisper-turbo on every case including quiet mics and
repeat counts.

Same contract as `stt.SttEngine` (`transcribe(audio_16k_mono) -> (text,
meta)`), so the desktop layer swaps engines on the `stt_model` config key
without knowing which is loaded. Runs on CPU via onnxruntime — deliberately:
it frees the shared-memory iGPU, which removes the preview-decode memory
pressure entirely. `timestamps` is accepted and ignored (meta["chunks"] is
always None; the chunk-timing filter is a Whisper-ism), as is
`rebuild_on_failure` (there is no fallback device to rebuild on — a CPU
engine either works or raises).
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import numpy as np

log = logging.getLogger("localflow.core.parakeet")


class ParakeetEngine:
    """Wraps onnx-asr's Parakeet TDT loader (int8 ONNX, CPU)."""

    def __init__(self, model_dir: str, model_name: str | None = None):
        import onnx_asr   # deferred: only the parakeet config path pays it

        self.model_dir = str(model_dir)
        self.model_name = model_name or Path(model_dir).name
        self.device = "CPU"
        t0 = time.perf_counter()
        self._model = onnx_asr.load_model(
            "nemo-parakeet-tdt-0.6b-v2", path=self.model_dir,
            quantization="int8")
        self.load_s = time.perf_counter() - t0
        log.info("loaded %s on CPU in %.1fs", self.model_name, self.load_s)

    def transcribe(self, audio_16k_mono: np.ndarray,
                   timestamps: bool = False,
                   rebuild_on_failure: bool = True) -> tuple[str, dict]:
        """audio: float32 mono @16kHz. Returns (text, meta) — the SttEngine
        contract; meta["chunks"] is always None."""
        audio = np.ascontiguousarray(audio_16k_mono, dtype=np.float32)
        meta = {"stt_ms": 0, "device": self.device,
                "model": self.model_name, "chunks": None}
        if len(audio) == 0:
            return "", meta
        t0 = time.perf_counter()
        text = self._model.recognize(audio, sample_rate=16000)
        meta["stt_ms"] = int((time.perf_counter() - t0) * 1000)
        return (text or "").strip(), meta
