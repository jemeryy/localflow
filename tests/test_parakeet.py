"""ParakeetEngine contract tests (docs/parakeet-spike.md). The e2e decode
runs only when the model dir is present (it is on Jeremy's machine); the
contract must match SttEngine's exactly - the desktop layer swaps engines
on config alone.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

onnx_asr = pytest.importorskip("onnx_asr")

MODEL_DIR = Path(__file__).resolve().parents[1] / "models" / \
    "parakeet-tdt-0.6b-v2-onnx"

pytestmark = pytest.mark.skipif(not MODEL_DIR.is_dir(),
                                reason="parakeet model not downloaded")


@pytest.fixture(scope="module")
def engine():
    from localflow.core.parakeet import ParakeetEngine
    return ParakeetEngine(str(MODEL_DIR))


def test_contract_shape_on_empty_audio(engine):
    text, meta = engine.transcribe(np.zeros(0, dtype=np.float32))
    assert text == ""
    assert meta == {"stt_ms": 0, "device": "CPU",
                    "model": "parakeet-tdt-0.6b-v2-onnx", "chunks": None}


def test_silence_decodes_to_nothing(engine):
    # The whole point of the transducer: silence is silence, not "Thank you."
    text, meta = engine.transcribe(np.zeros(16000 * 2, dtype=np.float32),
                                   timestamps=True, rebuild_on_failure=False)
    assert text == ""
    assert meta["chunks"] is None and meta["stt_ms"] >= 0


def test_real_speech_decodes(engine):
    import soundfile as sf
    wav = Path(__file__).resolve().parents[1] / ".tmp" / "test_utterance.wav"
    if not wav.exists():
        pytest.skip("test utterance wav not present")
    audio, rate = sf.read(wav, dtype="float32")
    from localflow.core import ensure_16k_mono
    text, meta = engine.transcribe(ensure_16k_mono(audio, rate))
    assert "quick brown fox" in text.lower()
    assert meta["device"] == "CPU"
