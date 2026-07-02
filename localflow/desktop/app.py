"""LocalFlow push-to-talk app (M3): hold the hotkey, speak, release, paste.

Run: `localflow` (console script) or `python -m localflow.desktop.app`.
Ctrl+C in the console quits. Tray UI and the config file arrive in M4;
for now everything is a CLI flag with PRD defaults.

Threading: pynput's listener callbacks must return fast, and the sounddevice
callback runs on the PortAudio thread — so key-up only enqueues the captured
audio, and a single worker thread does resample -> transcribe -> inject.
"""

from __future__ import annotations

import argparse
import logging
import queue
import sys
import threading
import time

import numpy as np
from pynput import keyboard

from localflow.core import SttEngine, VadGate, ensure_16k_mono, transcribe_audio
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
                 recorder: StreamRecorder, inject_mode: str = "paste"):
        self.engine = engine
        self.vad = vad
        self.recorder = recorder
        self.inject_mode = inject_mode
        self._work: queue.Queue = queue.Queue()
        self._worker = threading.Thread(target=self._drain, daemon=True)

    def on_hotkey_down(self) -> None:
        self.recorder.start()
        # FR-2 indicator (console for M3, tray in M4). ASCII only: Windows
        # consoles often run cp1252, which can't encode fancy glyphs.
        print("[rec] recording - release to transcribe", flush=True)

    def on_hotkey_up(self) -> None:
        audio, rate = self.recorder.stop()
        # Hand off immediately: transcribe + inject must stay off the
        # key-listener thread or held keys stop being tracked.
        self._work.put((audio, rate, time.perf_counter()))

    def _drain(self) -> None:
        while True:
            audio, rate, t_release = self._work.get()
            try:
                self._process(audio, rate, t_release)
            except Exception:
                log.exception("utterance failed — app keeps running")

    def _process(self, audio: np.ndarray, rate: int, t_release: float) -> None:
        if len(audio) == 0:
            print("[--] nothing captured", flush=True)
            return
        audio16 = ensure_16k_mono(audio, rate)
        result = transcribe_audio(audio16, self.engine, vad=self.vad)
        injected = inject_text(result.raw_text, mode=self.inject_mode)
        total_ms = int((time.perf_counter() - t_release) * 1000)
        if injected:
            # FR-7 per-utterance log + release-to-paste total (feeds AT-4).
            print(f"[ok] pasted {len(result.raw_text)} chars in {total_ms}ms "
                  f"(stt {result.stt_ms}ms on {result.device}): "
                  f"{result.raw_text!r}", flush=True)
        else:
            print(f"[--] silence - nothing pasted ({total_ms}ms)", flush=True)

    def start_worker(self) -> None:
        self._worker.start()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default="models/whisper-turbo-ov")
    ap.add_argument("--device", default="GPU", help="GPU | NPU | CPU")
    ap.add_argument("--fallback", default="CPU")
    ap.add_argument("--cache", default="models/ov_cache",
                    help="OpenVINO compile cache dir")
    ap.add_argument("--hotkey", default="alt+`",
                    help="hold-to-talk combo, e.g. alt+`, ctrl+space, f9")
    ap.add_argument("--inject-mode", default="paste", choices=["paste", "type"],
                    help="FR-12: paste (clipboard+Ctrl+V) or type (keystrokes)")
    ap.add_argument("--input-device", default=None,
                    help="sounddevice input device index or name substring")
    args = ap.parse_args(argv)

    # Transcripts echoed to the console may contain characters the active
    # codepage can't encode (cp1252) — degrade to '?' rather than crash.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")

    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)-7s %(name)s: %(message)s")

    input_device: int | str | None = args.input_device
    if input_device is not None and input_device.isdigit():
        input_device = int(input_device)

    # Build everything ONCE at startup, never per utterance (FR-14).
    print("Loading models (GPU warm load is ~1-3s)...", flush=True)
    engine = SttEngine(args.model, device=args.device,
                       fallback_device=args.fallback, cache_dir=args.cache)
    vad = VadGate()
    recorder = StreamRecorder(input_device=input_device)
    # Open the mic stream now, not on key-down: opening costs ~300ms and was
    # eating the first spoken words. Idle audio is discarded after 0.3s.
    recorder.open()

    app = LocalFlowApp(engine, vad, recorder, inject_mode=args.inject_mode)
    app.start_worker()
    hotkey = PushToTalkListener(args.hotkey, app.on_hotkey_down, app.on_hotkey_up)
    hotkey.start()

    print(f"\nLocalFlow ready on {engine.device} - hold {args.hotkey} and speak; "
          f"release to paste into the focused app. Ctrl+C here to quit.\n"
          f"(Note: pasting into elevated/admin windows silently fails - FR-11.)",
          flush=True)
    try:
        hotkey.join()
    except KeyboardInterrupt:
        print("\nbye")
        hotkey.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
