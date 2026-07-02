"""localflow.core — pure Python: audio in, text out (PRD §4).

Purity rule (PRD §3, AT-7): this package must never import the desktop layer,
touch the clipboard or keyboard, or make network calls. Models are loaded from
local files only. This is the seam that lets FARA and future phases call the
exact same transcription code.
"""

from .audio import TARGET_RATE, ensure_16k_mono
from .cleanup import CleanupEngine
from .pipeline import TranscriptResult, transcribe_audio
from .stt import SttEngine
from .vad import VadGate

__all__ = [
    "TARGET_RATE",
    "CleanupEngine",
    "SttEngine",
    "TranscriptResult",
    "VadGate",
    "ensure_16k_mono",
    "transcribe_audio",
]
