"""Text injection (FR-10/11/12): clipboard paste with save/restore, or typed keys.

Live typing (M13) is TARGETED: the control that had the keyboard focus when
the dictation started is captured (`focused_control`) and every revision is
POSTED straight to that window with window messages, not sent to whatever has
focus now. That is what lets the stream keep landing in the original field
while the user works somewhere else (Jeremy 27 Jul).

UIPI limit (FR-11): injecting into an elevated (run-as-administrator) window
silently fails — Windows blocks synthetic input across integrity levels and
reports no error. Documented, not fought, in v1.
"""

from __future__ import annotations

import ctypes
import logging
import re
import time
from ctypes import wintypes

import pyperclip
from pynput.keyboard import Controller, Key

log = logging.getLogger("localflow.desktop.inject")

_user32 = ctypes.windll.user32
# Declared argtypes: HWNDs and LPARAMs are pointer-sized — the default c_int
# conversion would truncate them on 64-bit Python.
_user32.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT,
                                 wintypes.WPARAM, wintypes.LPARAM)
_user32.IsWindow.argtypes = (wintypes.HWND,)
_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.GetWindowThreadProcessId.argtypes = (
    wintypes.HWND, ctypes.POINTER(wintypes.DWORD))


class _GUITHREADINFO(ctypes.Structure):
    _fields_ = (("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("hwndActive", wintypes.HWND), ("hwndFocus", wintypes.HWND),
                ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
                ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND),
                ("rcCaret", wintypes.RECT))


def focused_control() -> int | None:
    """HWND of the control that owns the keyboard focus right now — the
    dictation's target, captured at start. Falls back to the foreground
    window when its focused child can't be read; None when there is no
    foreground window at all (then injection stays focus-follows, as before).
    """
    fg = _user32.GetForegroundWindow()
    if not fg:
        return None
    tid = _user32.GetWindowThreadProcessId(fg, None)
    info = _GUITHREADINFO(cbSize=ctypes.sizeof(_GUITHREADINFO))
    if _user32.GetGUIThreadInfo(tid, ctypes.byref(info)) and info.hwndFocus:
        return info.hwndFocus
    return fg


_WM_KEYDOWN, _WM_KEYUP, _WM_CHAR = 0x0100, 0x0101, 0x0102
_VK_BACK = 0x08
_BACK_DOWN_LP = 1 | (0x0E << 16)             # repeat 1, backspace scancode
_BACK_UP_LP = _BACK_DOWN_LP | (0x3 << 30)    # previous-state + transition

# Window classes whose text surface deletes on the KEY-DOWN itself and
# ignores a 0x08 char: Chromium (Chrome/Edge/VS Code/Electron) and Gecko.
# ponytail: class sniff — extend the tuple when some app deletes on neither.
_KEYDOWN_DELETE_CLASSES = ("Chrome_RenderWidgetHostHWND", "MozillaWindowClass")


def _keydown_deletes(hwnd: int) -> bool:
    buf = ctypes.create_unicode_buffer(64)
    _user32.GetClassNameW(hwnd, buf, 64)
    return any(c in buf.value for c in _KEYDOWN_DELETE_CLASSES)


def _post_backspaces(hwnd: int, n: int) -> None:
    """Delete `n` chars in the target. POSTED (not sent) so a hung target can
    never stall the dictation. Two styles, because ordering is sacred:
    posting a raw key-down would make the target's own TranslateMessage
    enqueue the deleting WM_CHAR at the BACK of its queue — behind the
    replacement text we already posted — so the deletes landed after the
    retype and every revision garbled the line (live 27 Jul). Classic edit
    controls therefore get the WM_CHAR 0x08 they would have been handed
    anyway; Chromium/Gecko surfaces delete on the key-down itself (and
    ignore the 0x08 char), so they get the key pair. Both stay FIFO with
    the WM_CHAR text that follows."""
    if _keydown_deletes(hwnd):
        for _ in range(n):
            _user32.PostMessageW(hwnd, _WM_KEYDOWN, _VK_BACK, _BACK_DOWN_LP)
            _user32.PostMessageW(hwnd, _WM_KEYUP, _VK_BACK, _BACK_UP_LP)
    else:
        for _ in range(n):
            _user32.PostMessageW(hwnd, _WM_CHAR, _VK_BACK, 1)


def _post_text(hwnd: int, text: str) -> None:
    data = text.encode("utf-16-le")          # WM_CHAR takes UTF-16 units
    for i in range(0, len(data), 2):
        _user32.PostMessageW(hwnd, _WM_CHAR,
                             int.from_bytes(data[i:i + 2], "little"), 1)


def _target_alive(target: int | None, what: str) -> bool:
    """A captured target that has since been closed drops the patch — falling
    back to SendInput would spill the words into whatever the user is doing
    now, which is worse than losing a revision."""
    if target is None or _user32.IsWindow(target):
        return True
    log.warning("target window is gone - %s dropped", what)
    return False

# Modifier VKs -> the key that releases them: shift, ctrl, alt, lwin, rwin.
_MODIFIER_KEYS = ((0x10, Key.shift), (0x11, Key.ctrl), (0x12, Key.alt),
                  (0x5B, Key.cmd_l), (0x5C, Key.cmd_r))
_MODIFIER_VKS = tuple(vk for vk, _ in _MODIFIER_KEYS)

# FR-10(d): the target app reads the clipboard asynchronously after Ctrl+V
# arrives — restoring too soon races the paste and the OLD clipboard content
# gets pasted instead of the transcript (observed live at 120ms: a busy app
# pasted a stale shell command). There is NO OS signal for "paste consumed",
# so the delay is the only lever: 500ms default, tunable via
# restore_delay_ms in localflow.toml.
PASTE_SETTLE_S = 0.5
# Small beat between setting the clipboard and sending Ctrl+V so slow apps
# never see the pre-transcript clipboard.
CLIPBOARD_SET_S = 0.03

_keyboard = Controller()


def inject_text(text: str, mode: str = "paste",
                restore_delay_s: float = PASTE_SETTLE_S) -> bool:
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
        _paste(text, restore_delay_s)
    else:
        _keyboard.type(text)
    return True


# Between-word gap for a live revision's retype: enough that words visibly
# stream in one at a time (the Google feel), small enough to never lag the
# next decode. Synchronous on the caller's thread — when the dictation ends
# nothing is queued anywhere, so typing stops the instant close() lands.
LIVE_WORD_GAP_S = 0.08
_WORD_PIECE = re.compile(r"\s*\S+")


def live_patch(n_backspaces: int, text: str, target: int | None = None) -> None:
    """Apply one live revision (M13): backspace the divergent tail, then type
    the new words one by one. `target` is the HWND captured at dictation
    start — revisions are posted straight to it, so they keep landing in the
    field the dictation started in even after the user switches window/tab.
    None falls back to typing into the focus (capture failed). NO modifier
    wait — the hotkey's Alt stays physically down for a whole hold, and
    waiting would stall every revision for its full timeout. Safe: M11's
    suppression eats the physical keys, clear_modifiers told the app every
    modifier is up, and injected events pass the suppression hook
    (LLKHF_INJECTED)."""
    if not _target_alive(target, "live revision"):
        return
    if target is not None:
        _post_backspaces(target, n_backspaces)
    else:
        for _ in range(n_backspaces):
            _keyboard.tap(Key.backspace)
    if not text:
        return
    pieces = _WORD_PIECE.findall(text)
    if "".join(pieces) != text:
        pieces = [text]          # odd spacing: type it verbatim, unpaced
    for i, piece in enumerate(pieces):
        if i:
            time.sleep(LIVE_WORD_GAP_S)
        if target is not None:
            _post_text(target, piece)
        else:
            _keyboard.type(piece)


def patch_text(n_backspaces: int, retype: str, target: int | None = None) -> None:
    """M13 final patch: delete the divergent suffix, type the correction.
    With a `target` HWND the patch is posted straight to the dictation's
    original field (no modifier wait needed — posted messages never consult
    the physical keyboard state, and the target thread never saw the
    hotkey's Alt go down: the suppression hook ate it).

    In the SendInput fallback, backspace is a real virtual key (unlike
    unicode typing), so the hotkey's modifiers have to be out of the way
    first — Alt+Backspace is "undo" in plenty of apps, not a delete. By this
    point the dictation has ended, so the wait returns almost immediately.
    """
    if not n_backspaces and not retype:
        return
    if target is not None:
        if not _target_alive(target, "final patch"):
            return
        _post_backspaces(target, n_backspaces)
        if retype:
            _post_text(target, retype)
        return
    _wait_modifiers_released()
    for _ in range(n_backspaces):
        _keyboard.tap(Key.backspace)
    if retype:
        _keyboard.type(retype)


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


def clear_modifiers() -> None:
    """Tell the focused app that every modifier is up (M11).

    The Alt of alt+` stays physically down for the whole of a held dictation,
    and anything typed into the app while Windows thinks Alt is down arrives
    as Alt+key — menu accelerators and beeps, not text. One synthetic release
    each fixes the app-visible state; the keyboard suppression that runs
    alongside a dictation stops the OS auto-repeat from putting it back.
    """
    get_state = ctypes.windll.user32.GetAsyncKeyState
    for vk, key in _MODIFIER_KEYS:
        if get_state(vk) & 0x8000:
            try:
                _keyboard.release(key)
            except Exception:
                log.debug("could not release %r", key, exc_info=True)


def _send_ctrl_v() -> None:
    with _keyboard.pressed(Key.ctrl):
        _keyboard.press("v")
        _keyboard.release("v")


def _paste(text: str, restore_delay_s: float) -> None:
    # FR-10: save clipboard text -> set transcript -> Ctrl+V -> wait -> restore.
    # Only text survives the round-trip: pyperclip reads images/files as "",
    # so non-text clipboard content is lost. Accepted for v1 (G5 says text).
    saved = pyperclip.paste()
    pyperclip.copy(text)
    time.sleep(CLIPBOARD_SET_S)
    _send_ctrl_v()
    time.sleep(restore_delay_s)
    # If the user copied something new during the settle window, restoring
    # would clobber it — leave their copy alone (the transcript case: the
    # clipboard still holds our text, so restore is safe *if* the app has
    # had time to paste; that's what restore_delay_s buys).
    if pyperclip.paste() == text:
        pyperclip.copy(saved)
    else:
        log.info("clipboard changed during paste settle — leaving it as-is")
