"""Text injection (FR-10/11/12): clipboard paste with save/restore, or typed keys.

UIPI limit (FR-11): injecting into an elevated (run-as-administrator) window
silently fails — Windows blocks synthetic input across integrity levels and
reports no error. Documented, not fought, in v1.
"""

from __future__ import annotations

import ctypes
import logging
import time

import pyperclip
from pynput.keyboard import Controller, Key

log = logging.getLogger("localflow.desktop.inject")

# Modifier VKs for _wait_modifiers_released: shift, ctrl, alt, lwin, rwin.
_MODIFIER_VKS = (0x10, 0x11, 0x12, 0x5B, 0x5C)

# FR-10(d): the target app reads the clipboard asynchronously after Ctrl+V
# arrives — restoring sooner than ~120ms races the paste and corrupts it.
PASTE_SETTLE_S = 0.12
# Small beat between setting the clipboard and sending Ctrl+V so slow apps
# never see the pre-transcript clipboard.
CLIPBOARD_SET_S = 0.03

_keyboard = Controller()


def inject_text(text: str, mode: str = "paste") -> bool:
    """Insert `text` into whatever app has focus. Returns True if anything was sent.

    Empty text means the silence gate fired: inject NOTHING (FR-4 / AT-3).
    `mode` is FR-12's inject_mode: "paste" (default) or "type" (synthetic
    keystrokes, slower, for apps that block paste).
    """
    if mode not in ("paste", "type"):
        raise ValueError(f"unknown inject_mode {mode!r} (want 'paste' or 'type')")
    if not text:
        log.info("empty transcript — injecting nothing (FR-4)")
        return False
    _wait_modifiers_released()
    if mode == "paste":
        _paste(text)
    else:
        _keyboard.type(text)
    return True


def _wait_modifiers_released(timeout_s: float = 1.5) -> None:
    """Block until the user's physical modifier keys are all up.

    After a fast transcription the user may still be holding part of the
    hotkey (e.g. Alt of alt+`) — sending Ctrl+V then would deliver
    Ctrl+Alt+V and the paste would silently misfire. Times out rather than
    hang if a key is genuinely stuck."""
    get_state = ctypes.windll.user32.GetAsyncKeyState
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if not any(get_state(vk) & 0x8000 for vk in _MODIFIER_VKS):
            return
        time.sleep(0.01)
    log.warning("modifier still held after %.1fs — injecting anyway", timeout_s)


def _paste(text: str) -> None:
    # FR-10: save clipboard text -> set transcript -> Ctrl+V -> wait -> restore.
    # Only text survives the round-trip: pyperclip reads images/files as "",
    # so non-text clipboard content is lost. Accepted for v1 (G5 says text).
    saved = pyperclip.paste()
    pyperclip.copy(text)
    time.sleep(CLIPBOARD_SET_S)
    with _keyboard.pressed(Key.ctrl):
        _keyboard.press("v")
        _keyboard.release("v")
    time.sleep(PASTE_SETTLE_S)
    pyperclip.copy(saved)
