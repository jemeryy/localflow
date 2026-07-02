"""localflow.desktop — the thin Windows client layer.

Mic capture (M2), push-to-talk + injection (M3). Tray arrives in M4.
The desktop layer may import localflow.core; never the reverse.
The app entry point lives in localflow.desktop.app (imported on demand so
`import localflow.desktop` doesn't start listener machinery).
"""

from .capture import MAX_UTTERANCE_S, StreamRecorder, record_audio
from .inject import inject_text

__all__ = ["MAX_UTTERANCE_S", "StreamRecorder", "inject_text", "record_audio"]
