"""SttEngine — OpenVINO GenAI Whisper with device fallback and timing."""

from __future__ import annotations

import logging
import time
from pathlib import Path

import numpy as np

log = logging.getLogger("localflow.core.stt")


class SttEngine:
    """Wraps openvino_genai.WhisperPipeline (PRD §4).

    Construction compiles the model on the requested device (3–14s on GPU,
    ~3s on NPU with a warm CACHE_DIR) — build once at startup, never per
    utterance (FR-14). On init or inference failure it falls back to
    `fallback_device` with a loud warning instead of crashing (FR-6).
    """

    def __init__(
        self,
        model_dir: str,
        device: str = "GPU",
        fallback_device: str = "CPU",
        cache_dir: str | None = None,
        model_name: str | None = None,
        language: str = "<|en|>",
    ):
        self.model_dir = str(model_dir)
        self.requested_device = device
        self.fallback_device = fallback_device
        self.model_name = model_name or Path(model_dir).name
        self.language = language
        self._props: dict[str, str] = {}
        if cache_dir is not None:
            # Compile cache: free load-time win on every device, and the NPU
            # blob costs ~188s to rebuild without it (docs/m1-results.md).
            cache = Path(cache_dir)
            cache.mkdir(parents=True, exist_ok=True)
            self._props["CACHE_DIR"] = str(cache)

        self.device, self._pipe, self.load_s = self._build_with_fallback(device)
        self._config = self._make_config()

    def _build(self, device: str):
        import openvino_genai as ov_genai

        t0 = time.perf_counter()
        pipe = ov_genai.WhisperPipeline(self.model_dir, device, **self._props)
        return pipe, time.perf_counter() - t0

    def _build_with_fallback(self, device: str):
        try:
            pipe, load_s = self._build(device)
            log.info("loaded %s on %s in %.1fs", self.model_name, device, load_s)
            return device, pipe, load_s
        except Exception as e:
            if device == self.fallback_device:
                raise
            log.warning(
                "init on %s FAILED (%s: %s) — falling back to %s. CPU runs the "
                "turbo model at ~6s/utterance; consider a smaller model (PRD §7).",
                device, type(e).__name__, e, self.fallback_device,
            )
            pipe, load_s = self._build(self.fallback_device)
            log.info("loaded %s on %s in %.1fs", self.model_name, self.fallback_device, load_s)
            return self.fallback_device, pipe, load_s

    def _make_config(self):
        config = self._pipe.get_generation_config()
        config.language = self.language
        config.task = "transcribe"
        return config

    def transcribe(self, audio_16k_mono: np.ndarray) -> tuple[str, dict]:
        """audio: float32 mono @16kHz. Returns (text, meta).

        meta = {"stt_ms": int, "device": str, "model": str}
        """
        audio = np.ascontiguousarray(audio_16k_mono, dtype=np.float32)
        if len(audio) == 0:
            return "", {"stt_ms": 0, "device": self.device, "model": self.model_name}

        try:
            t0 = time.perf_counter()
            result = self._pipe.generate(audio, self._config)
        except Exception as e:
            if self.device == self.fallback_device:
                raise
            log.warning(
                "inference on %s FAILED (%s: %s) — rebuilding on %s and retrying",
                self.device, type(e).__name__, e, self.fallback_device,
            )
            self.device, self._pipe, self.load_s = self._build_with_fallback(
                self.fallback_device
            )
            self._config = self._make_config()
            t0 = time.perf_counter()
            result = self._pipe.generate(audio, self._config)

        stt_ms = int((time.perf_counter() - t0) * 1000)
        text = str(result).strip()
        log.debug("stt: %dms on %s -> %r", stt_ms, self.device, text)
        return text, {"stt_ms": stt_ms, "device": self.device, "model": self.model_name}
