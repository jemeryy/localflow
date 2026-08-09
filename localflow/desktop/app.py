"""LocalFlow push-to-talk app (M4): hold the hotkey, speak, release, paste.

Run: `localflow` (console script) or `python -m localflow.desktop.app`.
Settings come from localflow.toml (FR-13); CLI flags override. By default a
tray icon runs (FR-14) — grey idle, red recording, amber transcribing (FR-2);
quit from its menu. `--no-tray` keeps the M3 console-only mode (Ctrl+C quits).

Threading: pynput's listener callbacks must return fast, and the sounddevice
callback runs on the PortAudio thread — so key-up only enqueues the captured
audio, and a single worker thread does resample -> transcribe -> inject.
pystray needs the MAIN thread for its Win32 message loop, so tray.run()
lives there and everything else is on its own thread.
"""

from __future__ import annotations

import argparse
import ctypes
import gc
import logging
import os
import queue
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from pynput import keyboard

from localflow.core import (
    AudioPreprocessor,
    SttEngine,
    VadGate,
    collapse_repeats,
    ensure_16k_mono,
    estimate_noise_floor,
    high_pass,
    recommend_tuning,
    to_language_token,
    transcribe_audio,
)
from localflow.core.dictionary import apply_replacements, mine_substitutions
from localflow.core.numbers import spelled_to_digits
from localflow.core.live import patch_plan
from localflow.desktop import config
from localflow.desktop.capture import StreamRecorder
from localflow.desktop.inject import (
    clear_modifiers,
    focused_control,
    inject_text,
    live_patch,
    patch_text,
    window_class,
)

log = logging.getLogger("localflow.desktop.app")


@dataclass
class _Segment:
    """One unit of work on the worker queue (Tier 1). Mid-dictation segments
    (final=False) are transcribed raw and their text accumulated; the final
    one (produced on key-up) flushes the accumulation — join, cleanup,
    dictionary, inject, history — exactly once. dictation_id groups the
    segments of one hold together; t_release is stamped only on the final."""

    audio: np.ndarray
    rate: int
    dictation_id: int
    final: bool
    t_release: float | None = None
    cancelled: bool = False   # Esc (M11): flush the accumulation, paste nothing


def _process_age_s() -> float | None:
    """Seconds since this process was created (Windows). Anchors the M6
    startup breakdown: everything before this module finished importing is
    interpreter + imports (numpy/openvino load via the imports above)."""
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        created, exited, kern, user = (wintypes.FILETIME() for _ in range(4))
        # GetCurrentProcess() through argtype-less ctypes truncates the -1
        # pseudo-handle on 64-bit Python — build the HANDLE explicitly.
        if not k32.GetProcessTimes(wintypes.HANDLE(-1),
                                   ctypes.byref(created), ctypes.byref(exited),
                                   ctypes.byref(kern), ctypes.byref(user)):
            return None
        now = wintypes.FILETIME()
        k32.GetSystemTimeAsFileTime(ctypes.byref(now))
        t0 = (created.dwHighDateTime << 32) | created.dwLowDateTime
        t1 = (now.dwHighDateTime << 32) | now.dwLowDateTime
        return max(0.0, (t1 - t0) / 1e7)  # FILETIME ticks are 100ns
    except Exception:
        return None


_IMPORT_AGE_S = _process_age_s()   # launch -> end of this module's imports
_T_MODULE = time.perf_counter()    # perf-counter anchor for the same moment


def to_pynput_combo(combo: str) -> str:
    """'ctrl+space' -> '<ctrl>+<space>' (pynput HotKey.parse format).

    Multi-character parts are named keys and get angle brackets; single
    characters pass through: 'ctrl+shift+d' -> '<ctrl>+<shift>+d'.
    """
    parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
    if not parts:
        raise ValueError(f"empty hotkey combo {combo!r}")
    return "+".join(f"<{p}>" if len(p) > 1 else p for p in parts)


TAP_MAX_S = 0.3   # key-up within this of key-down = a tap, not a hold

# Low-level keyboard hook constants (M11 suppression).
_VK_ESCAPE = 0x1B
_PRESS_MSGS = (0x0100, 0x0104)      # WM_KEYDOWN, WM_SYSKEYDOWN
_RELEASE_MSGS = (0x0101, 0x0105)    # WM_KEYUP, WM_SYSKEYUP
# LLKHF_INJECTED | LLKHF_LOWER_IL_INJECTED — a synthetic key, i.e. our own
# typing or modifier clearing. Physical keys never set either bit.
_INJECTED_FLAGS = 0x10 | 0x02
# char -> vk for the hotkey's character key. Declared argtypes: without them
# ctypes passes a str as a pointer and the call fails.
_vk_key_scan = ctypes.windll.user32.VkKeyScanW
_vk_key_scan.argtypes = (ctypes.c_wchar,)
_vk_key_scan.restype = ctypes.c_short


class TapHoldGate:
    """Pure tap-vs-hold state machine for the dictation hotkey (M11).

    Feed it key-downs, key-ups, pill clicks and Esc; it answers with the
    action to take. A TAP (down and up inside `tap_max_s`) latches recording
    on until the next tap, a pill click or Esc; a longer press is the v1.1
    hold and ends on release. No OS, no threads, no clock of its own — the
    caller passes the timestamp, so this is unit-testable.
    """

    START, STOP, CANCEL, NONE = "start", "stop", "cancel", "none"

    def __init__(self, tap_max_s: float = TAP_MAX_S):
        self.tap_max_s = tap_max_s
        self._down_at: float | None = None   # set while the hotkey is held
        self._latched = False                # tapped on; ends on the next tap
        self._swallow_up = False             # the press that ended a dictation

    @property
    def active(self) -> bool:
        return self._down_at is not None or self._latched

    def key_down(self, now: float) -> str:
        if self._latched:
            # Second tap: end it here rather than on the release, so a long
            # second press doesn't look like a fresh hold.
            self._latched = False
            self._swallow_up = True
            return self.STOP
        if self._down_at is not None:
            return self.NONE                 # OS auto-repeat while held
        self._down_at = now
        return self.START

    def key_up(self, now: float) -> str:
        if self._swallow_up:
            self._swallow_up = False
            self._down_at = None
            return self.NONE
        if self._down_at is None:
            return self.NONE
        held, self._down_at = now - self._down_at, None
        if held < self.tap_max_s:
            self._latched = True             # tap: keep recording
            return self.NONE
        return self.STOP

    def click(self) -> str:
        """Pill click: a plain toggle, no key to wait on."""
        if not self.active:
            self._latched = True
            return self.START
        return self._end(self.STOP)

    def cancel(self) -> str:
        """Esc, or a start the app refused (mic test owns the recorder)."""
        return self._end(self.CANCEL) if self.active else self.NONE

    def _end(self, action: str) -> str:
        if self._down_at is not None:
            self._swallow_up = True          # the key is still physically down
        self._down_at = None
        self._latched = False
        return action


class PushToTalkListener:
    """The dictation hotkey (FR-1). on_start fires when the full combo is
    down, on_stop when any key of it is released — the app's TapHoldGate
    turns that into hold-to-talk or tap-to-toggle (M11).

    While `suppress` is set (a dictation is live) this also guards the
    dictation's TARGET field: keys that would land in it are swallowed so
    the user can't corrupt the text being streamed in, while the keyboard
    stays alive everywhere else (Jeremy 27 Jul). `target` is a callable
    returning that field's HWND; without one (tests, embedders, capture
    failed) the whole keyboard is suppressed — the old M11 rule, which is
    also the safe one when injection follows the focus. The hotkey is
    handled here and swallowed wherever the focus is; Esc cancels only from
    inside the target. The hook dies with the process, so a crash can never
    leave the keyboard dead."""

    def __init__(self, combo: str, on_start, on_stop,
                 suppress: threading.Event | None = None, on_cancel=None,
                 target=None):
        self._combo = set(keyboard.HotKey.parse(to_pynput_combo(combo)))
        self._on_start = on_start
        self._on_stop = on_stop
        self._on_cancel = on_cancel
        self._suppress = suppress
        self._target = target
        self._pressed: set = set()
        self._active = False
        self._listener = keyboard.Listener(
            on_press=self._handle_press, on_release=self._handle_release,
            win32_event_filter=self._win32_filter,
        )
        self._vk_keys = self._combo_vk_map()

    def _combo_vk_map(self) -> dict:
        """Raw virtual-key code -> the combo key it stands for. The low-level
        hook reports vks (left and right Alt are different codes), so this is
        how the filter recognises the hotkey without pynput's key objects.
        The values go back through _handle_press/_handle_release, which
        canonicalise exactly as the normal on_press path does."""
        canonical = self._listener.canonical
        vks: dict = {}
        for key in keyboard.Key:              # named keys, incl. alt_l/alt_r
            vk = key.value.vk
            if vk is not None and canonical(key) in self._combo:
                vks[vk] = key
        for key in self._combo:               # character keys, e.g. `
            char = getattr(key, "char", None)
            if char:
                res = _vk_key_scan(char)
                if res != -1:
                    vks[res & 0xFF] = key     # low byte is the vk
        return vks

    def _win32_filter(self, msg, data):
        """Low-level keyboard hook, called for EVERY key before the focused
        app sees it (M11). Transparent when idle. While `suppress` is set the
        keyboard is dead in the dictation's TARGET field only: the hotkey is
        handled here and swallowed from anywhere; other keys pass through
        untouched when the focus is elsewhere (27 Jul — the user keeps
        working while the stream lands in the original field).

        Return True = the key reaches the app AND on_press/on_release;
        return False = the app only; suppress_event() = nobody. Errors fail
        OPEN — a bug here must never leave the keyboard dead. Runs on the
        hook thread under Windows' hook timeout, doing the same work the
        on_press path already did on this same thread.
        """
        try:
            if data.flags & _INJECTED_FLAGS:
                # Text WE typed, or the modifier clear at dictation start.
                # The app must get it; our hotkey handlers must not react.
                return False
            if self._suppress is None or not self._suppress.is_set():
                return True
            key = self._vk_keys.get(data.vkCode)
            if key is not None:
                if msg in _PRESS_MSGS:
                    self._handle_press(key)
                elif msg in _RELEASE_MSGS:
                    self._handle_release(key)
            elif not self._focus_on_target():
                # Typing somewhere OTHER than the field being dictated into:
                # that keyboard stays alive. Esc included — elsewhere it
                # keeps its normal meaning; the hotkey still stops from
                # anywhere.
                return True
            elif data.vkCode == _VK_ESCAPE and msg in _PRESS_MSGS:
                if self._on_cancel is not None:
                    self._safely(self._on_cancel)
        except Exception:
            log.exception("key filter failed — letting the key through")
            return True
        self._listener.suppress_event()   # raises; the app never sees the key

    def _focus_on_target(self) -> bool:
        """Whether the keyboard focus is in the field this dictation types
        into. No target info (tests, embedders, or the capture failed and
        injection follows the focus) means the old M11 rule: suppress all."""
        if self._target is None:
            return True
        tgt = self._target()
        if tgt is None:
            return True
        return focused_control() == tgt

    def _handle_press(self, key) -> None:
        k = self._listener.canonical(key)  # ctrl_l/ctrl_r -> ctrl, etc.
        if k not in self._combo:
            return
        self._pressed.add(k)
        # OS key auto-repeat re-fires on_press while held; _active guards it.
        if not self._active and self._pressed == self._combo:
            self._active = True
            self._safely(self._on_start)

    def _handle_release(self, key) -> None:
        k = self._listener.canonical(key)
        if k not in self._combo:
            return
        self._pressed.discard(k)
        if self._active:
            self._active = False
            self._safely(self._on_stop)

    @staticmethod
    def _safely(callback) -> None:
        # An exception escaping a pynput callback kills the listener thread
        # and with it the whole app — a dictation tool must outlive one bad
        # utterance, so log and carry on instead.
        try:
            callback()
        except Exception:
            log.exception("hotkey callback failed — listener keeps running")

    def start(self) -> None:
        self._listener.start()

    def join(self) -> None:
        self._listener.join()

    def stop(self) -> None:
        self._listener.stop()


class LocalFlowApp:
    """Wires hotkey -> StreamRecorder -> core pipeline -> inject_text."""

    def __init__(self, engine: SttEngine, vad: VadGate,
                 recorder: StreamRecorder, inject_mode: str = "paste",
                 on_state=None, cleanup=None, restore_delay_s: float = 0.5,
                 preprocess: AudioPreprocessor | None = None,
                 cleanup_loader=None,
                 suppress_keys: threading.Event | None = None):
        self.engine = engine
        self.vad = vad
        self.recorder = recorder
        self.inject_mode = inject_mode
        self.restore_delay_s = restore_delay_s
        # Audio cleanup (high-pass / denoise / normalize) before STT. None
        # means raw mic audio goes straight to the pipeline.
        self.preprocess = preprocess
        # Drop Whisper's trailing-silence hallucinations (a stray "you" at the
        # end, "thanks for watching", ...) before cleanup. Read per segment so
        # a settings toggle applies live.
        self.filter_hallucinations = True
        # The last dictation's final text, for the pill's "Copy last dictation"
        # menu (Jeremy: recover the text if the paste target wasn't focused).
        # Kept in memory regardless of the history setting.
        self.last_text: str | None = None
        # FR-8 sync clean mode. The LLM costs ~2-3GB of shared GPU memory and
        # seconds of load time, so by default it is NOT loaded at startup:
        # `cleanup_loader` (a zero-arg callable returning a CleanupEngine)
        # defers that cost to the first tray toggle, in a background thread.
        # A preloaded `cleanup` (cleanup_enabled=true) still works as before.
        self.cleanup = cleanup
        self._cleanup_loader = cleanup_loader
        self._cleanup_loading = False
        # Serializes toggle_cleanup: the tray thread and the startup warmup
        # thread (cleanup_enabled preload) can both ask for a load - without
        # this, a check-then-spawn race could load two 2-3GB engines.
        self._cleanup_lock = threading.Lock()
        self._cleanup_user_touched = False  # tray toggle beats the preload
        self.cleanup_active = cleanup is not None
        # FR-2 hook: called with 'idle' | 'rec' | 'busy' (tray sets this).
        self.on_state = on_state
        # Called with a short message when a dictation produced NO text
        # (silence gate, garbage decode, dead mic). Jeremy 11 Jul: without
        # this the failure was invisible - no paste, no history, no cue.
        # _run wires it to the pill's flash.
        self.on_no_output = None
        self.latencies_ms: list[int] = []  # release-to-paste totals (AT-4)
        # M6: warmup (VadGate build + warm pass) runs on a background thread
        # after the hotkey goes live. `warm` gates the worker's first
        # transcribe so the engine is never driven from two threads at once.
        # An app constructed with its VAD already in hand is warm by
        # definition (tests, embedders); _run() passes vad=None and sets
        # `self.vad` + the event from its warmup thread.
        self.warm = threading.Event()
        if vad is not None:
            self.warm.set()
        # M8: the settings "Test mic" runs a transcription off the worker
        # thread; this lock keeps the engine strictly single-threaded, and
        # the flag keeps the RECORDER single-owner (a hotkey press during
        # the 3s test is refused instead of truncating both captures).
        self.engine_lock = threading.Lock()
        self.mic_test_active = False
        # Serializes CLAIMING the recorder: Test mic, Calibrate and a
        # hotkey press can all race check-then-act on recorder.recording
        # (found by review) - claim_mic_session() and on_hotkey_down both
        # decide under this lock, so exactly one owner wins.
        self.mic_session_lock = threading.Lock()
        # When the mic last started a session (dictation, test or calibrate).
        # Idle auto-calibration only samples well clear of this, so its 2s
        # claim can practically never eat a hotkey press mid-flow.
        self.last_mic_use = 0.0
        # M11: tap-to-toggle vs hold-to-talk, plus the keyboard suppression
        # that runs for the length of a dictation. The gate also swallows the
        # key-up of a refused key-down (the listener tracks the combo as
        # active regardless, and an unpaired up would steal the mic test's
        # recording). suppress_keys is None for tests and embedders: no hook
        # is installed there, so no synthetic keys are ever sent.
        self._gate = TapHoldGate()
        self.suppress_keys = suppress_keys
        # M13: live_typing off restores EXACTLY v1.1 — hold-to-talk (no tap
        # toggle), no keyboard suppression, no preview loop, paste on release.
        self.live_typing = True
        # M10: personal dictionary (applied after cleanup, before inject)
        # and the optional history store (None = history off, write nothing).
        self.replacements: dict[str, str] = {}
        # Rewrite spoken numbers as digits ("forty seven" -> "47") in the
        # same post-STT pass as the dictionary (9 Aug: Parakeet spells
        # numbers out; Jeremy wants digits).
        self.numbers_as_digits = False
        self.history = None
        # Tier 1 (VAD-segmented streaming). The segmenter cuts a long hold at
        # silences and enqueues mid-dictation segments transcribed DURING the
        # hold; the worker accumulates their raw texts and joins on release.
        # None until _run() builds it (after warmup). _dictation_id groups a
        # hold's segments; _produce_lock makes each drain+enqueue atomic so a
        # mid cut can never race the on-release final past it on the queue.
        self.segmenter = None
        # M12: the streaming commit engine. Re-decodes the live buffer ~1/s
        # and commits the words two decodes agree on. None until _run() builds
        # it (after warmup); log-only in M12.
        self.livetyper = None
        # The HWND of the field the current dictation types into, captured at
        # start. Live revisions and the final patch post straight to it, so
        # the text keeps landing there when focus moves (27 Jul). Safe as a
        # single slot: a stale dictation's typed_for() is "" so its final
        # never patches, and the livetyper only patches the current id.
        self._live_target: int | None = None
        self._dictation_id = 0
        self._produce_lock = threading.Lock()
        self._pending_id: int | None = None
        self._pending_texts: list[str] = []
        self._pending_stt_ms = 0
        self._pending_stt_device = "?"
        self._pending_had_audio = False
        self._pending_peak = 0.0
        # Auto mic gain (Jeremy 11 Jul): nudge the Windows input level after
        # real-but-quiet dictations, like meeting apps do. OFF here so tests
        # and embedders never touch COM; _run() arms it from cfg.auto_gain.
        self.auto_gain = False
        self._work: queue.Queue = queue.Queue()
        self._worker = threading.Thread(target=self._drain, daemon=True)

    def _set_state(self, state: str) -> None:
        if self.on_state is None:
            return
        try:
            self.on_state(state)
        except Exception:
            log.exception("state callback failed — app keeps running")

    def _notify_no_output(self, message: str) -> None:
        if self.on_no_output is None:
            return
        try:
            self.on_no_output(message)
        except Exception:
            log.exception("no-output callback failed — app keeps running")

    def claim_mic_session(self) -> bool:
        """Atomically take the recorder for a mic test / calibration (M8/
        M9). False when a dictation is running or another session owns it;
        the winner must clear mic_test_active in a finally."""
        with self.mic_session_lock:
            if self.mic_test_active or self.recorder.recording:
                return False
            self.mic_test_active = True
            self.last_mic_use = time.monotonic()
            return True

    def set_live_typing(self, on: bool) -> None:
        """M13's one switch. tap_max_s=0 makes every key-up a hold release,
        which is what "no toggle mode" means; the pill click keeps toggling
        (it did in v1.1 too). The preview loop reads live_typing through the
        is_dictating hook _run gives it, so this also silences the typing."""
        self.live_typing = bool(on)
        self._gate.tap_max_s = TAP_MAX_S if on else 0.0

    def on_hotkey_down(self) -> None:
        self._dispatch(self._gate.key_down(time.monotonic()))

    def on_hotkey_up(self) -> None:
        self._dispatch(self._gate.key_up(time.monotonic()))

    def cancel_dictation(self) -> None:
        """Esc while dictating: drop the audio, paste nothing. Frees the
        keyboard unconditionally — Esc is the escape hatch, so it must work
        even if the gate somehow thinks nothing is running."""
        action = self._gate.cancel()
        self._release_keyboard()
        self._dispatch(action)

    def _dispatch(self, action: str) -> None:
        if action == TapHoldGate.START:
            if not self._start_dictation():
                self._gate.cancel()   # refused: swallow the matching key-up
        elif action == TapHoldGate.STOP:
            self._stop_dictation()
        elif action == TapHoldGate.CANCEL:
            self._cancel_dictation()

    def _start_dictation(self) -> bool:
        """Begin recording. False when the settings "Test mic"/"Calibrate"
        owns the recorder — a dictation starting mid-test would truncate both
        captures, so the caller rolls the gate back instead."""
        with self.mic_session_lock:
            if self.mic_test_active:
                print("[--] mic test in progress - try again in a moment",
                      flush=True)
                return False
            # New dictation: bump the id so the segmenter and the final tag
            # this hold's segments together (and a stale mid cut from a prior
            # hold can never attach to this one).
            self._dictation_id += 1
            self.last_mic_use = time.monotonic()
            self.recorder.start()
        # The field under the caret RIGHT NOW is this dictation's target —
        # everything the dictation types is posted to it, wherever the user
        # goes afterwards. Only meaningful with live typing; the v1.1 paste
        # path stays focus-follows.
        self._live_target = focused_control() if self.live_typing else None
        if self._live_target:
            # The class decides the backspace style (inject._keydown_deletes)
            # — log it so a "backspaces ignored" report names the app at once.
            log.info("live target hwnd=0x%X class=%s", self._live_target,
                     window_class(self._live_target))
        self._grab_keyboard()
        self._set_state("rec")
        # FR-2 indicator (tray dot; console line kept for --no-tray). ASCII
        # only: Windows consoles often run cp1252, which can't encode glyphs.
        print("[rec] recording - release to transcribe", flush=True)
        return True

    def _stop_dictation(self) -> None:
        self._release_keyboard()
        self.last_mic_use = time.monotonic()
        # Atomic with any in-flight segment cut (Tier 1): stop() drains the
        # whole remaining buffer as the FINAL segment while the produce lock is
        # held, so a segmenter cut either lands fully before this or is refused
        # (recording is now False) — never enqueued after the final.
        with self._produce_lock:
            audio, rate = self.recorder.stop()
            self._close_livetyper()
            self._work.put(_Segment(audio=audio, rate=rate,
                                    dictation_id=self._dictation_id,
                                    final=True, t_release=time.perf_counter()))
        self._set_state("busy")

    def _cancel_dictation(self) -> None:
        """The captured audio is dropped here; a cancelled final still goes
        on the queue so the worker flushes whatever mid-dictation segments
        (Tier 1) it already accumulated instead of leaking them into the next
        dictation. Segments already queued are still transcribed, then
        discarded."""
        with self._produce_lock:
            self.recorder.stop()
            self._close_livetyper(discard=True)
            self._work.put(_Segment(audio=np.zeros(0, dtype=np.float32),
                                    rate=self.recorder.rate,
                                    dictation_id=self._dictation_id,
                                    final=True, cancelled=True))
        self._set_state("busy")
        print("[--] cancelled - nothing pasted", flush=True)

    def _close_livetyper(self, discard: bool = False) -> None:
        """M13: stop the preview committing the moment recording ends — a
        decode started while recording lands seconds later. The worker's
        authoritative segment tails still append; `discard` (Esc) blocks
        those too."""
        if self.livetyper is not None:
            self.livetyper.close(self._dictation_id, discard=discard)

    def _grab_keyboard(self) -> None:
        """M11: kill the physical keyboard for the length of the dictation and
        clear the app-visible modifier state (the hotkey's Alt stays down
        through a hold). No-op when no suppression event was supplied."""
        if self.suppress_keys is None or not self.live_typing:
            return
        self.suppress_keys.set()
        try:
            clear_modifiers()
        except Exception:
            log.exception("could not clear held modifiers — typing into the "
                          "focused app may misfire")

    def _release_keyboard(self) -> None:
        if self.suppress_keys is not None:
            self.suppress_keys.clear()

    def emit_segment(self, cut_frame: int, dictation_id: int) -> bool:
        """Tier 1 (called by the Segmenter): atomically drain a mid-dictation
        segment and enqueue it. Refused (False) if the hold already ended or
        moved on to a new dictation since the segmenter scanned — the drain and
        the enqueue happen together under the produce lock so ordering vs the
        on-release final is guaranteed."""
        with self._produce_lock:
            if dictation_id != self._dictation_id or not self.recorder.recording:
                return False
            seg = self.recorder.drain_segment(cut_frame)
            if len(seg) == 0:
                return False
            self._work.put(_Segment(audio=seg, rate=self.recorder.rate,
                                    dictation_id=dictation_id, final=False))
            if self.livetyper is not None:
                # M12: that audio has left the buffer, so the preview window
                # restarts here. Inside the produce lock for the same reason
                # the enqueue is: a preview tick must never scan the drained
                # buffer while still holding the pre-cut hypothesis.
                self.livetyper.on_cut(dictation_id)
        return True

    def toggle_recording(self) -> None:
        """Click-to-dictate (overlay): first call starts recording, second
        stops and transcribes. Runs on the overlay's Tk thread — both paths
        only flip the recorder and enqueue, same contract as the hotkey."""
        self._dispatch(self._gate.click())

    @property
    def cleanup_available(self) -> bool:
        """True when cleanup can be toggled: engine loaded, or lazily loadable."""
        return self.cleanup is not None or self._cleanup_loader is not None

    def toggle_cleanup(self) -> bool:
        """Tray toggle (FR-14). ON lazy-loads the model in the background
        (dictation keeps working raw meanwhile); OFF unloads it again, so
        the ~2-3GB of shared iGPU memory is only held while cleanup is
        actually wanted. No-op (False) when no model is available at all.
        Thread-safe: the tray thread and the startup preload both come
        through here."""
        self._cleanup_user_touched = True
        with self._cleanup_lock:
            return self._toggle_cleanup_locked()

    def preload_cleanup(self) -> None:
        """cleanup_enabled=true startup preload (M6: runs after warmup, off
        the critical path). Turns cleanup ON unless the user already used
        the tray toggle during warmup - the user's action always wins."""
        with self._cleanup_lock:
            if (self._cleanup_user_touched or self.cleanup is not None
                    or self.cleanup_active or self._cleanup_loading):
                return
            self._toggle_cleanup_locked()

    def _toggle_cleanup_locked(self) -> bool:
        if self.cleanup is None:
            if self._cleanup_loader is None:
                return False
            if self._cleanup_loading:   # user flipped again mid-load
                self.cleanup_active = not self.cleanup_active
                return self.cleanup_active
            self._cleanup_loading = True
            self.cleanup_active = True  # intent now; engine attaches when ready
            log.info("cleanup toggled ON — loading the model in the background "
                     "(dictation stays raw until it's ready)")
            threading.Thread(target=self._load_cleanup, daemon=True,
                             name="localflow-cleanup-load").start()
            return True
        self.cleanup_active = not self.cleanup_active
        if not self.cleanup_active and self._cleanup_loader is not None:
            self._unload_cleanup()
        else:
            log.info("cleanup %s", "ON" if self.cleanup_active else "OFF")
        return self.cleanup_active

    def _unload_cleanup(self) -> None:
        """Free the cleanup LLM's memory; the loader re-arms the next ON.
        An in-flight dictation keeps its own local reference and finishes
        safely — the engine is destroyed as soon as that drops."""
        engine, self.cleanup = self.cleanup, None
        close = getattr(engine, "close", None)
        if close is not None:
            close()
        gc.collect()   # destroy the C++ pipeline NOW, not at some later GC
        log.info("cleanup OFF — model unloaded (toggling it back on reloads)")

    def _load_cleanup(self) -> None:
        try:
            engine = self._cleanup_loader()
        except Exception:
            log.exception("cleanup model failed to load — cleanup stays OFF")
            with self._cleanup_lock:
                self.cleanup_active = False
                self._cleanup_loading = False
            return
        # The attach-or-discard decision must be atomic with the toggle: a
        # tray OFF that lands mid-load flips cleanup_active under the lock,
        # and this block must see it — otherwise the engine gets attached
        # anyway and 2-3GB stays resident behind an unchecked toggle.
        discard = None
        with self._cleanup_lock:
            self._cleanup_loading = False
            if self.cleanup_active:
                self.cleanup = engine
            else:
                discard = engine             # toggled back off mid-load
        if discard is not None:
            close = getattr(discard, "close", None)
            if close is not None:
                close()
            log.info("cleanup model loaded but toggled off mid-load — "
                     "discarded to free its memory")
        else:
            log.info("cleanup model loaded — cleanup is ON")

    def _drain(self) -> None:
        # COM-initialize this worker thread (never uninitialized; the app
        # hard-exits) so a cyclic GC that finalizes a pycaw/comtypes object
        # here can't access-violate in _ctypes. Same guard as the overlay UI
        # thread; micgain's auto-gain also runs on this thread.
        try:
            import comtypes
            comtypes.CoInitialize()
        except Exception:
            log.debug("worker CoInitialize failed", exc_info=True)
        while True:
            item = self._work.get()
            try:
                if isinstance(item, tuple):
                    # Legacy shape (audio, rate, t_release) = one complete
                    # single-segment utterance. Kept so direct queue puts still
                    # work exactly as before.
                    audio, rate, t_release = item
                    item = _Segment(audio=audio, rate=rate, dictation_id=-1,
                                    final=True, t_release=t_release)
                self._handle_segment(item)
            except Exception:
                log.exception("utterance failed — app keeps running")
            finally:
                # Back to idle only when nothing else is in flight: another
                # dictation may be queued behind this one (likelier now that
                # warmup can park the first), or the user may already be
                # recording the next - never stomp those indicators. A held
                # dictation whose mid-segments are being drained stays "rec".
                if self._work.empty() and not (
                        self.recorder is not None and self.recorder.recording):
                    self._set_state("idle")

    def _reset_pending(self, dictation_id: int | None) -> None:
        self._pending_id = dictation_id
        self._pending_texts = []
        self._pending_stt_ms = 0
        self._pending_stt_device = "?"
        self._pending_had_audio = False
        self._pending_peak = 0.0

    def _handle_segment(self, item: _Segment) -> None:
        """Worker step for one queued segment (Tier 1). Mid segments append
        their raw text to the current dictation; the final one joins them and
        runs cleanup + dictionary + inject + history once."""
        if item.dictation_id != self._pending_id:
            self._reset_pending(item.dictation_id)
        seg_text = ""
        if len(item.audio) > 0:
            if not self.warm.is_set():
                # A dictation in the first seconds after launch waits for the
                # background warmup instead of racing it - the engine must
                # NEVER be driven from two threads, so there is no timeout
                # escape here. The warmup thread sets the event in a `finally`,
                # so this wait always ends; the utterance is never dropped.
                log.info("dictation arrived before warmup finished - waiting")
                print("[..] still warming up - transcribing shortly", flush=True)
                self.warm.wait()
            self._pending_had_audio = True
            self._pending_peak = max(self._pending_peak,
                                     float(np.max(np.abs(item.audio))))
            result = self._transcribe_segment(item.audio, item.rate)
            seg_text = result.raw_text
            if result.raw_text:
                self._pending_texts.append(result.raw_text)
                self._pending_stt_ms += result.stt_ms
                self._pending_stt_device = result.device
        if self.livetyper is not None and not item.cancelled:
            # This segment's text is authoritative for the window the preview
            # was streaming: the livetyper appends the words it had not typed
            # yet (append-only, no correction — Jeremy 27 Jul). Called for
            # EVERY segment in queue order, silent mids and the final drain
            # included - reconciliation is in cut order, so skipping one
            # would shift every later segment onto the wrong window.
            self.livetyper.on_segment_text(item.dictation_id, seg_text)
        if item.final:
            if item.cancelled:      # Esc (M11): drop the whole dictation
                log.info("dictation cancelled — discarded %d segment(s)",
                         len(self._pending_texts))
                self._reset_pending(None)
                return
            texts = self._pending_texts
            stt_ms = self._pending_stt_ms
            device = self._pending_stt_device
            had_audio = self._pending_had_audio
            peak = self._pending_peak
            self._reset_pending(None)
            self._finish_dictation(texts, stt_ms, device,
                                   item.t_release or time.perf_counter(),
                                   had_audio, peak=peak,
                                   dictation_id=item.dictation_id)

    def _transcribe_segment(self, audio: np.ndarray, rate: int):
        """Raw STT of one segment (no cleanup, no inject) under the engine
        lock. Cleanup runs once later on the joined text, not per segment."""
        audio16 = ensure_16k_mono(audio, rate)
        with self.engine_lock:
            return transcribe_audio(
                audio16, self.engine, cleanup=None, vad=self.vad,
                preprocess=self.preprocess,
                filter_hallucinations=self.filter_hallucinations)

    def _finish_dictation(self, texts: list[str], stt_ms: int, device: str,
                          t_release: float, had_audio: bool,
                          peak: float = 0.0, dictation_id: int = -1) -> None:
        """Join a dictation's segment texts and run cleanup + dictionary +
        inject + history ONCE (Tier 1). For a normal short dictation `texts`
        holds a single segment, so this matches the old per-utterance path.
        `peak` is the raw mic peak across the dictation - auto_gain's input.

        With live typing on (M13) the words are already in the app — the
        final drain's tail was appended by on_segment_text before this runs —
        and NO correction pass touches them (Jeremy 27 Jul). The one
        exception is a final that produced nothing at all: typed text is
        taken back, or it would be words no dictation produced."""
        typed = self._typed_text(dictation_id)
        raw_joined = " ".join(t.strip() for t in texts if t and t.strip())
        if raw_joined and self.filter_hallucinations:
            # Per-segment collapse ran in the pipeline; this catches a repeat
            # straddling a Tier 1 segment join.
            raw_joined = collapse_repeats(raw_joined)
        if not raw_joined and len(typed.split()) > 3:
            # A final decode that returns NOTHING for audio the preview
            # committed real words from (two agreeing decodes each) is a
            # decode failure, not silence — the take-back erased a whole
            # 15-word dictation over one empty final (live 30 Jul). Trust
            # the screen: keep the typed text as this dictation's result.
            log.warning("final decode empty but %d live words typed — "
                        "keeping the typed text", len(typed.split()))
            raw_joined = typed
        if not raw_joined:
            total_ms = int((time.perf_counter() - t_release) * 1000)
            # The preview may have typed something the final pass then threw
            # away (a hallucinated tail, a VAD-gated window). Take it back —
            # leaving it would put words in the document that no dictation
            # ever produced. Only a SHORT stream qualifies (see above).
            self._patch_typed(typed, "")
            if not had_audio:
                # Zero frames the whole hold = wedged mic endpoint (seen live
                # 3 Jul: Samsung USB delivered nothing to ANY process until
                # replugged). Say so loudly instead of a silent no-paste.
                log.warning("recording captured 0 frames from %r — the mic "
                            "delivered no audio. If this repeats, replug the "
                            "mic or change the Windows default input device.",
                            getattr(self.recorder, "device_name", "?"))
                print("[--] nothing captured - mic delivered no audio",
                      flush=True)
                self._notify_no_output("Mic gave no audio - check the mic")
            else:
                print(f"[--] silence - nothing pasted ({total_ms}ms)",
                      flush=True)
                self._notify_no_output("Nothing heard - no text")
            return
        text = raw_joined
        clean_text: str | None = None
        cleanup_ms: int | None = None
        cleanup = self.cleanup if self.cleanup_active else None
        if cleanup is not None:
            t0 = time.perf_counter()
            try:  # FR-8: cleanup can never block dictation — raw wins on error
                clean_text = cleanup.clean(raw_joined)
                cleanup_ms = int((time.perf_counter() - t0) * 1000)
                text = clean_text
            except Exception:
                log.warning("cleanup failed — keeping raw text only",
                            exc_info=True)
                clean_text = None
        text = self.post_stt(text)
        # Remember it BEFORE injecting: "Copy last dictation" must recover the
        # text even when the paste went nowhere (target wasn't focused).
        self.last_text = text
        if typed:
            injected = True          # streamed + tail already appended
        else:
            injected = inject_text(text, mode=self.inject_mode,
                                   restore_delay_s=self.restore_delay_s)
        total_ms = int((time.perf_counter() - t_release) * 1000)
        if injected:
            self.latencies_ms.append(total_ms)
            history = self.history      # snapshot: the settings toggle can
            if history is not None:     # null this attr mid-utterance
                history.append(
                    raw_text=raw_joined, clean_text=clean_text,
                    final_text=text,
                    device=getattr(self.recorder, "device_name", "?"),
                    stt_ms=stt_ms)
            cleanup_note = (f", cleanup {cleanup_ms}ms"
                            if cleanup_ms is not None else "")
            # FR-7 per-utterance log + release-to-paste total (feeds AT-4).
            print(f"[ok] {'streamed' if typed else 'pasted'} {len(text)} chars "
                  f"in {total_ms}ms "
                  f"(stt {stt_ms}ms on {device}{cleanup_note}, "
                  f"n={len(self.latencies_ms)}): {text!r}", flush=True)
        else:
            print(f"[--] silence - nothing pasted ({total_ms}ms)", flush=True)
            self._notify_no_output("Nothing heard - no text")
        if self.auto_gain:
            # Real speech was decoded this dictation, so the peak is a
            # trustworthy loudness sample - adjust the Windows mic level on
            # it. The all-silence paths returned above on purpose.
            self._maybe_adjust_gain(peak)

    def post_stt(self, text: str) -> str:
        """Deterministic post-STT shaping: the M10 dictionary, then spoken
        numbers -> digits. The livetyper applies this same method to every
        preview decode and segment text, so live-typed screens, the final
        text and history all agree on one surface. Each stage is guarded —
        a bad rule must never block a paste."""
        if not text:
            return text
        if self.replacements:
            try:
                text = apply_replacements(text, self.replacements)
            except Exception:
                log.exception("dictionary failed — using unmodified text")
        if self.numbers_as_digits:
            try:
                text = spelled_to_digits(text)
            except Exception:
                log.exception("number rewrite failed — using unmodified text")
        return text

    def _typed_text(self, dictation_id: int) -> str:
        """What live typing already put in the focused app for THIS dictation
        (M13). Empty when the preview never ran for it - live typing off, or
        the toggle flipped mid-dictation - and every path below then falls
        straight back to the v1.1 paste. Asking the LiveTyper rather than the
        current flag is what keeps a mid-dictation toggle honest: the answer
        is what was actually typed, not what the setting says now."""
        if self.livetyper is None:
            return ""
        return self.livetyper.typed_for(dictation_id)

    def _patch_typed(self, typed: str, final: str) -> None:
        """Turn the live-typed text into the final text in place (M13).
        Since the no-correction rule (27 Jul) its only caller is the
        silence take-back: final="" erases text the dictation disowned."""
        if not typed:
            return
        # loose: punctuation/case drift in already-typed words is left alone
        # (fixing a mid-text comma via keystrokes means retyping everything
        # after it); real word differences are patched from where they start.
        n_back, retype = patch_plan(typed, final, loose=True)
        if not n_back and not retype:
            log.info("live typing matched the final text - nothing to patch")
            return
        log.info("final patch: %d backspace(s), retyping %r", n_back, retype)
        try:
            patch_text(n_back, retype, target=self._live_target)
        except Exception:
            log.exception("final patch failed - the live text stays as typed")

    def _maybe_adjust_gain(self, peak: float) -> None:
        """auto_gain: one nudge of the WINDOWS mic input level after a
        dictation that decoded speech but peaked too quiet (or clipped) -
        what meeting apps' auto-sensitivity does; digital normalize can't
        recover SNR the way a higher capture level can. Only while following
        the system default: that is the endpoint being adjusted, and a
        pinned mic is the user's explicit choice."""
        rec = self.recorder
        if (rec is None
                or getattr(rec, "_configured_device", None) is not None
                or getattr(rec, "fallback_active", False)):
            return
        try:
            from localflow.desktop import micgain
            changed = micgain.adjust_for_peak(peak)
        except Exception:
            log.debug("auto-gain skipped", exc_info=True)
            return
        if changed is not None:
            old, new = changed
            direction = "quiet" if new > old else "clipping"
            log.info("auto-gain: dictation peaked %.3f (%s) - Windows mic "
                     "input volume %d%% -> %d%%", peak, direction,
                     round(old * 100), round(new * 100))
            print(f"[..] mic {direction} - input volume "
                  f"{round(old * 100)}% -> {round(new * 100)}%", flush=True)

    def latency_summary(self) -> str | None:
        """AT-4: p50/p95 release-to-paste over this session's pasted
        utterances. None if nothing was pasted."""
        if not self.latencies_ms:
            return None
        p50, p95 = np.percentile(self.latencies_ms, [50, 95])
        return (f"[at-4] {len(self.latencies_ms)} dictations: p50 {int(p50)}ms, "
                f"p95 {int(p95)}ms (target: p50 < 1200ms)")

    def start_worker(self) -> None:
        self._worker.start()


def apply_cli_overrides(cfg: config.Config, args: argparse.Namespace) -> config.Config:
    """CLI flags beat the config file (FR-13). None means 'flag not given'."""
    for flag, field in (
        ("model", "stt_model_dir"),
        ("device", "device"),
        ("fallback", "fallback_device"),
        ("cache", "cache_dir"),
        ("hotkey", "hotkey"),
        ("inject_mode", "inject_mode"),
        ("input_device", "input_device"),
    ):
        value = getattr(args, flag)
        if value is not None:
            setattr(cfg, field, value)
    if getattr(args, "no_overlay", False):
        cfg.overlay_enabled = False
    return cfg


# M8: live-apply plumbing. Keys listed here take effect only on restart;
# everything else in Config has a live path in apply_settings below (the
# settings window shows a "takes effect on restart" note for these).
RESTART_KEYS = frozenset({
    "device", "fallback_device", "stt_model_dir", "stt_language",
    "cleanup_model_dir", "cache_dir", "hot_words", "overlay_enabled",
})
_PREPROCESS_KEYS = ("denoise_enabled", "normalize_audio", "denoise_strength")


def apply_settings(changes: dict, cfg: config.Config, hooks: dict) -> set[str]:
    """Apply already-validated settings changes to the RUNNING app (M8).

    `hooks` maps action names to callables owned by _run():
    hotkey, preprocess, restore_delay, inject_mode, input_device,
    visibility, pill_size, cleanup. Only keys that actually differ from
    `cfg` are applied; `cfg` is mutated so it stays the source of truth.
    Returns the subset of keys that need a restart instead."""
    restart: set[str] = set()
    preprocess_dirty = False
    for key, value in changes.items():
        if getattr(cfg, key, object()) == value:
            continue
        setattr(cfg, key, value)
        if key in RESTART_KEYS:
            restart.add(key)
        elif key in _PREPROCESS_KEYS:
            preprocess_dirty = True     # one rebuild for the whole group
        elif key == "hotkey":
            hooks["hotkey"](value)
        elif key == "restore_delay_ms":
            hooks["restore_delay"](value / 1000.0)
        elif key == "max_utterance_s":
            hooks["max_utterance"](value)
        elif key == "segment_seconds":
            hooks["segment"](value)
        elif key == "inject_mode":
            hooks["inject_mode"](value)
        elif key == "input_device":
            hooks["input_device"](value)
        elif key == "vad_threshold":
            hooks["vad"](value)
        elif key == "filter_hallucinations":
            hooks["filter_hallucinations"](value)
        elif key == "tts_length_scale":
            hooks["tts_speed"](value)
        elif key == "tts_volume":
            hooks["tts_volume"](value)
        elif key == "overlay_visibility":
            hooks["visibility"](value)
        elif key == "pill_size":
            hooks["pill_size"](value)
        elif key == "cleanup_enabled":
            hooks["cleanup"](value)
        elif key == "live_typing":
            hooks["live_typing"](value)
        elif key == "overlay_position":
            pass                        # owned by the drag/reset paths
        elif key == "history_enabled":
            hooks["history"](value)
        elif key == "replacements":
            hooks["replacements"](value)
        else:
            restart.add(key)            # no live path -> be honest
    if preprocess_dirty:
        hooks["preprocess"]()
    return restart


def warmup(engine: SttEngine, vad: VadGate, cleanup=None,
           preprocess: AudioPreprocessor | None = None) -> None:
    """One dummy pass at startup so the first real utterance doesn't pay the
    first-run costs (M3: 1247ms first vs ~450ms warm; plus one 879ms VAD
    outlier suspected to be soxr/ONNX warmup). Output is discarded."""
    t0 = time.perf_counter()
    silence = np.zeros(int(0.5 * 44_100), dtype=np.float32)
    if preprocess is not None:
        preprocess.process(np.zeros(8_000, dtype=np.float32))  # warms scipy import
    vad.trim(ensure_16k_mono(silence, 44_100))  # warms soxr + the VAD session
    engine.transcribe(np.zeros(8_000, dtype=np.float32))  # warms Whisper
    if cleanup is not None:
        try:
            cleanup.clean("um hello there")     # warms the LLM pipeline
        except Exception:
            log.warning("cleanup warmup failed", exc_info=True)
    log.info("warmup pass done in %dms", int((time.perf_counter() - t0) * 1000))


def _already_running() -> bool:
    """Single-instance guard: two LocalFlows would both hear the hotkey and
    paste every dictation twice. A named Win32 mutex (session-local) marks a
    live instance; the OS releases it when the process dies."""
    import ctypes
    kernel32 = ctypes.windll.kernel32
    kernel32.SetLastError(0)
    kernel32.CreateMutexW(None, False, "LocalFlow-singleton")
    return kernel32.GetLastError() == 183  # ERROR_ALREADY_EXISTS


def _alert(message: str) -> None:
    """Windowless launches have no console — surface fatal problems in a
    message box so they don't vanish silently."""
    import ctypes
    ctypes.windll.user32.MessageBoxW(None, message, "LocalFlow", 0x40)


# Keeps the crash.log file object alive: faulthandler holds only the fd, and
# if the file were GC'd the fd could be reused and the handler would scribble
# on some other file at the worst possible moment.
_crash_log = None


def _report_previous_crash(crash_path: Path) -> None:
    """If the previous run died hard (native fault or import failure), say so
    in THIS run's normal log — the evidence is otherwise only in crash.log,
    which nobody reads until a session goes hunting for it."""
    try:
        tail = crash_path.read_text(encoding="utf-8", errors="replace")[-65536:]
    except OSError:
        return
    last_run = tail.rsplit("--- LocalFlow start", 1)[-1]
    if "Windows fatal exception" in last_run or "failed to import" in tail[-4096:]:
        log.warning("previous run CRASHED - the reason is recorded in %s",
                    crash_path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=None, metavar="PATH",
                    help="settings file (default search: ./localflow.toml, "
                         "then %%APPDATA%%/LocalFlow/localflow.toml)")
    ap.add_argument("--model", default=None, help="override stt_model_dir")
    ap.add_argument("--device", default=None, help="GPU | NPU | CPU")
    ap.add_argument("--fallback", default=None, help="override fallback_device")
    ap.add_argument("--cache", default=None, help="OpenVINO compile cache dir")
    ap.add_argument("--hotkey", default=None,
                    help="hold-to-talk combo, e.g. alt+`, ctrl+space, f9")
    ap.add_argument("--inject-mode", default=None, choices=["paste", "type"],
                    help="FR-12: paste (clipboard+Ctrl+V) or type (keystrokes)")
    ap.add_argument("--input-device", default=None,
                    help="sounddevice input device index or name substring")
    ap.add_argument("--no-tray", action="store_true",
                    help="console-only mode (no tray icon); Ctrl+C quits")
    ap.add_argument("--no-overlay", action="store_true",
                    help="disable the on-screen waveform pill")
    args = ap.parse_args(argv)

    # Transcripts echoed to the console may contain characters the active
    # codepage can't encode (cp1252) — degrade to '?' rather than crash.
    # Under localflow-tray (gui-script) sys.stdout is None and print() is a
    # silent no-op — the tray is the UI there and the log file has the rest.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")

    fmt = "%(levelname)-7s %(name)s: %(message)s"
    if sys.stderr is not None:
        logging.basicConfig(level=logging.INFO, format=fmt)
        log_path = None
    else:
        # Windowless launch: no console to log to — write a file instead.
        log_dir = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "LocalFlow"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "localflow.log"
        logging.basicConfig(level=logging.INFO, format="%(asctime)s " + fmt,
                            filename=str(log_path), encoding="utf-8")

    # Uncaught Python exceptions — main thread, any worker thread, or a
    # __del__/finalizer — must land in the log; under pythonw they would
    # otherwise vanish with the process.
    sys.excepthook = lambda *exc: log.critical(
        "uncaught exception", exc_info=exc)
    threading.excepthook = lambda a: log.critical(
        "uncaught exception in thread %s",
        a.thread.name if a.thread else "?",
        exc_info=(a.exc_type, a.exc_value, a.exc_traceback))
    sys.unraisablehook = lambda a: log.error(
        "unraisable exception in %r: %r", a.object, a.exc_value)

    # A native crash (access violation) kills pythonw with NOTHING in the
    # log — the three 13 Jul _ctypes.pyd crashes had to be guessed at from
    # Windows Error Reporting. faulthandler catches the fault first and
    # writes every thread's Python stack here, naming the guilty code path.
    global _crash_log
    try:
        import faulthandler
        crash_dir = (Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
                     / "LocalFlow")
        crash_dir.mkdir(parents=True, exist_ok=True)
        _report_previous_crash(crash_dir / "crash.log")
        _crash_log = open(crash_dir / "crash.log", "a", encoding="utf-8")
        _crash_log.write(f"--- LocalFlow start "
                         f"{time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
        _crash_log.flush()
        faulthandler.enable(_crash_log, all_threads=True)
        log.info("crash handler armed -> %s", crash_dir / "crash.log")
    except Exception:
        log.warning("faulthandler setup failed", exc_info=True)

    try:
        return _run(args)
    except Exception:
        log.exception("LocalFlow died on startup or shutdown")
        if log_path is not None:
            _alert(f"LocalFlow failed to start.\nSee the log: {log_path}")
        return 1


def _run(args: argparse.Namespace) -> int:
    if _already_running():
        msg = "LocalFlow is already running - check the system tray."
        print(msg, flush=True)
        log.warning(msg)
        if sys.stderr is None:
            _alert(msg)
        return 2

    cfg_path = config.find_config(args.config)
    cfg = apply_cli_overrides(config.load_config(cfg_path), args)
    settings_path = cfg_path or Path.cwd() / config.CONFIG_FILENAME

    def persist_settings(changes: dict) -> None:
        # Comment-preserving write-back (M7). Never lets a settings write
        # take dictation down.
        try:
            config.save_config(settings_path, changes)
        except Exception:
            log.exception("could not save settings %s", sorted(changes))

    def save_pill_position(pos) -> None:
        # cfg must track the drag too: a pill_size rebuild (M8) reads
        # cfg.overlay_position - a file-only save would snap the pill back
        # to a stale spot on rebuild.
        cfg.overlay_position = pos
        persist_settings({"overlay_position": pos})

    # The overlay goes up FIRST so launching gives instant feedback ("loading
    # models...") instead of a blank few seconds; it fails soft to no overlay.
    # M7: the overlay thread + Tk root exist in EVERY visibility mode (the
    # settings window needs a UI thread regardless); overlay_enabled=false
    # and --no-overlay map to the "never" mode (window withdrawn, tray menu
    # is the way back).
    overlay = None
    overlay_visibility = cfg.overlay_visibility if cfg.overlay_enabled else "never"
    try:
        from localflow.desktop.overlay import WaveformOverlay
        overlay = WaveformOverlay(hotkey=cfg.hotkey,
                                  visibility=overlay_visibility,
                                  position=cfg.overlay_position,
                                  pill_size=cfg.pill_size)
        overlay.on_position_change = save_pill_position
        overlay.set_state("loading")
    except Exception:
        log.exception("overlay unavailable — continuing without it")

    input_device: int | str | None = cfg.input_device
    if isinstance(input_device, str) and input_device.isdigit():
        input_device = int(input_device)

    # Whisper vocabulary biasing (hot_words in localflow.toml).
    initial_prompt = None
    if cfg.hot_words:
        initial_prompt = "Vocabulary: " + ", ".join(cfg.hot_words) + "."

    # Build everything on a BACKGROUND startup thread (M11, Jeremy's request:
    # pill up immediately with a loading bar, only "loading" vs "usable" shown).
    # The mic open (~1s), Whisper model load (~1.7s), VAD load (~2.4s) and warm
    # pass all run in `startup_worker` below while the main thread goes straight
    # to the tray loop — so the pill, tray and hotkey are all live in well under
    # a second. The worker waits on app.warm, so engine/vad/preprocess are never
    # used before they exist.
    print("Starting LocalFlow (models load in the background)...", flush=True)
    recorder = StreamRecorder(input_device=input_device,
                              max_seconds=cfg.max_utterance_s)

    # Audio cleanup before STT (FR-13): high-pass + gentle spectral denoise
    # and loudness normalization. Helps the quiet/noisy built-in mic array
    # most; toggle in localflow.toml if it ever costs accuracy.
    def build_preprocessor() -> AudioPreprocessor | None:
        """One builder for startup + live-apply + calibration (M9): attaches
        the calibrated noise floor when a saved profile matches the mic."""
        if not (cfg.denoise_enabled or cfg.normalize_audio):
            return None
        floor = None
        if cfg.denoise_enabled:
            try:
                from localflow.desktop import noiseprofile
                floor = noiseprofile.load_for_device(recorder.device_name)
            except Exception:
                log.exception("noise profile load failed — using the "
                              "per-clip estimate")
        return AudioPreprocessor(denoise=cfg.denoise_enabled,
                                 normalize=cfg.normalize_audio,
                                 denoise_strength=cfg.denoise_strength,
                                 noise_floor=floor)

    # (preprocess is built inside startup_worker, once the mic is open.)

    # Cleanup LLM: ~2-3GB of shared iGPU memory + seconds of load time, so it
    # is only loaded at startup when cleanup_enabled=true. Otherwise, if the
    # model exists on disk, hand the app a lazy loader — the tray toggle
    # loads it in the background the first time Jeremy actually wants it.
    cleanup = None
    cleanup_loader = None
    if cfg.cleanup_model_dir and Path(cfg.cleanup_model_dir).is_dir():
        def cleanup_loader():
            from localflow.core import CleanupEngine
            engine = CleanupEngine(cfg.cleanup_model_dir, device=cfg.device,
                                   cache_dir=cfg.cache_dir,
                                   terms=cfg.cleanup_terms)
            try:
                engine.clean("um hello there")  # warm the pipeline off-path
            except Exception:
                log.debug("cleanup warmup failed", exc_info=True)
            return engine
        if cfg.cleanup_enabled:
            # M6: the preload moved into the background warmup thread below
            # so a multi-second LLM load never delays the hotkey going live.
            log.info("cleanup_enabled=true — the model loads in the "
                     "background after warmup")
        else:
            log.info("cleanup model found — not loading it at startup; the "
                     "tray toggle loads it on demand")
    elif cfg.cleanup_enabled:
        log.warning("cleanup_enabled=true but the model dir %s does not "
                    "exist — cleanup unavailable", cfg.cleanup_model_dir)

    # engine=None, vad=None, preprocess=None: all three arrive from
    # startup_worker below; the worker waits on app.warm before its first
    # transcribe, so it never sees them unset (never crashes or drops).
    app = LocalFlowApp(None, None, recorder, inject_mode=cfg.inject_mode,
                       cleanup=cleanup, cleanup_loader=cleanup_loader,
                       restore_delay_s=cfg.restore_delay_ms / 1000.0,
                       preprocess=None, suppress_keys=threading.Event())
    app.filter_hallucinations = bool(cfg.filter_hallucinations)
    app.auto_gain = bool(cfg.auto_gain)
    app.set_live_typing(cfg.live_typing)
    # M10: dictionary + history from config (both live-appliable).
    app.replacements = dict(cfg.replacements)
    app.numbers_as_digits = bool(cfg.numbers_as_digits)
    if cfg.history_enabled:
        from localflow.desktop.history import HistoryStore
        app.history = HistoryStore()
    app.start_worker()
    # Tier 1 (VAD-segmented streaming): the segmenter cuts a long hold at
    # silences DURING the hold so the end-of-dictation wait stays short. Its
    # private VAD is built and the thread started from the warmup thread below;
    # segment_seconds = 0 leaves it dormant.
    from localflow.desktop.segmenter import Segmenter
    app.segmenter = Segmenter(recorder, vad=None, emit=app.emit_segment,
                              current_id=lambda: app._dictation_id,
                              segment_seconds=cfg.segment_seconds)
    # M12/M13: the preview loop types committed words DURING the dictation
    # (append-only: two decodes must agree, nothing is ever rewritten -
    # Jeremy 27 Jul). is_dictating excludes the mic test / calibration (they
    # also put the recorder in `recording` but are not speech to type) and
    # live_typing=false (v1.1 mode: nothing streams).
    from localflow.desktop.livetyper import LiveTyper
    app.livetyper = LiveTyper(
        recorder, engine_lock=app.engine_lock,
        is_dictating=lambda: (recorder.recording and not app.mic_test_active
                              and app.live_typing),
        current_id=lambda: app._dictation_id,
        on_patch=lambda n, t: live_patch(n, t, target=app._live_target),
        transform=app.post_stt)
    # Held in a ref so the settings window can rebuild it live (M8).
    hotkey_ref = {"listener": PushToTalkListener(
        cfg.hotkey, app.on_hotkey_down, app.on_hotkey_up,
        suppress=app.suppress_keys, on_cancel=app.cancel_dictation,
        target=lambda: app._live_target)}
    hotkey_ref["listener"].start()

    # Read-aloud (TTS): press read_aloud_hotkey to speak the SELECTED text, or
    # "Narrate clipboard" from the tray. Fully local Piper voice, lazy-loaded
    # on first use so startup stays lean. Off if the voice file is missing.
    narrator = None
    narrate_selection = None
    narrate_ref: dict = {"listener": None}
    if cfg.tts_enabled:
        if Path(cfg.tts_voice).is_file():
            from localflow.desktop.narrate import Narrator
            narrator = Narrator(cfg.tts_voice, length_scale=cfg.tts_length_scale,
                                volume=cfg.tts_volume)

            def narrate_selection() -> None:
                # Always OFF the caller's thread: toggle_selection copies the
                # selection with a ~0.2s settle, which must never block the
                # pynput hook thread (freezes the keyboard) or the Tk thread.
                def work() -> None:
                    try:
                        narrator.toggle_selection()
                    except Exception:
                        log.exception("read-aloud failed - continuing")
                threading.Thread(target=work, daemon=True,
                                 name="localflow-narrate").start()

            try:
                narrate_ref["listener"] = keyboard.GlobalHotKeys(
                    {to_pynput_combo(cfg.read_aloud_hotkey): narrate_selection})
                narrate_ref["listener"].start()
                log.info("read-aloud hotkey live -> %s", cfg.read_aloud_hotkey)
            except Exception:
                log.exception("read-aloud hotkey %r failed to register",
                              cfg.read_aloud_hotkey)
        else:
            log.warning("tts_enabled but voice file %s not found - read-aloud "
                        "off", cfg.tts_voice)

    # M7: pill visibility + position controls, live from the tray.
    vis_state = {"mode": overlay_visibility}

    def set_pill_visibility(mode: str) -> None:
        vis_state["mode"] = mode
        if overlay is not None:
            overlay.set_visibility(mode)
        changes: dict = {"overlay_visibility": mode}
        if mode != "never" and not cfg.overlay_enabled:
            # Choosing a visible mode from the tray must survive a restart
            # even when the pill was disabled outright in the config.
            changes["overlay_enabled"] = True
            cfg.overlay_enabled = True
        persist_settings(changes)

    def reset_pill_position() -> None:
        if overlay is not None:
            overlay.reset_position()  # its callback persists the removal

    def open_settings_entry() -> None:
        # M8: the settings window lives on the overlay's UI thread; the
        # overlay marshals this through its command queue. No overlay ->
        # the old escape hatch (open the toml).
        if overlay is not None:
            overlay.open_settings()
        else:
            config.open_settings_editor(settings_path)

    # Shown in the tray menu + settings header; startup_worker fills in the
    # real device once the engine is loaded (pystray re-reads the callable).
    status_holder = {"text": "LocalFlow - loading model..."}

    tray = None
    restart_ref = {"want": False}
    if not args.no_tray:
        try:
            from localflow.desktop.tray import TrayIcon

            def request_restart() -> None:
                restart_ref["want"] = True

            tray = TrayIcon(lambda item: status_holder["text"], settings_path,
                            restart=request_restart,
                            cleanup_get=(lambda: app.cleanup_active)
                            if app.cleanup_available else None,
                            cleanup_toggle=app.toggle_cleanup,
                            visibility_get=(lambda: vis_state["mode"])
                            if overlay is not None else None,
                            visibility_set=set_pill_visibility
                            if overlay is not None else None,
                            reset_pill_position=reset_pill_position
                            if overlay is not None else None,
                            open_settings=open_settings_entry,
                            narrate=narrate_selection)
        except Exception:
            log.exception("tray unavailable — falling back to console-only mode")
            tray = None

    def fan_out_state(state: str) -> None:
        if tray is not None:
            tray.set_state(state)
        if overlay is not None:
            overlay.set_state(state)

    quit_event = threading.Event()

    def request_quit() -> None:
        # Overlay "Quit": unblock whichever loop owns the main thread.
        log.info("quit requested from the overlay")
        quit_event.set()
        if tray is not None:
            tray.stop()

    # M8: mic levels fan out to the pill AND the settings window's meter.
    level_sinks: list = []

    def fan_out_level(rms: float) -> None:
        for sink in list(level_sinks):
            try:
                sink(rms)
            except Exception:
                log.debug("level sink failed", exc_info=True)

    recorder.on_level = fan_out_level

    # -- M8: live-apply hooks (each owns ONE settings key's live path) ----
    def hook_hotkey(combo: str) -> None:
        old = hotkey_ref["listener"]
        new = PushToTalkListener(combo, app.on_hotkey_down, app.on_hotkey_up,
                                 suppress=app.suppress_keys,
                                 on_cancel=app.cancel_dictation,
                                 target=lambda: app._live_target)
        new.start()
        hotkey_ref["listener"] = new
        old.stop()
        log.info("hotkey rebuilt live -> %s", combo)

    def hook_preprocess() -> None:
        app.preprocess = build_preprocessor()
        log.info("audio preprocessing now: %s",
                 app.preprocess.describe() if app.preprocess else "off")

    def hook_vad(value: float) -> None:
        # VadGate.trim reads self.threshold per call, so this is live. If
        # warmup hasn't built the gate yet, it constructs from cfg (already
        # mutated by apply_settings) - either order lands the new value. The
        # segmenter's private gate tracks the same threshold.
        if app.vad is not None:
            app.vad.threshold = float(value)
        if app.segmenter is not None and app.segmenter.vad is not None:
            app.segmenter.vad.threshold = float(value)
        log.info("vad threshold -> %.2f", value)

    def hook_segment_seconds(seconds: float) -> None:
        if app.segmenter is not None:
            app.segmenter.set_segment_seconds(seconds)
        log.info("segment length -> %s",
                 "off" if seconds <= 0 else f"{seconds:.0f}s")

    def hook_input_device(value) -> None:
        dev = value
        if isinstance(dev, str) and dev.isdigit():
            dev = int(dev)
        recorder._configured_device = dev if dev != "" else None
        recorder.input_device = recorder._configured_device
        if not recorder.refresh_default_device():
            log.warning("mic change saved while a recording was live - it "
                        "takes effect on the next idle refresh or restart")
        # Same rule as the micwatch on_switch: the calibrated noise floor
        # belongs to ONE mic and must not survive a swap (M9).
        hook_preprocess()
        log.info("mic %s", f"pinned to {dev!r}" if dev not in (None, "")
                 else "following the system default again")

    def hook_pill_size(value: float) -> None:
        # Live pill_size change WITHOUT tearing the overlay thread/root down
        # (the old rebuild crashed natively when a settings window was open
        # over the pill - it enqueues a rescale the overlay applies on its
        # own thread). Safe from any thread: it only puts on the command queue.
        if overlay is not None:
            overlay.set_pill_size(value)

    def hook_cleanup(on: bool) -> None:
        if bool(on) != app.cleanup_active:
            app.toggle_cleanup()

    def hook_restore_delay(seconds: float) -> None:
        app.restore_delay_s = seconds

    def hook_live_typing(on: bool) -> None:
        app.set_live_typing(on)
        log.info("live typing %s", "on" if on
                 else "off - hold to talk, paste on release (v1.1)")

    def hook_max_utterance(seconds: float) -> None:
        recorder.set_max_seconds(seconds)
        log.info("max utterance -> %s",
                 "unlimited" if seconds <= 0 else f"{seconds:.0f}s")

    # (hook_segment_seconds defined above, next to hook_vad)

    def hook_history(enabled: bool) -> None:
        if enabled and app.history is None:
            from localflow.desktop.history import HistoryStore
            app.history = HistoryStore()
        elif not enabled:
            app.history = None          # stop writing; file stays until
            log.info("history recording off")  # "Clear history" removes it

    def hook_replacements(rules: dict) -> None:
        app.replacements = dict(rules or {})
        log.info("dictionary now has %d rule(s)", len(app.replacements))

    def hook_inject_mode(mode: str) -> None:
        app.inject_mode = mode

    def hook_filter_hallucinations(enabled: bool) -> None:
        app.filter_hallucinations = bool(enabled)
        log.info("hallucination filter %s", "on" if enabled else "off")

    def hook_tts_speed(length_scale: float) -> None:
        if narrator is not None:
            narrator.set_length_scale(float(length_scale))
        log.info("read-aloud speed -> length_scale %.2f", length_scale)

    def hook_tts_volume(volume: float) -> None:
        if narrator is not None:
            narrator.set_volume(float(volume))
        log.info("read-aloud volume -> %.2f", volume)

    # Follow the Windows default mic, live (M8). Started inside startup_worker
    # once the mic is actually open; held in a ref so the teardown can stop it.
    # on_switch rebuilds the preprocessor: a calibrated noise floor belongs to
    # ONE mic and must not survive a hot-swap (M9 review finding).
    watcher_ref: dict = {"w": None}

    live_hooks = {
        "hotkey": hook_hotkey,
        "preprocess": hook_preprocess,
        "vad": hook_vad,
        "restore_delay": hook_restore_delay,
        "max_utterance": hook_max_utterance,
        "segment": hook_segment_seconds,
        "inject_mode": hook_inject_mode,
        "filter_hallucinations": hook_filter_hallucinations,
        "input_device": hook_input_device,
        "visibility": set_pill_visibility,
        "pill_size": hook_pill_size,
        "cleanup": hook_cleanup,
        "live_typing": hook_live_typing,
        "history": hook_history,
        "replacements": hook_replacements,
        "tts_speed": hook_tts_speed,
        "tts_volume": hook_tts_volume,
    }

    def apply_from_ui(changes: dict) -> set[str]:
        """Settings window Apply: validate the merged config, persist only
        the real deltas (comment-preserving), live-apply. Returns the keys
        that need a restart (the window shows the note)."""
        config.Config(**{**cfg.model_dump(), **changes})  # validate first
        delta = {k: v for k, v in changes.items()
                 if getattr(cfg, k, object()) != v}
        if not delta:
            return set()
        persist_settings(delta)
        return apply_settings(delta, cfg, live_hooks)

    def list_mics() -> list[tuple[str, object]]:
        """(label, input_device value). First entry = follow the default."""
        mics: list[tuple[str, object]] = [
            ("System default (follow Windows)", None)]
        try:
            import sounddevice as sd
            hostapis = sd.query_hostapis()
            for idx, dev in enumerate(sd.query_devices()):
                if (dev["max_input_channels"] >= 1 and
                        hostapis[dev["hostapi"]]["name"] == "Windows WASAPI"):
                    mics.append((str(dev["name"]), idx))
        except Exception:
            log.exception("mic enumeration failed")
        return mics

    latest_level = {"rms": 0.0}
    level_sinks.append(lambda rms: latest_level.__setitem__("rms", rms))

    def test_mic(on_done) -> bool:
        """Settings 'Test mic': ~3s capture through the real pipeline, no
        injection. Async (worker thread); False when a dictation or another
        mic session is live (claimed atomically - review found the
        check-then-spawn race with Calibrate and the hotkey)."""
        if not app.claim_mic_session():
            return False

        def run() -> None:
            try:
                # No timeout escape: warmup drives the engine without
                # holding engine_lock, so proceeding early could run the
                # engine from two threads. warm is set in a finally.
                app.warm.wait()
                recorder.start()
                time.sleep(3.0)
                audio, rate = recorder.stop()
                if len(audio) == 0:
                    on_done("(no audio captured - mic delivered nothing)")
                    return
                audio16 = ensure_16k_mono(audio, rate)
                with app.engine_lock:  # never race a real dictation
                    result = transcribe_audio(
                        audio16, app.engine, cleanup=None, vad=app.vad,
                        preprocess=app.preprocess)
                on_done(result.raw_text.strip() or "(silence)")
            except Exception as e:
                log.exception("mic test failed")
                try:
                    on_done(f"(mic test failed: {e})")
                except Exception:
                    pass
            finally:
                app.mic_test_active = False

        threading.Thread(target=run, daemon=True,
                         name="localflow-mic-test").start()
        return True

    def validate_hotkey(combo: str) -> None:
        """Raises ValueError on an unusable combo (settings capture box)."""
        keyboard.HotKey.parse(to_pynput_combo(combo))

    def calibrate_noise(on_done) -> bool:
        """Settings 'Calibrate room noise' (M9): record 2s of QUIET room,
        estimate the per-frequency floor, save it for the active mic and
        attach it live. Async; False while a dictation or another mic
        session is running (atomic claim, same as Test mic)."""
        if not app.claim_mic_session():
            return False

        def run() -> None:
            try:
                app.warm.wait()
                recorder.start()
                time.sleep(2.0)
                audio, rate = recorder.stop()
                if len(audio) == 0:
                    on_done("(no audio captured - mic delivered nothing)", None)
                    return
                audio16 = ensure_16k_mono(audio, rate)
                if app.vad is not None:
                    with app.engine_lock:  # vad is also used by the worker
                        speech = app.vad.trim(audio16)
                    if speech is not None:
                        on_done("(speech detected - stay quiet during "
                                "calibration and try again)", None)
                        return
                # Same high-pass as the live chain: the floor and the measured
                # level must both describe what reduce_noise actually sees.
                hp = high_pass(audio16)
                floor = estimate_noise_floor(hp)
                noise_rms = float(np.sqrt(np.mean(np.square(
                    hp.astype(np.float64)))))
                denoise, sensitivity = recommend_tuning(noise_rms)
                from localflow.desktop import noiseprofile
                noiseprofile.save_profile(floor, recorder.device_name)
                app.preprocess = build_preprocessor()  # attach the floor now
                import math
                db = 20.0 * math.log10(max(noise_rms, 1e-7))
                on_done(
                    f"Calibrated for {recorder.device_name} - room noise "
                    f"{db:.0f} dBFS, set denoise {denoise:.2f} / "
                    f"sensitivity {sensitivity:.2f}",
                    {"denoise_strength": denoise, "vad_threshold": sensitivity})
            except Exception as e:
                log.exception("noise calibration failed")
                try:
                    on_done(f"(calibration failed: {e})", None)
                except Exception:
                    pass
            finally:
                app.mic_test_active = False

        threading.Thread(target=run, daemon=True,
                         name="localflow-calibrate").start()
        return True

    def auto_calibrate_loop() -> None:
        """Keep the room-noise floor current while idle (Jeremy 4 Aug).
        Profile refresh ONLY - the tuning recommendation is logged, never
        applied, so the sliders stay his. Safe by construction:
        calibrate_noise claims the mic atomically (refused = skip), discards
        any sample the VAD hears speech in, and the idle guard keeps the 2s
        claim well clear of active dictation."""
        interval_s = cfg.auto_calibrate_minutes * 60.0
        idle_s = 300.0

        def done(msg: str, _tuning) -> None:
            log.info("auto-calibrate: %s", msg)

        while not quit_event.wait(interval_s):
            if time.monotonic() - app.last_mic_use < idle_s:
                continue                      # recently dictating: not idle
            if not calibrate_noise(done):
                continue                      # mic busy right now: next cycle

    if cfg.auto_calibrate_minutes > 0:
        threading.Thread(target=auto_calibrate_loop, daemon=True,
                         name="localflow-autocalibrate").start()
        log.info("idle room-noise recalibration every %.0f min",
                 cfg.auto_calibrate_minutes)

    def clear_noise_calibration() -> str:
        from localflow.desktop import noiseprofile
        removed = noiseprofile.clear_profile()
        app.preprocess = build_preprocessor()
        return "Calibration cleared" if removed else "No calibration saved"

    def calibration_status() -> str:
        from localflow.desktop import noiseprofile
        loaded = noiseprofile.load_profile()
        if loaded is None:
            return "No room-noise calibration saved"
        _floor, device, saved_at = loaded
        when = time.strftime("%d %b %H:%M", time.localtime(saved_at))
        return f"Calibrated for {device} ({when})"

    # -- M10: history + dictionary plumbing for the settings window -------
    def history_entries() -> list:
        """Newest first; [] when history is off."""
        return list(reversed(app.history.load())) if app.history else []

    def save_correction(ts: float, text: str) -> bool:
        return (app.history.set_correction(ts, text)
                if app.history is not None else False)

    def clear_history() -> None:
        from localflow.desktop.history import HistoryStore
        (app.history or HistoryStore()).clear()  # remove the file even if off

    def dictionary_suggestions() -> list[tuple[str, str, int]]:
        """Mined substitution rules not already in the dictionary."""
        if app.history is None:
            return []
        mined = mine_substitutions(app.history.corrections())
        existing = {k.lower() for k in cfg.replacements}
        return [(s, t, n) for s, t, n in mined if s.lower() not in existing]

    def wire_settings_ui(ov) -> None:
        """Give an overlay the settings-window factory (runs on ITS UI
        thread via the overlay command queue)."""
        try:
            from localflow.desktop import settings_ui
        except Exception:
            log.exception("settings window unavailable — the Settings "
                          "entries fall back to opening the toml")
            return
        ctx = settings_ui.SettingsContext(
            cfg=cfg,
            apply=apply_from_ui,
            list_mics=list_mics,
            mic_name=lambda: recorder.device_name,
            fallback_active=lambda: recorder.fallback_active,
            latest_level=lambda: latest_level["rms"],
            test_mic=test_mic,
            validate_hotkey=validate_hotkey,
            calibrate=calibrate_noise,
            clear_calibration=clear_noise_calibration,
            calibration_status=calibration_status,
            history_entries=history_entries,
            save_correction=save_correction,
            clear_history=clear_history,
            suggestions=dictionary_suggestions,
            reset_pill_position=reset_pill_position,
            open_config_file=lambda: config.open_settings_editor(settings_path),
            status_text=lambda: status_holder["text"],
            cleanup_available=app.cleanup_available,
            cleanup_active=lambda: app.cleanup_active,
        )
        ov.settings_factory = lambda root: settings_ui.open_settings(root, ctx)
        ov.on_settings = ov.open_settings

    def copy_last_text() -> str | None:
        """Pill menu 'Copy last dictation': put the last transcript back on the
        clipboard. Returns the text (for the pill's confirmation) or None when
        there is nothing yet."""
        text = app.last_text
        if not text:
            return None
        try:
            import pyperclip
            pyperclip.copy(text)
            log.info("copied last dictation to clipboard (%d chars)", len(text))
            return text
        except Exception:
            log.exception("copy last dictation failed")
            return None

    app.on_state = fan_out_state
    if overlay is not None:
        app.on_no_output = overlay.flash
        level_sinks.append(overlay.push_level)
        overlay.settings_path = settings_path
        overlay.on_toggle = app.toggle_recording
        overlay.on_copy_last = copy_last_text
        overlay.on_narrate = narrate_selection   # None if TTS off/voice missing
        overlay.on_quit = request_quit

        def request_restart_from_pill() -> None:
            # Pill "Restart" = tray "Restart": flag the relaunch, then run
            # the normal quit teardown, which spawns the fresh instance.
            log.info("restart requested from the overlay")
            restart_ref["want"] = True
            request_quit()

        overlay.on_restart = request_restart_from_pill
        wire_settings_ui(overlay)
        overlay.set_progress(0.05, "Starting")  # loading bar from the first frame

    ui_live_s = (_IMPORT_AGE_S or 0.0) + (time.perf_counter() - _T_MODULE)
    quit_hint = ("quit from the tray menu" if tray is not None
                 else "Ctrl+C here to quit")
    print(f"\nLocalFlow starting - hold {cfg.hotkey} and speak once it's ready. "
          f"{quit_hint}.\n"
          f"(Note: pasting into elevated/admin windows silently fails - FR-11.)",
          flush=True)
    log.info("UI live at %.1fs — models loading in the background", ui_live_s)

    def _progress(frac: float, label: str) -> None:
        if overlay is not None:
            overlay.set_progress(frac, label)

    def startup_worker() -> None:
        # Everything heavy, in order, with a loading bar (M11). The worker
        # thread is gated on app.warm, so engine/vad/preprocess are never
        # touched until this sets them and app.warm below.
        engine_build_ms = 0
        try:
            _progress(0.12, "Opening mic")
            # open_or_fallback errors ONLY when no mic on the machine opens; a
            # wedged/pinned mic falls back to a working one instead.
            err = recorder.open_or_fallback()
            if err is not None:
                log.error("no usable microphone: %s", err)
                _alert("LocalFlow could not open any microphone.\n"
                       "Plug one in and restart LocalFlow.")
                _progress(0.12, "No mic found")
            app.preprocess = build_preprocessor()
            if app.preprocess is not None:
                log.info("audio preprocessing: %s", app.preprocess.describe())
            try:                              # default-mic watcher, mic now open
                from localflow.desktop import micgain
                from localflow.desktop.micwatch import DefaultMicWatcher

                def on_mic_switch() -> None:
                    # auto-gain's cached level belongs to the OLD endpoint.
                    micgain.invalidate_cache()
                    hook_preprocess()

                w = DefaultMicWatcher(recorder, on_switch=on_mic_switch)
                w.start()
                watcher_ref["w"] = w
            except Exception:
                log.exception("default-mic watcher unavailable")

            _progress(0.30, "Loading model")
            t0 = time.perf_counter()
            engine = None
            if cfg.stt_model == "parakeet":
                # Transducer on CPU (docs/parakeet-spike.md): immune to
                # decode loops / silence hallucinations, frees the iGPU.
                # A load failure falls back to whisper - dictation must
                # never be dead over a missing model dir.
                try:
                    from localflow.core.parakeet import ParakeetEngine
                    engine = ParakeetEngine(cfg.parakeet_model_dir)
                except Exception:
                    log.exception("parakeet engine failed to load — "
                                  "falling back to whisper")
            if engine is None:
                engine = SttEngine(cfg.stt_model_dir, device=cfg.device,
                                   fallback_device=cfg.fallback_device,
                                   cache_dir=cfg.cache_dir,
                                   language=to_language_token(cfg.stt_language),
                                   initial_prompt=initial_prompt)
            engine_build_ms = int((time.perf_counter() - t0) * 1000)
            app.engine = engine
            status_holder["text"] = f"{engine.model_name} on {engine.device}"

            _progress(0.60, "Loading voice")
            app.vad = VadGate(threshold=cfg.vad_threshold)

            _progress(0.82, "Warming up")
            warmup(engine, app.vad, app.cleanup, app.preprocess)
        except Exception:
            log.exception("startup failed — dictation unavailable this session")
            _alert("LocalFlow failed to load the speech model.\n"
                   "See the log and restart LocalFlow.")
            _progress(1.0, "Load failed")
            app.warm.set()   # don't hang a queued dictation; it fails cleanly
            return
        app.warm.set()
        # Tier 1: arm the segmenter with its OWN gate (get_speech_timestamps
        # mutates Silero state, so it must never share the worker's app.vad).
        if app.segmenter is not None:
            try:
                app.segmenter.vad = VadGate(threshold=cfg.vad_threshold)
                app.segmenter.start()
            except Exception:
                log.exception("segmenter unavailable — long dictations run "
                              "in one shot on release")
        if app.livetyper is not None:
            app.livetyper.engine = app.engine
            app.livetyper.start()
        warm_s = (_IMPORT_AGE_S or 0.0) + (time.perf_counter() - _T_MODULE)
        log.info("startup breakdown: interpreter+imports %s, UI live at %.1fs, "
                 "engine build %dms, usable at %.1fs",
                 f"{_IMPORT_AGE_S:.1f}s" if _IMPORT_AGE_S is not None else "n/a",
                 ui_live_s, engine_build_ms, warm_s)
        print(f"LocalFlow ready on {app.engine.device} - hold {cfg.hotkey} "
              f"and speak.", flush=True)
        _progress(1.0, "Ready")
        if overlay is not None:
            overlay.set_state("idle")   # loading -> usable (the only two states)
        if cfg.cleanup_enabled and cleanup_loader is not None:
            print("Loading cleanup model (slower, cleaner mode)...", flush=True)
            app.preload_cleanup()

    threading.Thread(target=startup_worker, daemon=True,
                     name="localflow-startup").start()

    try:
        if tray is not None:
            tray.run()  # blocks the MAIN thread (Windows message loop)
        else:
            # Not the listener's join(): a live hotkey rebuild (M8) stops
            # the old listener, which must not look like a quit. The short
            # wait keeps Ctrl+C responsive on Windows.
            while not quit_event.wait(0.5):
                pass
    except KeyboardInterrupt:
        if tray is not None:
            tray.stop()
    finally:
        # Immediate, clean shutdown (Jeremy: quit must release EVERYTHING at
        # once so relaunching straight away never hits the single-instance
        # guard). Do the essential releases, flush the log, then hard-exit —
        # a normal return would wait on the overlay-thread join, OpenVINO's
        # GPU teardown and interpreter GC, any of which can keep the process
        # (and its named mutex) alive for a second or more.
        def _try(fn):
            try:
                fn()
            except Exception:
                pass
        _try(hotkey_ref["listener"].stop)
        if narrate_ref["listener"] is not None:
            _try(narrate_ref["listener"].stop)
        if narrator is not None:
            _try(narrator.stop)
        w = watcher_ref.get("w")
        if w is not None:
            _try(w.stop)
        if app.segmenter is not None:
            _try(app.segmenter.stop)
        if app.livetyper is not None:
            _try(app.livetyper.stop)
        _try(recorder.close)          # release the mic/PortAudio stream now
        summary = app.latency_summary()
        if summary:
            log.info("%s", summary)
        if restart_ref["want"]:
            # Tray "Restart": spawn the new instance, then die. Imports take
            # the child seconds to reach the single-instance check; our
            # os._exit releases the mutex in milliseconds — no race.
            import subprocess
            _try(lambda: subprocess.Popen(
                [sys.executable, "-c",
                 "from localflow.desktop.boot import main; main()"]
                + sys.argv[1:], close_fds=True))
            log.info("restarting — new instance spawned")
        log.info("bye — exiting immediately")
        logging.shutdown()            # flush the log file before the hard exit
        os._exit(0)                   # releases the mutex at once; no lingering
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
