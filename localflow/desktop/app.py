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
        self.cleanup_active = cleanup is not None
        # FR-2 hook: called with 'idle' | 'rec' | 'busy' (tray sets this).
        self.on_state = on_state
        self.latencies_ms: list[int] = []  # release-to-paste totals (AT-4)
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
        self.recorder.start()
        self._set_state("rec")
        # FR-2 indicator (tray dot; console line kept for --no-tray). ASCII
        # only: Windows consoles often run cp1252, which can't encode glyphs.
        print("[rec] recording - release to transcribe", flush=True)

    def on_hotkey_up(self) -> None:
        audio, rate = self.recorder.stop()
        self._set_state("busy")
        # Hand off immediately: transcribe + inject must stay off the
        # key-listener thread or held keys stop being tracked.
        self._work.put((audio, rate, time.perf_counter()))

    def toggle_recording(self) -> None:
        """Click-to-dictate (overlay): first call starts recording, second
        stops and transcribes. Runs on the overlay's Tk thread — both paths
        only flip the recorder and enqueue, same contract as the hotkey."""
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
        actually wanted. No-op (False) when no model is available at all."""
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
            if not self.cleanup_active:  # toggled back off mid-load
                close = getattr(engine, "close", None)
                if close is not None:
                    close()
                log.info("cleanup model loaded but toggled off mid-load — "
                         "discarded to free its memory")
            else:
                self.cleanup = engine
                log.info("cleanup model loaded — cleanup is ON")
        except Exception:
            log.exception("cleanup model failed to load — cleanup stays OFF")
            self.cleanup_active = False
        finally:
            self._cleanup_loading = False

    def _drain(self) -> None:
        while True:
            audio, rate, t_release = self._work.get()
            try:
                self._process(audio, rate, t_release)
            except Exception:
                log.exception("utterance failed — app keeps running")
            finally:
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
        audio16 = ensure_16k_mono(audio, rate)
        cleanup = self.cleanup if self.cleanup_active else None
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

    # The overlay goes up FIRST so launching gives instant feedback ("loading
    # models...") instead of a blank few seconds; it fails soft to no overlay.
    overlay = None
    if cfg.overlay_enabled:
        try:
            from localflow.desktop.overlay import WaveformOverlay
            overlay = WaveformOverlay(hotkey=cfg.hotkey)
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
    print("Loading models (GPU warm load is ~1-3s)...", flush=True)
    engine = SttEngine(cfg.stt_model_dir, device=cfg.device,
                       fallback_device=cfg.fallback_device,
                       cache_dir=cfg.cache_dir,
                       initial_prompt=initial_prompt)
    vad = VadGate()

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
            try:
                print("Loading cleanup model (slower, cleaner mode)...",
                      flush=True)
                cleanup = cleanup_loader()
            except Exception:
                log.exception("cleanup model failed to load — dictation "
                              "continues with raw transcripts only")
        else:
            log.info("cleanup model found — not loading it at startup; the "
                     "tray toggle loads it on demand")
    elif cfg.cleanup_enabled:
        log.warning("cleanup_enabled=true but the model dir %s does not "
                    "exist — cleanup unavailable", cfg.cleanup_model_dir)

    warmup(engine, vad, cleanup, preprocess)
    recorder = StreamRecorder(input_device=input_device)
    # Open the mic stream now, not on key-down: opening costs ~300ms and was
    # eating the first spoken words. Idle audio is discarded after 0.3s.
    recorder.open()

    # No pinned mic -> follow the Windows default, live (hot-swaps the
    # stream when the default capture endpoint changes).
    watcher = None
    if input_device is None:
        try:
            from localflow.desktop.micwatch import DefaultMicWatcher
            watcher = DefaultMicWatcher(recorder)
            watcher.start()
            log.info("following the system default mic (pin one via "
                     "input_device in localflow.toml to disable)")
        except Exception:
            log.exception("default-mic watcher unavailable — the mic is "
                          "fixed for this session")
            watcher = None

    app = LocalFlowApp(engine, vad, recorder, inject_mode=cfg.inject_mode,
                       cleanup=cleanup, cleanup_loader=cleanup_loader,
                       restore_delay_s=cfg.restore_delay_ms / 1000.0,
                       preprocess=preprocess)
    app.start_worker()
    hotkey = PushToTalkListener(cfg.hotkey, app.on_hotkey_down, app.on_hotkey_up)
    hotkey.start()

    settings_path = cfg_path or Path.cwd() / config.CONFIG_FILENAME
    tray = None
    if not args.no_tray:
        try:
            from localflow.desktop.tray import TrayIcon
            tray = TrayIcon(f"{engine.model_name} on {engine.device}", settings_path,
                            cleanup_get=(lambda: app.cleanup_active)
                            if app.cleanup_available else None,
                            cleanup_toggle=app.toggle_cleanup)
        except Exception:
            log.exception("tray unavailable — falling back to console-only mode")
            tray = None

    def fan_out_state(state: str) -> None:
        if tray is not None:
            tray.set_state(state)
        if overlay is not None:
            overlay.set_state(state)

    def request_quit() -> None:
        # Overlay "Quit": unblock whichever loop owns the main thread.
        log.info("quit requested from the overlay")
        if tray is not None:
            tray.stop()
        else:
            hotkey.stop()

    app.on_state = fan_out_state
    if overlay is not None:
        recorder.on_level = overlay.push_level
        overlay.settings_path = settings_path
        overlay.on_toggle = app.toggle_recording
        overlay.on_quit = request_quit
        overlay.set_state("ready")  # flashes the hint, then idles to the chip

    quit_hint = ("quit from the tray menu" if tray is not None
                 else "Ctrl+C here to quit")
    print(f"\nLocalFlow ready on {engine.device} - hold {cfg.hotkey} and speak; "
          f"release to paste into the focused app. {quit_hint}.\n"
          f"(Note: pasting into elevated/admin windows silently fails - FR-11.)",
          flush=True)
    log.info("ready on %s - hotkey %s, inject_mode %s",
             engine.device, cfg.hotkey, cfg.inject_mode)

    try:
        if tray is not None:
            tray.run()  # blocks the MAIN thread (Windows message loop)
        else:
            hotkey.join()
    except KeyboardInterrupt:
        if tray is not None:
            tray.stop()
    finally:
        hotkey.stop()
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
