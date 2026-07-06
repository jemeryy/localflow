"""Core tests. The STT tests need the exported model + this machine's GPU —
they are integration tests by nature (LocalFlow targets exactly one machine).

Run: .venv\\Scripts\\python.exe -m pytest tests -v
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

REPO = Path(__file__).resolve().parents[1]
MODEL_DIR = REPO / "models" / "whisper-turbo-ov"
CACHE_DIR = REPO / "models" / "ov_cache"
TEST_WAV = REPO / ".tmp" / "test_utterance.wav"

needs_model = pytest.mark.skipif(not MODEL_DIR.exists(), reason="exported model missing")
needs_wav = pytest.mark.skipif(not TEST_WAV.exists(), reason="test WAV missing")


def test_core_import_purity():
    """AT-7 seam: importing localflow.core must not drag in the desktop layer."""
    code = (
        "import sys; import localflow.core; "
        "bad = [m for m in ('sounddevice', 'pynput', 'pyperclip') if m in sys.modules]; "
        "bad += [m for m in sys.modules if m.startswith('localflow.desktop')]; "
        "sys.exit('leaked: ' + ', '.join(bad) if bad else 0)"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr or proc.stdout


def test_ensure_16k_mono_resamples_and_downmixes():
    from localflow.core import TARGET_RATE, ensure_16k_mono

    t = np.linspace(0, 1.0, 48_000, endpoint=False)
    stereo = np.stack([np.sin(2 * np.pi * 440 * t)] * 2, axis=1).astype(np.float32)
    out = ensure_16k_mono(stereo, 48_000)
    assert out.ndim == 1
    assert out.dtype == np.float32
    assert abs(len(out) - TARGET_RATE) <= 2


def test_ensure_16k_mono_scales_int16():
    from localflow.core import ensure_16k_mono

    loud = np.full(16_000, 16_384, dtype=np.int16)  # 0.5 full-scale
    out = ensure_16k_mono(loud, 16_000)
    assert 0.49 < float(out.max()) < 0.51


def test_vad_gates_silence():
    from localflow.core import VadGate

    assert VadGate().trim(np.zeros(2 * 16_000, dtype=np.float32)) is None


def test_normalize_peak_boosts_quiet_signal():
    from localflow.core.preprocess import normalize_peak

    t = np.linspace(0, 1.0, 16_000, endpoint=False)
    quiet = (0.1 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)  # ~-20 dBFS
    out = normalize_peak(quiet, target_peak=0.95)
    assert out.dtype == np.float32
    assert len(out) == len(quiet)
    assert 0.8 < float(np.max(np.abs(out))) <= 1.0  # brought up near full scale


def test_normalize_peak_leaves_silence_alone():
    from localflow.core.preprocess import normalize_peak

    out = normalize_peak(np.zeros(16_000, dtype=np.float32))
    assert float(np.max(np.abs(out))) == 0.0  # never amplify a silent buffer


def test_high_pass_attenuates_low_rumble_keeps_voice():
    from localflow.core.preprocess import high_pass

    t = np.linspace(0, 1.0, 16_000, endpoint=False)
    rms = lambda x: float(np.sqrt(np.mean(x * x)))
    rumble = np.sin(2 * np.pi * 30 * t).astype(np.float32)    # below speech band
    voice = np.sin(2 * np.pi * 1_000 * t).astype(np.float32)  # in band
    assert rms(high_pass(rumble)) < 0.3 * rms(rumble)
    assert rms(high_pass(voice)) > 0.8 * rms(voice)


def test_reduce_noise_pulls_down_noise_floor_and_keeps_length():
    from localflow.core.preprocess import reduce_noise

    rng = np.random.default_rng(1)
    n = 32_000
    t = np.linspace(0, 2.0, n, endpoint=False)
    noisy = (0.05 * rng.standard_normal(n)).astype(np.float32)
    noisy[n // 2:] += (0.4 * np.sin(2 * np.pi * 300 * t[n // 2:])).astype(np.float32)
    out = reduce_noise(noisy, denoise_strength=0.8)
    assert len(out) == n
    rms = lambda x: float(np.sqrt(np.mean(x * x)))
    assert rms(out[:n // 2]) < rms(noisy[:n // 2])  # noise-only half attenuated


def test_preprocessor_passthrough_when_disabled():
    from localflow.core import AudioPreprocessor

    pp = AudioPreprocessor(denoise=False, normalize=False)
    assert pp.active is False
    x = np.linspace(-0.3, 0.3, 8_000, dtype=np.float32)
    assert np.array_equal(pp.process(x), x)


def test_preprocessor_preserves_shape_and_handles_edges():
    from localflow.core import AudioPreprocessor

    pp = AudioPreprocessor()
    assert pp.active is True
    rng = np.random.default_rng(2)
    x = (0.1 * rng.standard_normal(24_000)).astype(np.float32)
    out = pp.process(x)
    assert out.dtype == np.float32 and len(out) == len(x)
    assert len(pp.process(np.zeros(0, dtype=np.float32))) == 0  # empty is fine
    silent = pp.process(np.zeros(16_000, dtype=np.float32))
    assert float(np.max(np.abs(silent))) < 1e-3  # silence not amplified to noise


@needs_wav
def test_vad_finds_speech_in_test_wav():
    import soundfile as sf

    from localflow.core import VadGate

    audio, rate = sf.read(TEST_WAV, dtype="float32")
    assert rate == 16_000
    trimmed = VadGate().trim(audio)
    assert trimmed is not None
    assert 16_000 < len(trimmed) <= len(audio)


@pytest.fixture(scope="module")
def engine():
    from localflow.core import SttEngine

    return SttEngine(str(MODEL_DIR), device="GPU", cache_dir=str(CACHE_DIR))


@needs_model
@needs_wav
def test_transcribe_audio_end_to_end_on_gpu(engine):
    import soundfile as sf

    from localflow.core import VadGate, transcribe_audio

    audio, _ = sf.read(TEST_WAV, dtype="float32")
    result = transcribe_audio(audio, engine, vad=VadGate())
    assert result.device == "GPU", "expected the iGPU on the target machine"
    assert "quick brown fox" in result.raw_text.lower()
    assert result.stt_ms < 2_000
    assert result.clean_text is None and result.cleanup_ms is None
    assert result.duration_s > 1.0


@needs_model
def test_silence_produces_empty_result(engine):
    from localflow.core import VadGate, transcribe_audio

    result = transcribe_audio(np.zeros(3 * 16_000, dtype=np.float32), engine, vad=VadGate())
    assert result.raw_text == ""
    assert result.stt_ms == 0 and result.duration_s == 0.0


def test_validate_cleanup_guardrails():
    """FR-9: cleanup may edit, never expand or blank the transcript."""
    from localflow.core.cleanup import validate_cleanup

    raw = "um so the meeting is uh on tuesday"
    assert validate_cleanup(raw, "The meeting is on Tuesday.") == \
        "The meeting is on Tuesday."
    # models love wrapping the answer in quotes — strip them
    assert validate_cleanup(raw, '"The meeting is on Tuesday."') == \
        "The meeting is on Tuesday."
    with pytest.raises(ValueError, match="empty"):
        validate_cleanup(raw, "   ")
    with pytest.raises(ValueError, match="long"):
        validate_cleanup("hi", "x" * 200)  # answered instead of edited
