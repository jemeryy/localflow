"""localflow.core — pure Python: audio in, text out (PRD §4).

Purity rule (PRD §3, AT-7): this package must never import the desktop layer,
touch the clipboard or keyboard, or make network calls. Models are loaded from
local files only. This is the seam that lets FARA and future phases call the
exact same transcription code.
"""

from .audio import TARGET_RATE, ensure_16k_mono
from .cleanup import CleanupEngine
from .hallucination import collapse_repeats, strip_hallucinations
from .live import commit_prefix, patch_plan
from .pipeline import TranscriptResult, transcribe_audio
from .preprocess import (
    AudioPreprocessor,
    estimate_noise_floor,
    high_pass,
    recommend_tuning,
)
from .segment import find_cut_point
from .stt import SttEngine, to_language_token
from .tts import TtsEngine, clean_for_speech
from .vad import VadGate

__all__ = [
    "TARGET_RATE",
    "AudioPreprocessor",
    "CleanupEngine",
    "SttEngine",
    "TranscriptResult",
    "TtsEngine",
    "VadGate",
    "clean_for_speech",
    "collapse_repeats",
    "commit_prefix",
    "ensure_16k_mono",
    "estimate_noise_floor",
    "find_cut_point",
    "high_pass",
    "patch_plan",
    "recommend_tuning",
    "strip_hallucinations",
    "to_language_token",
    "transcribe_audio",
]
