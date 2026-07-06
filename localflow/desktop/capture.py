"""Mic capture via sounddevice: fixed-duration (M2) and push-to-talk (M3)."""

from __future__ import annotations

import logging
import threading
import time

import numpy as np
import sounddevice as sd

log = logging.getLogger("localflow.desktop.capture")

MAX_UTTERANCE_S = 60.0  # FR-3 safety cap


def _find_live_input(exclude_name: str, listen_s: float = 1.5):
    """Probe WASAPI input devices and return (index, name) of the first one
    that actually delivers audio frames, or None if every mic is silent.

    Found live 3 Jul: a wedged endpoint OPENS fine but never fires the
    callback, so "did a frame arrive" is the only trustworthy liveness test.
    Opening a wedged endpoint can block for ~10s+, so only call this off the
    hot path (the micwatch thread). Names are matched by prefix because MME
    truncates device names to 31 chars while WASAPI does not.
    """
    try:
        hostapis = sd.query_hostapis()
        devices = sd.query_devices()
    except Exception:
        log.debug("device enumeration failed", exc_info=True)
        return None
    for idx, dev in enumerate(devices):
        try:
            if dev["max_input_channels"] < 1:
                continue
            if hostapis[dev["hostapi"]]["name"] != "Windows WASAPI":
                continue
            name = str(dev["name"])
            if name.startswith(exclude_name) or exclude_name.startswith(name):
                continue  # the wedged device (or its truncated MME twin)
            got_frames = threading.Event()

            def _cb(indata, frames, time_info, status, _got=got_frames):
                _got.set()

            stream = sd.InputStream(
                samplerate=int(dev["default_samplerate"]), channels=1,
                dtype="float32", device=idx, callback=_cb)
            try:
                stream.start()
                alive = got_frames.wait(listen_s)
            finally:
                stream.stop()
                stream.close()
            if alive:
                return idx, name
            log.info("mic probe: %r opened but delivered no frames", name)
        except Exception:
            log.debug("mic probe failed for device %d", idx, exc_info=True)
    return None


def record_audio(
    seconds: float, input_device: int | str | None = None
) -> tuple[np.ndarray, int]:
    """Blocking fixed-length capture at the device's native rate (FR-3).

    Returns (float32 mono 1-D array, sample_rate). Feed through
    localflow.core.ensure_16k_mono before transcription — capturing at the
    native rate and resampling ourselves is deterministic across drivers,
    unlike asking WASAPI for 16kHz directly.
    """
    if seconds > MAX_UTTERANCE_S:
        log.warning("capping recording at %.0fs (asked %.0fs)", MAX_UTTERANCE_S, seconds)
        seconds = MAX_UTTERANCE_S

    info = sd.query_devices(input_device, kind="input")
    rate = int(info["default_samplerate"])
    log.info("recording %.1fs from %r @ %dHz", seconds, info["name"], rate)

    frames = int(seconds * rate)
    buf = sd.rec(frames, samplerate=rate, channels=1, dtype="float32",
                 device=input_device)
    sd.wait()
    return buf[:, 0].copy(), rate


class StreamRecorder:
    """Push-to-talk capture: start() on key-down, stop() on key-up (FR-1).

    The input stream stays open for the recorder's whole life (open it once
    at app startup): opening a WASAPI stream costs ~300ms, which used to eat
    the first spoken words. While idle, the callback keeps only a short
    pre-roll ring (`preroll_s`, then discarded — nothing is retained or sent
    anywhere, per G2); start() seeds the recording with that pre-roll so
    speech that begins right at (or a hair before) key-down is captured.
    stop() returns (audio, rate) ready for localflow.core.ensure_16k_mono.
    Frames past the 60s cap are discarded (FR-3).
    """

    def __init__(
        self,
        input_device: int | str | None = None,
        max_seconds: float = MAX_UTTERANCE_S,
        preroll_s: float = 0.3,
    ):
        self.input_device = input_device
        # What the user configured (None = follow the Windows default).
        # input_device itself may be temporarily overridden by the fallback
        # hunt below; every refresh restores the configured value first.
        self._configured_device = input_device
        self._fallback_active = False
        self._heal_streak = 0
        self.max_seconds = max_seconds
        self.preroll_s = preroll_s
        # A pinned device can be invalid at startup: a USB headset that
        # reconnected on a different index, or whose input endpoint vanished so
        # the index now points at its OUTPUT (sd raises "Not an input device").
        # Fall back to the system default so the app always starts - the user
        # can re-pick the mic in Settings. Only a machine with no usable input
        # device at all (default resolve also fails) is fatal.
        try:
            self._resolve_device()
        except Exception as e:
            log.warning("configured mic %r is not a usable input device (%s) - "
                        "following the system default instead; re-pick it in "
                        "Settings", self.input_device, e)
            self.input_device = None
            self._fallback_active = True
            self._resolve_device()
        self._lock = threading.Lock()
        self._reopen_lock = threading.Lock()  # refresh vs start() races
        self._chunks: list[np.ndarray] = []
        self._preroll: list[np.ndarray] = []
        self._frames = 0
        self._capped = False
        self._recording = False
        self._stream: sd.InputStream | None = None
        # Stale-stream watchdog (found live 3 Jul: a wedged WASAPI endpoint
        # left the stream "open" but delivering zero frames — every dictation
        # captured 0.00s). _last_frame is stamped by every callback; if an
        # open stream goes silent for seconds, heal_if_stale() reopens it.
        self._last_frame = 0.0
        self._last_heal = 0.0
        # Optional UI hook: called with the chunk RMS (float) while recording
        # only — feeds the waveform overlay. Set it before recording starts.
        self.on_level = None

    def _resolve_device(self) -> None:
        info = sd.query_devices(self.input_device, kind="input")
        self.device_name = str(info["name"])
        self.rate = int(info["default_samplerate"])
        self._max_frames = int(self.max_seconds * self.rate)
        self._preroll_frames = int(self.preroll_s * self.rate)

    @property
    def recording(self) -> bool:
        return self._recording

    @property
    def fallback_active(self) -> bool:
        """True while recording from a hunted-down working mic instead of
        the configured/default one (which was wedged)."""
        return self._fallback_active

    def refresh_default_device(self) -> bool:
        """Re-resolve the input device and reopen the stream on it.

        PortAudio snapshots the device list (and the default device) at
        init, so a changed Windows default mic is INVISIBLE to an already
        initialized PortAudio — it must be terminated and reinitialized
        with no open streams. Call only while not recording; returns False
        if a recording was in progress. Costs ~300ms, so callers should do
        this off the key-down path (the micwatch thread does it while idle).
        """
        with self._reopen_lock:
            if self._recording:
                return False
            self.close()
            sd._terminate()
            sd._initialize()
            # Always aim back at what the user configured — a refresh is how
            # a fallback ends (e.g. the replugged headset became the default
            # again, or the wedged default endpoint changed).
            self.input_device = self._configured_device
            self._fallback_active = False
            self._resolve_device()
            self.open()
        return True

    def open(self) -> None:
        """Open (or reopen) the persistent stream. Idempotent; call at startup
        so key-down never pays the stream-open latency."""
        if self._stream is not None:
            if self._stream.active:
                return
            self._stream.close()
            self._stream = None
        self._stream = sd.InputStream(
            samplerate=self.rate, channels=1, dtype="float32",
            device=self.input_device, callback=self._on_chunk,
        )
        self._stream.start()
        self._last_frame = time.monotonic()  # fresh stream is not yet stale
        log.info("mic stream open (%r @ %dHz, %.1fs pre-roll)",
                 self.device_name, self.rate, self._preroll_frames / self.rate)

    def open_or_fallback(self) -> BaseException | None:
        """Startup open that never kills the app. Try the configured/default
        device; if it won't OPEN (a wedged USB headset raises a PortAudio host
        error, not silence), hunt for any input device that delivers audio and
        record from that with fallback_active set, so the user can still reach
        Settings to fix the pin. Returns the original error only when NOTHING
        on the machine opens (truly no mic) - that stays fatal.

        Unlike heal_if_stale, this DOES fall back even for a pinned device: a
        dead app the user can't fix without hand-editing the toml is worse than
        a loud fallback they can see in the pill and Settings.
        """
        try:
            self.open()
            return None
        except BaseException as first:
            log.warning("configured mic %r failed to open (%s) - hunting for a "
                        "working input device", self.device_name, first)
            try:
                self.close()
            except Exception:
                pass
            # PortAudio snapshots the device list at init; a device that went
            # bad after init needs a reinit before the probe sees reality.
            try:
                sd._terminate()
                sd._initialize()
            except Exception:
                log.debug("portaudio reinit failed", exc_info=True)
            found = _find_live_input(exclude_name=self.device_name)
            if found is None:
                return first    # no working mic anywhere - let startup fail
            idx, name = found
            self.input_device = idx
            self._fallback_active = True
            self._resolve_device()
            try:
                self.open()
            except BaseException as second:
                return second
            log.warning(
                "FALLBACK MIC at startup: the configured mic is unavailable - "
                "recording from %r instead. Replug it or pick a mic in "
                "Settings to switch back.", name)
            return None

    def close(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    def stale(self, threshold_s: float = 2.0) -> bool:
        """True when the stream claims to be open but no audio callback has
        fired for `threshold_s` — a wedged device (idle callbacks normally
        arrive every few ms feeding the pre-roll)."""
        return (self._stream is not None and not self._recording
                and time.monotonic() - self._last_frame > threshold_s)

    def heal_if_stale(self, threshold_s: float = 2.0,
                      min_interval_s: float = 10.0) -> bool:
        """Reopen a silent-but-open stream (full PortAudio reinit, ~300ms).
        Rate-limited by `min_interval_s` so a mic that is wedged at the OS
        level (replug required) doesn't trigger a reinit storm.

        Escalation (found live 3 Jul: the Samsung USB headset delivered 0
        frames to EVERY process until replugged — reopening it forever left
        dictation dead): if a plain reopen didn't bring frames back and we're
        following the system default, hunt for any input device that actually
        works and record from that instead. A pinned input_device never falls
        back — the user chose it explicitly."""
        if not self.stale(threshold_s):
            self._heal_streak = 0
            return False
        now = time.monotonic()
        if now - self._last_heal < min_interval_s:
            return False
        self._last_heal = now
        self._heal_streak += 1
        if self._heal_streak >= 2 and self._configured_device is None:
            return self._fallback_to_working_device()
        log.warning(
            "mic stream is open but SILENT (no frames for >%.0fs from %r) — "
            "reopening. If this repeats, the mic itself is wedged: replug it "
            "or pick another default mic.", threshold_s, self.device_name)
        return self.refresh_default_device()

    def _fallback_to_working_device(self) -> bool:
        """The default mic stayed silent through a reopen — switch to any
        input device that actually delivers frames. Runs on the micwatch
        thread; probing can block for seconds, which is fine there. The
        fallback ends on the next refresh_default_device() (the micwatch
        fires one when the default endpoint changes, e.g. on replug)."""
        wedged = self.device_name
        with self._reopen_lock:
            if self._recording:
                return False
            self.close()
            sd._terminate()
            sd._initialize()
            found = _find_live_input(exclude_name=wedged)
            if found is None:
                # Nothing on the machine delivers audio. Reopen the
                # configured device so recovery-by-replug still works.
                log.error(
                    "no working mic found: %r is wedged and no other input "
                    "device delivers audio. Replug the mic or check Windows "
                    "sound settings.", wedged)
                self.input_device = self._configured_device
                self._fallback_active = False
                self._resolve_device()
                self.open()
                return False
            idx, name = found
            self.input_device = idx
            self._fallback_active = True
            self._resolve_device()
            self.open()
        log.warning(
            "FALLBACK MIC: %r is wedged (delivers no audio) — now recording "
            "from %r instead. Replug the wedged mic or change the Windows "
            "default input to switch back.", wedged, name)
        return True

    def start(self) -> None:
        """Begin accumulating, seeded with the pre-roll. No-op if already
        recording (the OS auto-repeats key-down while the hotkey is held).

        Runs on the pynput hook thread (and the overlay's Tk thread), so it
        must NEVER block for long: a held Windows keyboard hook freezes ALL
        keyboard input. Healing a wedged stream is the micwatch thread's job;
        if a swap/heal holds the reopen lock right now, skip the insurance
        open() — recording still starts, and chunks flow as soon as the
        reopened stream is live."""
        if self._recording:
            return
        if self._reopen_lock.acquire(timeout=0.05):
            try:
                self.open()
            finally:
                self._reopen_lock.release()
        with self._lock:
            self._chunks = list(self._preroll)
            self._frames = sum(len(c) for c in self._chunks)
            self._capped = False
            self._recording = True
        log.info("recording started")

    def _on_chunk(self, indata, frames, time_info, status) -> None:
        # Runs on the PortAudio thread — keep it tiny.
        self._last_frame = time.monotonic()
        if status:
            log.warning("capture status: %s", status)
        chunk = None
        with self._lock:
            if not self._recording:
                self._preroll.append(indata[:, 0].copy())
                kept = sum(len(c) for c in self._preroll)
                while self._preroll and kept - len(self._preroll[0]) >= self._preroll_frames:
                    kept -= len(self._preroll.pop(0))
                return
            if self._frames >= self._max_frames:
                if not self._capped:
                    self._capped = True
                    log.warning("hit the %.0fs cap — discarding further audio (FR-3)",
                                self.max_seconds)
                return
            take = min(frames, self._max_frames - self._frames)
            chunk = indata[:take, 0].copy()
            self._chunks.append(chunk)
            self._frames += take
        if self.on_level is not None and chunk is not None and len(chunk):
            try:  # outside the lock; a UI hiccup must not touch the audio path
                self.on_level(float(np.sqrt(np.mean(chunk * chunk))))
            except Exception:
                log.debug("on_level callback failed", exc_info=True)

    def stop(self) -> tuple[np.ndarray, int]:
        """Stop accumulating (the stream stays open) and return
        (float32 mono audio, sample_rate). Safe to call when not recording."""
        with self._lock:
            self._recording = False
            chunks, self._chunks = self._chunks, []
            self._frames = 0
            self._preroll = []
        audio = (np.concatenate(chunks) if chunks
                 else np.zeros(0, dtype=np.float32))
        log.info("recording stopped: %.2fs captured", len(audio) / self.rate)
        return audio, self.rate
