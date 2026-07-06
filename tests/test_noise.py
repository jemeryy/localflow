"""M9 tests: calibrated noise floor (core, pure) + profile store (desktop)
+ vad_threshold plumbing. Headless-safe: synthetic audio only."""

from __future__ import annotations

import numpy as np
import pytest

from localflow.core.preprocess import estimate_noise_floor, reduce_noise


def _noise(n: int, amp: float, seed: int = 7) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return (rng.standard_normal(n) * amp).astype(np.float32)


def _tone(n: int, freq: float, amp: float = 0.5, rate: int = 16_000) -> np.ndarray:
    t = np.arange(n, dtype=np.float32) / rate
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


# -- estimate_noise_floor ---------------------------------------------------

def test_noise_floor_shape_and_scaling():
    quiet = estimate_noise_floor(_noise(16_000, 0.01))
    loud = estimate_noise_floor(_noise(16_000, 0.1))
    assert quiet.shape == (257, 1)               # n_fft 512 -> 257 bins
    assert quiet.dtype == np.float32
    # a 10x louder room measures a clearly higher floor everywhere
    assert float(np.median(loud / np.maximum(quiet, 1e-12))) > 5.0


def test_noise_floor_rejects_too_short_clip():
    with pytest.raises(ValueError, match="too short"):
        estimate_noise_floor(np.zeros(100, dtype=np.float32))


# -- reduce_noise with an injected floor -------------------------------------

def test_injected_floor_beats_per_clip_estimate_on_speechy_clips():
    """The point of calibration (M9): when the clip is mostly 'speech', the
    per-clip percentile overestimates the floor and eats signal; a floor
    measured on real room noise attenuates the noise, not the speech."""
    n = 32_000                                   # 2s @16k
    speech = _tone(n, 440.0, amp=0.4)            # stands in for speech
    noise = _noise(n, 0.02)
    clip = speech + noise
    floor = estimate_noise_floor(_noise(n, 0.02, seed=8))  # separate capture

    with_profile = reduce_noise(clip, denoise_strength=0.9,
                                noise_floor=floor)
    without = reduce_noise(clip, denoise_strength=0.9)

    def band_energy(x: np.ndarray, low: float, high: float) -> float:
        spec = np.abs(np.fft.rfft(x))
        freqs = np.fft.rfftfreq(len(x), d=1 / 16_000)
        return float(np.sum(spec[(freqs >= low) & (freqs <= high)] ** 2))

    tone_kept_profile = band_energy(with_profile, 400, 480)
    tone_kept_plain = band_energy(without, 400, 480)
    # the calibrated floor must keep at least as much of the speech band
    assert tone_kept_profile >= tone_kept_plain * 0.99
    # and still remove noise: broadband energy well below the input's
    assert band_energy(with_profile, 3000, 7000) < band_energy(clip, 3000, 7000)


def test_wrong_shape_profile_falls_back_to_per_clip():
    clip = _tone(32_000, 440.0) + _noise(32_000, 0.02)
    bad_floor = np.ones((100, 1), dtype=np.float32)   # wrong STFT geometry
    out_bad = reduce_noise(clip, denoise_strength=0.6, noise_floor=bad_floor)
    out_none = reduce_noise(clip, denoise_strength=0.6)
    assert np.allclose(out_bad, out_none)             # same fallback path


# -- profile store (desktop layer) -------------------------------------------

def test_profile_round_trip_and_device_match(tmp_path):
    from localflow.desktop import noiseprofile

    path = tmp_path / "noise_profile.npz"
    floor = estimate_noise_floor(_noise(16_000, 0.02))
    noiseprofile.save_profile(floor, "Headset Microphone (Samsung USB", path)

    loaded = noiseprofile.load_profile(path)
    assert loaded is not None
    got, device, saved_at = loaded
    assert np.allclose(got, floor)
    assert device.startswith("Headset Microphone")
    assert saved_at > 0

    # prefix matching both ways (MME truncates names to 31 chars)
    assert noiseprofile.load_for_device(
        "Headset Microphone (Samsung USB Type C)", path) is not None
    assert noiseprofile.load_for_device("Headset Micro", path) is not None
    # a different mic must NOT get this profile
    assert noiseprofile.load_for_device("Intel Smart Sound Mic", path) is None


def test_profile_clear_and_missing(tmp_path):
    from localflow.desktop import noiseprofile

    path = tmp_path / "noise_profile.npz"
    assert noiseprofile.load_profile(path) is None
    assert noiseprofile.clear_profile(path) is False
    noiseprofile.save_profile(
        estimate_noise_floor(_noise(16_000, 0.02)), "mic", path)
    assert noiseprofile.clear_profile(path) is True
    assert noiseprofile.load_profile(path) is None


# -- vad_threshold plumbing ---------------------------------------------------

def test_config_vad_threshold_validation(tmp_path):
    from localflow.desktop import config

    p = tmp_path / "localflow.toml"
    p.write_text("vad_threshold = 0.65\n", encoding="utf-8")
    assert config.load_config(p).vad_threshold == 0.65
    p.write_text("vad_threshold = 0.05\n", encoding="utf-8")
    with pytest.raises(ValueError):
        config.load_config(p)


def test_apply_settings_dispatches_vad_threshold():
    from localflow.desktop import config
    from localflow.desktop.app import apply_settings

    cfg = config.Config()
    got: dict = {}
    restart = apply_settings({"vad_threshold": 0.62}, cfg,
                             {"vad": lambda v: got.setdefault("v", v)})
    assert restart == set()
    assert got["v"] == 0.62
    assert cfg.vad_threshold == 0.62
