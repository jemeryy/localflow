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
import gc
import logging
import os
import queue
import sys
import threading
import time
from pathlib import Path

import numpy as np
from pynput import keyboard

from localflow.core import (
    AudioPreprocessor,
    SttEngine,
    VadGate,
    ensure_16k_mono,
    transcribe_audio,
)
from localflow.desktop import config
from localflow.desktop.capture import StreamRecorder
from localflow.desktop.inject import inject_text

log = logging.getLogger("localflow.desktop.app")


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


class PushToTalkListener:
    """Hold-to-record hotkey (FR-1): on_start fires when the full combo is
    down, on_stop when any key of it is released. Push-to-talk, not toggle."""

    def __init__(self, combo: str, on_start, on_stop):
        self._combo = set(keyboard.HotKey.parse(to_pynput_combo(combo)))
        self._on_start = on_start
        self._on_stop = on_stop
        self._pressed: set = set()
        self._active = False
        self._listener = keyboard.Listener(
            on_press=self._handle_press, on_release=self._handle_release
        )

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
                 cleanup_loader=None):
        self.engine = engine
        self.vad = vad
        self.recorder = recorder
        self.inject_mode = inject_mode
        self.restore_delay_s = restore_delay_s
        # Audio cleanup (high-pass / denoise / normalize) before STT. None
        # means raw mic audio goes straight to the pipeline.
        self.preprocess = preprocess
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
        # A refused key-down must also swallow its key-up: the listener
        # tracks the combo as active regardless, and an unpaired
        # on_hotkey_up would stop (steal) the mic test's recording.
        self._hotkey_suppressed = False
        self._work: queue.Queue = queue.Queue()
        self._worker = threading.Thread(target=self._drain, daemon=True)

    def _set_state(self, state: str) -> None:
        if self.on_state is None:
            return
        try:
            self.on_state(state)
        except Exception:
            log.exception("state callback failed — app keeps running")

    def on_hotkey_down(self) -> None:
        if self.mic_test_active:
            # The settings "Test mic" owns the recorder for ~3s; a dictation
            # starting mid-test would truncate both captures (M8).
            self._hotkey_suppressed = True
            print("[--] mic test in progress - try again in a moment",
                  flush=True)
            return
        self.recorder.start()
        self._set_state("rec")
        # FR-2 indicator (tray dot; console line kept for --no-tray). ASCII
        # only: Windows consoles often run cp1252, which can't encode glyphs.
        print("[rec] recording - release to transcribe", flush=True)

    def on_hotkey_up(self) -> None:
        if self._hotkey_suppressed:
            self._hotkey_suppressed = False  # the matching refused key-down
            return
        audio, rate = self.recorder.stop()
        self._set_state("busy")
        # Hand off immediately: transcribe + inject must stay off the
        # key-listener thread or held keys stop being tracked.
        self._work.put((audio, rate, time.perf_counter()))

    def toggle_recording(self) -> None:
        """Click-to-dictate (overlay): first call starts recording, second
        stops and transcribes. Runs on the overlay's Tk thread — both paths
        only flip the recorder and enqueue, same contract as the hotkey."""
        if self.mic_test_active:
            # recorder.recording is True DURING the mic test - a pill click
            # must not stop (steal) the test capture.
            print("[--] mic test in progress - try again in a moment",
                  flush=True)
            return
        if self.recorder.recording:
            self.on_hotkey_up()
        else:
            self.on_hotkey_down()

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
        while True:
            audio, rate, t_release = self._work.get()
            try:
                self._process(audio, rate, t_release)
            except Exception:
                log.exception("utterance failed — app keeps running")
            finally:
                # Back to idle only when nothing else is in flight: another
                # dictation may be queued behind this one (likelier now that
                # warmup can park the first), or the user may already be
                # recording the next - never stomp those indicators.
                if self._work.empty() and not (
                        self.recorder is not None and self.recorder.recording):
                    self._set_state("idle")

    def _process(self, audio: np.ndarray, rate: int, t_release: float) -> None:
        if len(audio) == 0:
            # Zero frames from an open stream = wedged mic endpoint (seen
            # live 3 Jul: Samsung USB delivered nothing to ANY process until
            # replugged). Say so loudly instead of a silent no-paste.
            log.warning("recording captured 0 frames from %r — the mic "
                        "delivered no audio. If this repeats, replug the mic "
                        "or change the Windows default input device.",
                        self.recorder.device_name)
            print("[--] nothing captured - mic delivered no audio", flush=True)
            return
        if not self.warm.is_set():
            # A dictation in the first seconds after launch waits for the
            # background warmup instead of racing it - the engine must NEVER
            # be driven from two threads, so there is no timeout escape here.
            # The warmup thread sets the event in a `finally`, so this wait
            # always ends; the utterance runs later, it is never dropped.
            log.info("dictation arrived before warmup finished - waiting")
            print("[..] still warming up - transcribing shortly", flush=True)
            self.warm.wait()
        audio16 = ensure_16k_mono(audio, rate)
        cleanup = self.cleanup if self.cleanup_active else None
        with self.engine_lock:
            result = transcribe_audio(audio16, self.engine, cleanup=cleanup,
                                      vad=self.vad, preprocess=self.preprocess)
        # clean_text is None when cleanup is off or failed — raw always wins
        # over no paste (FR-8: cleanup can never block dictation).
        text = result.clean_text if result.clean_text is not None else result.raw_text
        injected = inject_text(text, mode=self.inject_mode,
                               restore_delay_s=self.restore_delay_s)
        total_ms = int((time.perf_counter() - t_release) * 1000)
        if injected:
            self.latencies_ms.append(total_ms)
            cleanup_note = (f", cleanup {result.cleanup_ms}ms"
                            if result.cleanup_ms is not None else "")
            # FR-7 per-utterance log + release-to-paste total (feeds AT-4).
            print(f"[ok] pasted {len(text)} chars in {total_ms}ms "
                  f"(stt {result.stt_ms}ms on {result.device}{cleanup_note}, "
                  f"n={len(self.latencies_ms)}): {text!r}", flush=True)
        else:
            print(f"[--] silence - nothing pasted ({total_ms}ms)", flush=True)

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
    "device", "fallback_device", "stt_model_dir", "cleanup_model_dir",
    "cache_dir", "hot_words", "overlay_enabled",
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
        elif key == "inject_mode":
            hooks["inject_mode"](value)
        elif key == "input_device":
            hooks["input_device"](value)
        elif key == "overlay_visibility":
            hooks["visibility"](value)
        elif key == "pill_size":
            hooks["pill_size"](value)
        elif key == "cleanup_enabled":
            hooks["cleanup"](value)
        elif key == "overlay_position":
            pass                        # owned by the drag/reset paths
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

    # Build everything ONCE at startup, never per utterance (FR-14).
    # M6: the SttEngine is the ONE thing dictation cannot start without, so
    # it builds first and everything slow-but-optional (VadGate, warmup, the
    # cleanup LLM preload) moves to a background thread. Hotkey target: live
    # in <=8s instead of 14-19s.
    print("Loading models (GPU warm load is ~1-3s)...", flush=True)
    # The mic stream open costs ~1s of PortAudio/WASAPI work and touches
    # nothing the engine build needs — overlap the two (M6). Opening now,
    # not on key-down, also keeps the M3 fix: a per-keydown open ate the
    # first spoken words. Idle audio is discarded after 0.3s.
    recorder = StreamRecorder(input_device=input_device)
    mic_open_err: list[BaseException] = []

    def open_recorder() -> None:
        try:
            recorder.open()
        except BaseException as e:      # re-raised on the main thread below
            mic_open_err.append(e)

    mic_open = threading.Thread(target=open_recorder, daemon=True,
                                name="localflow-mic-open")
    mic_open.start()

    t_engine0 = time.perf_counter()
    engine = SttEngine(cfg.stt_model_dir, device=cfg.device,
                       fallback_device=cfg.fallback_device,
                       cache_dir=cfg.cache_dir,
                       initial_prompt=initial_prompt)
    engine_build_ms = int((time.perf_counter() - t_engine0) * 1000)

    # Audio cleanup before STT (FR-13): high-pass + gentle spectral denoise
    # and loudness normalization. Helps the quiet/noisy built-in mic array
    # most; toggle in localflow.toml if it ever costs accuracy.
    preprocess = None
    if cfg.denoise_enabled or cfg.normalize_audio:
        preprocess = AudioPreprocessor(denoise=cfg.denoise_enabled,
                                       normalize=cfg.normalize_audio,
                                       denoise_strength=cfg.denoise_strength)
        log.info("audio preprocessing: %s", preprocess.describe())

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
                                   cache_dir=cfg.cache_dir)
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

    mic_open.join()
    if mic_open_err:
        raise mic_open_err[0]

    # Follow the Windows default mic, live. The watcher now always runs
    # (M8: the mic can be pinned/unpinned from the settings window without
    # a restart); check_once() itself no-ops the switch while pinned and
    # keeps the heal tick either way.
    watcher = None
    try:
        from localflow.desktop.micwatch import DefaultMicWatcher
        watcher = DefaultMicWatcher(recorder)
        watcher.start()
        if input_device is None:
            log.info("following the system default mic (pin one in "
                     "Settings or localflow.toml to disable)")
    except Exception:
        log.exception("default-mic watcher unavailable — the mic is "
                      "fixed for this session")
        watcher = None

    # vad=None: the VadGate arrives from the warmup thread below; the worker
    # waits on app.warm before its first transcribe (never crashes or drops).
    app = LocalFlowApp(engine, None, recorder, inject_mode=cfg.inject_mode,
                       cleanup=cleanup, cleanup_loader=cleanup_loader,
                       restore_delay_s=cfg.restore_delay_ms / 1000.0,
                       preprocess=preprocess)
    app.start_worker()
    # Held in a ref so the settings window can rebuild it live (M8).
    hotkey_ref = {"listener": PushToTalkListener(
        cfg.hotkey, app.on_hotkey_down, app.on_hotkey_up)}
    hotkey_ref["listener"].start()

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

    tray = None
    if not args.no_tray:
        try:
            from localflow.desktop.tray import TrayIcon
            tray = TrayIcon(f"{engine.model_name} on {engine.device}", settings_path,
                            cleanup_get=(lambda: app.cleanup_active)
                            if app.cleanup_available else None,
                            cleanup_toggle=app.toggle_cleanup,
                            visibility_get=(lambda: vis_state["mode"])
                            if overlay is not None else None,
                            visibility_set=set_pill_visibility
                            if overlay is not None else None,
                            reset_pill_position=reset_pill_position
                            if overlay is not None else None,
                            open_settings=open_settings_entry)
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
        new = PushToTalkListener(combo, app.on_hotkey_down, app.on_hotkey_up)
        new.start()
        hotkey_ref["listener"] = new
        old.stop()
        log.info("hotkey rebuilt live -> %s", combo)

    def hook_preprocess() -> None:
        if cfg.denoise_enabled or cfg.normalize_audio:
            app.preprocess = AudioPreprocessor(
                denoise=cfg.denoise_enabled, normalize=cfg.normalize_audio,
                denoise_strength=cfg.denoise_strength)
            log.info("audio preprocessing now: %s", app.preprocess.describe())
        else:
            app.preprocess = None
            log.info("audio preprocessing now: off")

    def hook_input_device(value) -> None:
        dev = value
        if isinstance(dev, str) and dev.isdigit():
            dev = int(dev)
        recorder._configured_device = dev if dev != "" else None
        recorder.input_device = recorder._configured_device
        recorder.refresh_default_device()
        log.info("mic %s", f"pinned to {dev!r}" if dev not in (None, "")
                 else "following the system default again")

    def hook_pill_size(_value: float) -> None:
        # The pill's scale is baked into its UI thread at build time; a
        # live change means tearing that thread down and rebuilding it.
        # Must NOT run on the overlay's own thread (close() joins it) —
        # the settings window calls this via its rebuild-worker thread.
        rebuild_overlay()

    def hook_cleanup(on: bool) -> None:
        if bool(on) != app.cleanup_active:
            app.toggle_cleanup()

    def hook_restore_delay(seconds: float) -> None:
        app.restore_delay_s = seconds

    def hook_inject_mode(mode: str) -> None:
        app.inject_mode = mode

    live_hooks = {
        "hotkey": hook_hotkey,
        "preprocess": hook_preprocess,
        "restore_delay": hook_restore_delay,
        "inject_mode": hook_inject_mode,
        "input_device": hook_input_device,
        "visibility": set_pill_visibility,
        "pill_size": hook_pill_size,
        "cleanup": hook_cleanup,
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
        injection. Async (worker thread); False when a dictation is live."""
        if recorder.recording:
            return False

        def run() -> None:
            app.mic_test_active = True     # hotkey presses refuse mid-test
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
            reset_pill_position=reset_pill_position,
            open_config_file=lambda: config.open_settings_editor(settings_path),
            status_text=f"{engine.model_name} on {engine.device}",
            cleanup_available=app.cleanup_available,
            cleanup_active=lambda: app.cleanup_active,
        )
        ov.settings_factory = lambda root: settings_ui.open_settings(root, ctx)
        ov.on_settings = ov.open_settings

    def rebuild_overlay() -> None:
        """pill_size live-apply: tear down the pill's UI thread and build a
        fresh one at the new scale. NEVER call from the overlay UI thread
        (close() joins it) — the settings window uses a worker thread."""
        nonlocal overlay
        old = overlay
        if old is not None:
            try:
                level_sinks.remove(old.push_level)
            except ValueError:
                pass
            old.close()
        from localflow.desktop.overlay import WaveformOverlay
        overlay = WaveformOverlay(hotkey=cfg.hotkey,
                                  visibility=vis_state["mode"],
                                  position=cfg.overlay_position,
                                  pill_size=cfg.pill_size)
        overlay.on_position_change = save_pill_position
        overlay.settings_path = settings_path
        overlay.on_toggle = app.toggle_recording
        overlay.on_quit = request_quit
        wire_settings_ui(overlay)
        level_sinks.append(overlay.push_level)
        overlay.set_state("idle")
        log.info("pill rebuilt at size %.2f", cfg.pill_size)

    app.on_state = fan_out_state
    if overlay is not None:
        level_sinks.append(overlay.push_level)
        overlay.settings_path = settings_path
        overlay.on_toggle = app.toggle_recording
        overlay.on_quit = request_quit
        wire_settings_ui(overlay)
        overlay.set_state("warming")  # dictation works; warmup still running

    hotkey_live_s = (_IMPORT_AGE_S or 0.0) + (time.perf_counter() - _T_MODULE)
    quit_hint = ("quit from the tray menu" if tray is not None
                 else "Ctrl+C here to quit")
    print(f"\nLocalFlow ready on {engine.device} - hold {cfg.hotkey} and speak; "
          f"release to paste into the focused app. {quit_hint}.\n"
          f"(Warming up in the background - the first dictation may run a "
          f"little slower.)\n"
          f"(Note: pasting into elevated/admin windows silently fails - FR-11.)",
          flush=True)
    log.info("hotkey live on %s at %.1fs - hotkey %s, inject_mode %s "
             "(warmup continues in the background)",
             engine.device, hotkey_live_s, cfg.hotkey, cfg.inject_mode)

    def warm_in_background() -> None:
        # VadGate build + warm pass off the critical path (M6). The worker
        # waits on app.warm, so the engine is never used from two threads.
        try:
            app.vad = VadGate()
            warmup(engine, app.vad, app.cleanup, preprocess)
        except Exception:
            log.exception("background warmup failed — dictation continues "
                          "unwarmed (first utterance runs slower)")
        finally:
            app.warm.set()
        warm_s = (_IMPORT_AGE_S or 0.0) + (time.perf_counter() - _T_MODULE)
        imports_note = (f"{_IMPORT_AGE_S:.1f}s" if _IMPORT_AGE_S is not None
                        else "n/a")
        log.info("startup breakdown: interpreter+imports %s, engine build "
                 "%dms, hotkey live at %.1fs, fully warm at %.1fs",
                 imports_note, engine_build_ms, hotkey_live_s, warm_s)
        if overlay is not None:
            # Unconditional: the overlay's own queue ordering decides — its
            # apply() skips "ready" while rec/busy/menu is showing, so this
            # can never clobber an in-flight dictation, and a queued-but-
            # unprocessed "warming" can never strand the pill.
            overlay.set_state("ready")  # flash the hint, then idle chip
        if cfg.cleanup_enabled and cleanup_loader is not None:
            # cleanup_enabled=true preloads the LLM here, AFTER warm is set,
            # so the multi-second load never delays dictation readiness.
            # Same lock as the tray toggle - no double load, and a user
            # toggle during warmup always wins over the config preload.
            print("Loading cleanup model (slower, cleaner mode)...", flush=True)
            app.preload_cleanup()

    threading.Thread(target=warm_in_background, daemon=True,
                     name="localflow-warmup").start()

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
        hotkey_ref["listener"].stop()
        if watcher is not None:
            watcher.stop()
        recorder.close()
        if overlay is not None:
            overlay.close()

    summary = app.latency_summary()
    if summary:
        print(summary, flush=True)
        log.info("%s", summary)
    print("bye")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
