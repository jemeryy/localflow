"""Read-aloud: speak the selected text or the clipboard with a local voice.

core/tts.py does text -> wav (pure). This desktop layer owns the parts core
must not: reading the OS clipboard, the copy-selection keystrokes, and audio
playback. Playback is stdlib winsound - a new async sound replaces the old one
(one voice at a time for free) and SND_PURGE stops it, so there is no ffplay
subprocess or pid-file kill like the interim personal-assistant script needed.

Trigger the hotkey (or tray "Narrate") once to speak; again to stop.
"""

from __future__ import annotations

import ctypes
import logging
import os
import tempfile
import threading
import time
import wave
import winsound

import pyperclip

from localflow.core.tts import TtsEngine, clean_for_speech

log = logging.getLogger("localflow.desktop.narrate")

# keybd_event constants for the copy-selection simulation (ported from the
# proven personal-assistant read_selection.py: on this machine, injecting
# Ctrl+C while the user still holds Ctrl+Alt from the hotkey arrives as
# Ctrl+Alt+C unless Alt is released first).
_VK_CONTROL, _VK_MENU, _VK_C = 0x11, 0x12, 0x43
_KEYUP = 0x2
_user32 = ctypes.windll.user32


def _wav_seconds(path: str) -> float:
    try:
        with wave.open(path, "rb") as w:
            rate = w.getframerate()
            return w.getnframes() / rate if rate else 0.0
    except (OSError, wave.Error):
        return 0.0


class Narrator:
    """Speak selection/clipboard aloud; toggle to stop. Thread-safe entry
    points (called from the hotkey thread and the tray thread)."""

    def __init__(self, model_path: str, length_scale: float = 0.8,
                 volume: float = 1.0, max_chars: int = 1500):
        self._model_path = model_path
        self._length_scale = float(length_scale)
        self._volume = float(volume)
        self._max_chars = max_chars
        self._engine: TtsEngine | None = None    # lazy: load the voice on first use
        self._engine_lock = threading.Lock()
        self._gen = 0                 # bump cancels an in-flight/playing narration
        self._speaking = False
        self._wav = os.path.join(tempfile.gettempdir(), "localflow_tts.wav")

    def set_length_scale(self, length_scale: float) -> None:
        """Live speed change from Settings (lower = faster). Applies to the
        next narration; no model reload."""
        self._length_scale = float(length_scale)
        if self._engine is not None:
            self._engine.length_scale = self._length_scale

    def set_volume(self, volume: float) -> None:
        """Live volume change from Settings (1.0 = as synthesized). Applies to
        the next narration; no model reload."""
        self._volume = float(volume)
        if self._engine is not None:
            self._engine.volume = self._volume

    @property
    def speaking(self) -> bool:
        return self._speaking

    def stop(self) -> None:
        self._gen += 1                # supersede any synth thread mid-flight
        self._speaking = False
        try:
            winsound.PlaySound(None, winsound.SND_PURGE)
        except Exception:
            log.debug("stop playback failed", exc_info=True)

    def toggle_selection(self) -> None:
        """Hotkey: speaking -> stop; else copy the current selection and speak."""
        if self._speaking:
            self.stop()
            return
        self._start(self._copy_selection())

    def toggle_clipboard(self) -> None:
        """Tray 'Narrate clipboard': speaking -> stop; else speak the clipboard.

        Focus moves to the tray when its menu opens, so simulating a copy
        would grab nothing - the tray path reads whatever is already copied.
        """
        if self._speaking:
            self.stop()
            return
        self._start(self._read_clipboard())

    # -- internals ---------------------------------------------------------

    def _ensure_engine(self) -> TtsEngine:
        if self._engine is None:
            with self._engine_lock:
                if self._engine is None:
                    self._engine = TtsEngine(self._model_path,
                                             self._length_scale, self._volume)
        return self._engine

    def _read_clipboard(self) -> str:
        try:
            return pyperclip.paste()      # "" for non-text clipboard content
        except Exception:
            log.debug("clipboard read failed", exc_info=True)
            return ""

    def _copy_selection(self) -> str:
        saved = self._read_clipboard()
        # Clear the clipboard first as a sentinel: if NOTHING is selected,
        # Ctrl+C copies nothing and it stays empty, so we return "" (no
        # narration) instead of re-speaking whatever was copied before.
        try:
            pyperclip.copy("")
        except Exception:
            log.debug("clipboard clear failed", exc_info=True)
        _user32.keybd_event(_VK_MENU, 0, _KEYUP, 0)   # drop Alt from the hotkey
        time.sleep(0.05)
        _user32.keybd_event(_VK_CONTROL, 0, 0, 0)
        _user32.keybd_event(_VK_C, 0, 0, 0)
        time.sleep(0.02)
        _user32.keybd_event(_VK_C, 0, _KEYUP, 0)
        _user32.keybd_event(_VK_CONTROL, 0, _KEYUP, 0)
        time.sleep(0.15)                               # let the app write it
        text = self._read_clipboard()
        # Restore the user's clipboard (we already captured `text`, so this
        # can't clobber what we're about to speak).
        if saved != text:
            try:
                pyperclip.copy(saved)
            except Exception:
                log.debug("clipboard restore failed", exc_info=True)
        return text

    def _start(self, raw: str) -> None:
        text = clean_for_speech(raw or "", self._max_chars)
        if not text:
            log.info("nothing to narrate")
            return
        self.stop()                   # cancel any prior; bumps _gen
        self._speaking = True
        gen = self._gen
        threading.Thread(target=self._run, args=(text, gen), daemon=True,
                         name="localflow-tts").start()

    def _run(self, text: str, gen: int) -> None:
        # Synthesis allocates a lot (onnxruntime), which can trigger a cyclic
        # GC on THIS thread; COM-initialize it so finalizing a stray pycaw/
        # comtypes object here can't access-violate (same guard as the overlay
        # and worker threads). Short-lived thread; COM cleans up on exit.
        try:
            import comtypes
            comtypes.CoInitialize()
        except Exception:
            log.debug("narrate CoInitialize failed", exc_info=True)
        try:
            engine = self._ensure_engine()
            engine.synthesize_wav(text, self._wav)
            if gen != self._gen:      # stopped/superseded during synthesis
                return
            winsound.PlaySound(self._wav,
                               winsound.SND_FILENAME | winsound.SND_ASYNC)
            # SND_ASYNC has no "done" callback, so sleep out the clip's length
            # to clear _speaking: without this, a natural finish would leave
            # the flag set and the next press would just stop silence.
            end = time.monotonic() + _wav_seconds(self._wav)
            while time.monotonic() < end:
                if gen != self._gen:
                    return
                time.sleep(0.05)
            if gen == self._gen:
                self._speaking = False
        except Exception:
            log.exception("narration failed")
            if gen == self._gen:
                self._speaking = False
