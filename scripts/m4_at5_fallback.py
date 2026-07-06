"""AT-5 harness (M4): force a bad device string and prove the app-level
behaviour headlessly — SttEngine falls back to CPU with a warning log AND
still transcribes real speech. The live half of AT-5 (normal launch logs
device=GPU) is just reading the `localflow` startup line.

Run: .venv\\Scripts\\python.exe scripts/m4_at5_fallback.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import soundfile as sf

from localflow.core import SttEngine, VadGate, ensure_16k_mono, transcribe_audio

WAV = Path(".tmp/test_utterance.wav")  # known-good speech sample from M1/M2


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)-7s %(name)s: %(message)s")
    if not WAV.is_file():
        print(f"missing {WAV} - record one with scripts/m2_capture_vad.py")
        return 1

    engine = SttEngine("models/whisper-turbo-ov", device="NONSENSE",
                       fallback_device="CPU", cache_dir="models/ov_cache")
    if engine.device != "CPU":
        print(f"FAIL: expected fallback to CPU, engine ran on {engine.device}")
        return 1

    audio, rate = sf.read(WAV, dtype="float32")
    result = transcribe_audio(ensure_16k_mono(audio, rate), engine, vad=VadGate())
    if not result.raw_text:
        print("FAIL: fallback engine produced no text")
        return 1

    print(f"\nAT-5 (headless half) PASS: requested NONSENSE -> ran on "
          f"{result.device}, stt {result.stt_ms}ms\n  -> {result.raw_text!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
