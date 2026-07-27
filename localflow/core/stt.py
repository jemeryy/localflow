"""SttEngine — OpenVINO GenAI Whisper with device fallback and timing."""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path

import numpy as np

log = logging.getLogger("localflow.core.stt")

# Whisper special/timestamp markers ("<|7.00|>", "<|en|>", "<|transcribe|>").
# openvino_genai leaks these into the decoded text in return_timestamps mode
# (seen live 11 Jul: "<|7.00|> replaced by..."). Strip them from every decode.
_SPECIAL_TOKEN = re.compile(r"<\|[^|>]*\|>")


def strip_special_tokens(text: str) -> str:
    return _SPECIAL_TOKEN.sub("", text)


def to_language_token(code: str | None) -> str | None:
    """Map a config language code to the Whisper token the pipeline wants.

    "en" -> "<|en|>" (force English), "fr"/"es"/"zh"/... -> "<|xx|>", and
    "auto"/"detect"/""/None -> None (let Whisper detect the language per
    utterance). Already-bracketed tokens pass through unchanged.
    """
    if code is None:
        return None
    c = code.strip().lower()
    if c in ("", "auto", "detect", "none"):
        return None
    c = c.strip("<|>")               # tolerate "<|fr|>" written in the config
    if c in ("", "auto", "detect"):
        return None
    return f"<|{c}|>"


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
        initial_prompt: str | None = None,
    ):
        self.model_dir = str(model_dir)
        self.requested_device = device
        self.fallback_device = fallback_device
        self.model_name = model_name or Path(model_dir).name
        # Accept either a Whisper token ("<|en|>") or a plain code ("en",
        # "fr", "auto"); None / "auto" means detect the language per utterance.
        self.language = to_language_token(language)
        # Vocabulary biasing: Whisper conditions on this text, so domain
        # words listed here ("Codex", "OpenVINO", ...) are far more likely
        # to be transcribed as themselves instead of near-homophones.
        self.initial_prompt = initial_prompt
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
        config.language = self.language     # None = detect per utterance
        config.task = "transcribe"
        log.info("STT language: %s", self.language or "auto-detect")
        if self.initial_prompt:
            if hasattr(config, "initial_prompt"):
                config.initial_prompt = self.initial_prompt
                log.info("hot words active: %s", self.initial_prompt)
            else:  # older openvino_genai without prompt biasing
                log.warning("this openvino_genai has no initial_prompt — "
                            "hot_words are ignored")
        return config

    def transcribe(self, audio_16k_mono: np.ndarray,
                   timestamps: bool = False) -> tuple[str, dict]:
        """audio: float32 mono @16kHz. Returns (text, meta).

        meta = {"stt_ms": int, "device": str, "model": str, "chunks": ...}

        `timestamps` enables return_timestamps so the pipeline can drop
        trailing silence hallucinations by their timing (meta["chunks"] is a
        list of (text, start_ts, end_ts), else None). It roughly doubles decode
        time, so it is only turned on when the hallucination filter is active.
        """
        audio = np.ascontiguousarray(audio_16k_mono, dtype=np.float32)
        if len(audio) == 0:
            return "", {"stt_ms": 0, "device": self.device,
                        "model": self.model_name, "chunks": None}

        self._config.return_timestamps = bool(timestamps)
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
            self._config.return_timestamps = bool(timestamps)
            t0 = time.perf_counter()
            result = self._pipe.generate(audio, self._config)

        stt_ms = int((time.perf_counter() - t0) * 1000)
        text = strip_special_tokens(str(result)).strip()
        chunks = None
        if timestamps:
            raw = getattr(result, "chunks", None) or []
            # Don't .strip() per-chunk text: the pipeline joins chunks and
            # relies on Whisper's leading spaces to keep words apart.
            chunks = [(strip_special_tokens(c.text), float(c.start_ts),
                       float(c.end_ts)) for c in raw]
        log.debug("stt: %dms on %s -> %r", stt_ms, self.device, text)
        return text, {"stt_ms": stt_ms, "device": self.device,
                      "model": self.model_name, "chunks": chunks}
