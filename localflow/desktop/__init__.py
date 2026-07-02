"""localflow.desktop — the thin Windows client layer.

M2 ships only mic capture. Hotkey, clipboard injection and tray arrive in
M3/M4. The desktop layer may import localflow.core; never the reverse.
"""

from .capture import MAX_UTTERANCE_S, record_audio

__all__ = ["MAX_UTTERANCE_S", "record_audio"]
