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
