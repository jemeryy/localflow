"""Text-to-speech seam: text in, spoken audio out (mirror of the STT seam).

Fully local (Piper neural voice, ONNX). Like the rest of core/ this stays
pure: no clipboard, no keyboard, no playback, no network. The desktop layer
(desktop/narrate.py) owns reading the OS clipboard and playing the wav.

Phase 2/3 (FARA, cloud) can reuse this exactly as they reuse the STT core.
"""

from __future__ import annotations

import logging
import re
import wave

log = logging.getLogger("localflow.core.tts")

# Whisper/Claude output is markdown; speaking the raw markup ("hash hash",
# "star star") is unlistenable. Strip it to plain speakable text. Cap length
# so a whole document pasted by mistake can't tie up synthesis for minutes.
MAX_CHARS = 1500


def clean_for_speech(text: str, max_chars: int = MAX_CHARS) -> str:
    """Selected/markdown text -> plain speakable text, capped.

    Pure and deterministic. Beyond stripping markdown, this fixes the pacing
    the raw synth gets wrong: line-break hyphenation from PDFs/web, paragraph
    breaks (which must stay as a pause, not collapse into a run-on), and leading
    list markers (so a bullet isn't read as a word).
    """
    text = re.sub(r"```.*?```", " (code block skipped) ", text, flags=re.S)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)   # links -> label
    # PDF/web line-break hyphenation: "inter-\nnational" -> "international".
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)
    text = re.sub(r"[*_#>|~]", " ", text)                  # emphasis / rules
    # Honour the writer's spacing as pauses: every line break (a new line, a
    # list point, a paragraph) ends its line with sentence punctuation so the
    # voice pauses there instead of running straight on.
    out = ""
    for line in text.split("\n"):
        # Leading bullet/number markers become nothing (a "-" or "1." read
        # aloud is noise): "- item" / "1. item" / "• item" -> "item".
        line = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s+", "", line)
        line = re.sub(r"[ \t]+", " ", line).strip()
        if not line:
            continue
        if line[-1] not in ".!?:;,":
            line += "."
        out += line + " "
    # Pause before a bracketed aside: "text (note)" -> "text, (note)".
    out = re.sub(r"(\S)\s*\(", r"\1, (", out)
    text = re.sub(r"\s+", " ", out).strip()
    return text[:max_chars]


class TtsEngine:
    """Loads a Piper voice once; synthesizes text to a WAV file on disk.

    length_scale is speech speed: lower is faster (0.8 is ~25% faster than
    the 1.0 baseline). It is a plain attribute so the desktop layer can
    live-update it from the Settings slider without reloading the model.
    """

    def __init__(self, model_path: str, length_scale: float = 0.8,
                 volume: float = 1.0):
        from piper import PiperVoice

        log.info("loading Piper voice %s", model_path)
        # config_path=None -> Piper reads "<model_path>.json" beside the onnx.
        self.voice = PiperVoice.load(model_path)
        self.length_scale = float(length_scale)
        self.volume = float(volume)     # multiplier: 1.0 = as synthesized

    def synthesize_wav(self, text: str, wav_path: str) -> None:
        from piper import SynthesisConfig

        cfg = SynthesisConfig(length_scale=self.length_scale,
                              volume=self.volume)
        with wave.open(wav_path, "wb") as wav_file:
            self.voice.synthesize_wav(text, wav_file, syn_config=cfg)
