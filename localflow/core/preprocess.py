"""Audio preprocessing (Jeremy's request): clean the mic signal before STT.

Pure DSP on a 16kHz mono float32 buffer — no I/O, no network, no desktop
imports — so it lives in the core seam (PRD §3) and FARA reuses it too. It runs
BEFORE the VAD gate in transcribe_audio(), so the noise estimate sees the
non-speech regions (pre-roll + trailing pad) and both the VAD and Whisper get
the cleaned signal.

Three stages, each independently toggleable:

1. High-pass filter (~80Hz): drops HVAC rumble, desk thumps and mains hum that
   sit below the speech band. Cheap and safe.
2. Spectral-gating noise reduction: estimates a per-frequency noise floor from
   the quietest frames, keeps bins that stand above it and attenuates the rest
   toward a spectral floor of (1 - denoise_strength), then smooths the mask in
   time and frequency so it never fully nulls a bin — a hard null leaves
   'musical noise' Whisper mishears. This is the stage most likely to cost
   accuracy if pushed too hard, so denoise_strength is the dial to back off
   (the M4 handoff warns that over-processing hurt WER before).
3. Peak normalization: the built-in mic array and the Samsung headset both run
   quiet (M2), and Whisper loses SNR on a low signal. Boost toward a target
   peak, using a high percentile as the reference so a lone click doesn't cap
   the gain, capped so near-silence isn't amplified into a roar.
"""

from __future__ import annotations

import logging

import numpy as np

from .audio import TARGET_RATE

log = logging.getLogger("localflow.core.preprocess")


def high_pass(audio: np.ndarray, sample_rate: int = TARGET_RATE,
              cutoff_hz: float = 80.0, order: int = 2) -> np.ndarray:
    """Zero-phase Butterworth high-pass. Returns the audio unchanged if it is
    too short to filter cleanly or scipy is unavailable."""
    if cutoff_hz <= 0 or len(audio) < 3 * (2 * order + 1):
        return audio
    try:
        import scipy.signal as sps

        sos = sps.butter(order, cutoff_hz / (sample_rate / 2.0),
                         btype="highpass", output="sos")
        return sps.sosfiltfilt(sos, audio).astype(np.float32, copy=False)
    except Exception:
        log.debug("high-pass skipped", exc_info=True)
        return audio


def estimate_noise_floor(audio: np.ndarray, sample_rate: int = TARGET_RATE,
                         n_fft: int = 512, hop: int = 128) -> np.ndarray:
    """Per-frequency noise floor of a (quiet, speech-free) calibration clip
    (M9). Same STFT parameters as reduce_noise, so the result can be passed
    straight in as ``noise_floor``. Median magnitude per bin: robust to the
    odd keyboard click in an otherwise steady room tone. Pure: the caller
    owns persistence (core does no file I/O). Shape: (n_fft // 2 + 1, 1)."""
    import scipy.signal as sps

    audio = np.ascontiguousarray(audio, dtype=np.float32)
    if len(audio) < n_fft * 2:
        raise ValueError(f"calibration clip too short: {len(audio)} samples")
    _, _, Z = sps.stft(audio, fs=sample_rate, nperseg=n_fft,
                       noverlap=n_fft - hop)
    return np.median(np.abs(Z), axis=1, keepdims=True).astype(np.float32)


def recommend_tuning(noise_rms: float) -> tuple[float, float]:
    """Map a measured room-noise level to recommended audio-tuning slider
    values (M9): (denoise_strength, vad_threshold/sensitivity).

    ``noise_rms`` is the broadband RMS (linear, ~0-1) of a quiet, speech-free
    calibration clip AFTER the same high-pass the denoiser sees. Louder room
    -> stronger denoise + a higher speech gate (so background noise doesn't
    trigger); quieter room -> lighter denoise + a lower gate (accept quiet
    speech). Pure and unit-tested; breakpoints are first-pass and meant to be
    tuned live. Returns values already clamped to their slider ranges:
    denoise in [0, 1], sensitivity in [0.3, 0.7]."""
    import math

    db = 20.0 * math.log10(max(float(noise_rms), 1e-7))     # dBFS, negative
    # -55 dBFS ~ a quiet room; -35 dBFS ~ a noticeably noisy one (fan/keyboard).
    quiet_db, loud_db = -55.0, -35.0
    t = min(1.0, max(0.0, (db - quiet_db) / (loud_db - quiet_db)))
    denoise = round(min(1.0, max(0.0, 0.25 + t * (0.75 - 0.25))), 2)
    sensitivity = round(min(0.7, max(0.3, 0.42 + t * (0.62 - 0.42))), 2)
    return denoise, sensitivity


def reduce_noise(audio: np.ndarray, sample_rate: int = TARGET_RATE,
                 n_fft: int = 512, hop: int = 128, noise_percentile: float = 30.0,
                 gate_factor: float = 1.5, denoise_strength: float = 0.6,
                 smooth_freq: int = 2, smooth_time: int = 3,
                 noise_floor: np.ndarray | None = None) -> np.ndarray:
    """Stationary spectral-gating denoise.

    Estimates the noise floor per frequency from the quietest frames
    (``noise_percentile``), keeps STFT bins above ``gate_factor`` x floor and
    attenuates the rest to a floor of ``(1 - denoise_strength)``, smooths that
    mask in time and frequency to suppress musical noise, then reconstructs.
    ``denoise_strength`` in [0, 1]: 0 disables, ~0.6 gentle, ~0.9 aggressive
    (risks eating quiet speech).

    ``noise_floor`` (M9): a calibrated per-frequency floor from
    estimate_noise_floor(). Better than the per-clip percentile when the
    clip is mostly speech (the percentile then overestimates the floor and
    eats quiet speech). Falls back to the per-clip estimate on any shape
    mismatch (e.g. a profile from different STFT settings)."""
    if denoise_strength <= 0 or len(audio) < n_fft * 2:
        return audio
    try:
        import scipy.signal as sps
        from scipy.ndimage import uniform_filter
    except Exception:
        log.debug("denoise skipped — scipy unavailable", exc_info=True)
        return audio
    try:
        _, _, Z = sps.stft(audio, fs=sample_rate, nperseg=n_fft,
                           noverlap=n_fft - hop)
        mag = np.abs(Z)
        if (noise_floor is not None
                and noise_floor.shape[0] == mag.shape[0]):
            floor = noise_floor.reshape(-1, 1)
        else:
            if noise_floor is not None:
                log.debug("noise profile shape %s does not fit %s bins — "
                          "using the per-clip estimate",
                          noise_floor.shape, mag.shape[0])
            floor = np.percentile(mag, noise_percentile, axis=1, keepdims=True)
        keep = (mag >= floor * gate_factor).astype(np.float32)
        gain = keep + (1.0 - keep) * (1.0 - denoise_strength)
        gain = uniform_filter(gain, size=(max(1, smooth_freq), max(1, smooth_time)))
        _, out = sps.istft(Z * gain, fs=sample_rate, nperseg=n_fft,
                          noverlap=n_fft - hop)
        out = np.asarray(out, dtype=np.float32)
        if len(out) >= len(audio):
            return out[:len(audio)]
        return np.pad(out, (0, len(audio) - len(out)))
    except Exception:
        log.debug("denoise failed — returning original audio", exc_info=True)
        return audio


def normalize_peak(audio: np.ndarray, target_peak: float = 0.95,
                   max_gain: float = 20.0) -> np.ndarray:
    """Scale toward ``target_peak`` using the 99.5th percentile of |audio| as
    the reference (so a lone click doesn't cap the gain), clamp the gain so
    near-silence isn't blown up, then hard-clip the few over-target samples.
    No-op on silence."""
    if len(audio) == 0:
        return audio
    ref = float(np.percentile(np.abs(audio), 99.5))
    if ref < 1e-4:
        return audio  # silence / no signal — don't amplify hiss
    if ref >= target_peak:
        return audio  # already loud enough — don't boost into clipping
    gain = min(target_peak / ref, max_gain)
    out = np.clip(audio * gain, -1.0, 1.0)  # brickwall the few outlier samples
    return out.astype(np.float32, copy=False)


class AudioPreprocessor:
    """Config-driven cleanup chain. ``process()`` is a passthrough when every
    stage is disabled, so transcribe_audio can hold one optional reference and
    not branch. The high-pass rides with the denoise toggle (both are 'clean up
    background noise'); normalization is the separate 'even out loudness' knob.
    """

    def __init__(self, sample_rate: int = TARGET_RATE, denoise: bool = True,
                 normalize: bool = True, denoise_strength: float = 0.6,
                 high_pass_hz: float = 80.0, target_peak: float = 0.95,
                 noise_floor: np.ndarray | None = None):
        self.sample_rate = sample_rate
        self.denoise = denoise
        self.normalize = normalize
        self.denoise_strength = float(np.clip(denoise_strength, 0.0, 1.0))
        self.high_pass_hz = high_pass_hz if denoise else 0.0
        self.target_peak = target_peak
        # M9: calibrated per-frequency floor (estimate_noise_floor); None =
        # per-clip percentile estimate. Passed in by the desktop layer -
        # the profile FILE lives there, core stays pure.
        self.noise_floor = noise_floor

    @property
    def active(self) -> bool:
        return bool(self.high_pass_hz) or self.denoise or self.normalize

    def describe(self) -> str:
        parts = []
        if self.high_pass_hz:
            parts.append(f"high-pass {self.high_pass_hz:.0f}Hz")
        if self.denoise:
            note = " (calibrated)" if self.noise_floor is not None else ""
            parts.append(f"denoise {self.denoise_strength:.2f}{note}")
        if self.normalize:
            parts.append(f"normalize -> {self.target_peak:.2f}")
        return ", ".join(parts) if parts else "off"

    def process(self, audio_16k_mono: np.ndarray) -> np.ndarray:
        audio = np.ascontiguousarray(audio_16k_mono, dtype=np.float32)
        if len(audio) == 0 or not self.active:
            return audio
        audio = audio - float(np.mean(audio))          # remove DC offset
        if self.high_pass_hz:
            audio = high_pass(audio, self.sample_rate, self.high_pass_hz)
        if self.denoise:
            audio = reduce_noise(audio, self.sample_rate,
                                 denoise_strength=self.denoise_strength,
                                 noise_floor=self.noise_floor)
        if self.normalize:
            audio = normalize_peak(audio, self.target_peak)
        return np.ascontiguousarray(audio, dtype=np.float32)
