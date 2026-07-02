"""Mic capture via sounddevice: fixed-duration (M2) and push-to-talk (M3)."""

from __future__ import annotations

import logging
import threading

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


class StreamRecorder:
    """Push-to-talk capture: start() on key-down, stop() on key-up (FR-1).

    The input stream stays open for the recorder's whole life (open it once
    at app startup): opening a WASAPI stream costs ~300ms, which used to eat
    the first spoken words. While idle, the callback keeps only a short
    pre-roll ring (`preroll_s`, then discarded — nothing is retained or sent
    anywhere, per G2); start() seeds the recording with that pre-roll so
    speech that begins right at (or a hair before) key-down is captured.
    stop() returns (audio, rate) ready for localflow.core.ensure_16k_mono.
    Frames past the 60s cap are discarded (FR-3).
    """

    def __init__(
        self,
        input_device: int | str | None = None,
        max_seconds: float = MAX_UTTERANCE_S,
        preroll_s: float = 0.3,
    ):
        info = sd.query_devices(input_device, kind="input")
        self.input_device = input_device
        self.device_name = str(info["name"])
        self.rate = int(info["default_samplerate"])
        self.max_seconds = max_seconds
        self._max_frames = int(max_seconds * self.rate)
        self._preroll_frames = int(preroll_s * self.rate)
        self._lock = threading.Lock()
        self._chunks: list[np.ndarray] = []
        self._preroll: list[np.ndarray] = []
        self._frames = 0
        self._capped = False
        self._recording = False
        self._stream: sd.InputStream | None = None

    @property
    def recording(self) -> bool:
        return self._recording

    def open(self) -> None:
        """Open (or reopen) the persistent stream. Idempotent; call at startup
        so key-down never pays the stream-open latency."""
        if self._stream is not None:
            if self._stream.active:
                return
            self._stream.close()
            self._stream = None
        self._stream = sd.InputStream(
            samplerate=self.rate, channels=1, dtype="float32",
            device=self.input_device, callback=self._on_chunk,
        )
        self._stream.start()
        log.info("mic stream open (%r @ %dHz, %.1fs pre-roll)",
                 self.device_name, self.rate, self._preroll_frames / self.rate)

    def close(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    def start(self) -> None:
        """Begin accumulating, seeded with the pre-roll. No-op if already
        recording (the OS auto-repeats key-down while the hotkey is held)."""
        if self._recording:
            return
        self.open()
        with self._lock:
            self._chunks = list(self._preroll)
            self._frames = sum(len(c) for c in self._chunks)
            self._capped = False
            self._recording = True
        log.info("recording started")

    def _on_chunk(self, indata, frames, time_info, status) -> None:
        # Runs on the PortAudio thread — keep it tiny.
        if status:
            log.warning("capture status: %s", status)
        with self._lock:
            if not self._recording:
                self._preroll.append(indata[:, 0].copy())
                kept = sum(len(c) for c in self._preroll)
                while self._preroll and kept - len(self._preroll[0]) >= self._preroll_frames:
                    kept -= len(self._preroll.pop(0))
                return
            if self._frames >= self._max_frames:
                if not self._capped:
                    self._capped = True
                    log.warning("hit the %.0fs cap — discarding further audio (FR-3)",
                                self.max_seconds)
                return
            take = min(frames, self._max_frames - self._frames)
            self._chunks.append(indata[:take, 0].copy())
            self._frames += take

    def stop(self) -> tuple[np.ndarray, int]:
        """Stop accumulating (the stream stays open) and return
        (float32 mono audio, sample_rate). Safe to call when not recording."""
        with self._lock:
            self._recording = False
            chunks, self._chunks = self._chunks, []
            self._frames = 0
            self._preroll = []
        audio = (np.concatenate(chunks) if chunks
                 else np.zeros(0, dtype=np.float32))
        log.info("recording stopped: %.2fs captured", len(audio) / self.rate)
        return audio, self.rate
