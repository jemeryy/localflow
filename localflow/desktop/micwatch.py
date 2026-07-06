"""Follow the Windows default mic (Jeremy's request).

PortAudio cannot see default-device changes after it initializes, so this
watcher polls the actual WASAPI default capture endpoint ID via COM (pycaw)
every couple of seconds and hot-swaps the recorder's stream when it changes.
Only relevant when input_device is unset (i.e. "use the system default");
a pinned device never switches.

The swap is deferred while a recording is in progress — the utterance being
spoken finishes on the old mic, and the switch happens on the next tick.
"""

from __future__ import annotations

import logging
import threading

log = logging.getLogger("localflow.desktop.micwatch")

_E_CAPTURE = 1   # EDataFlow.eCapture
_E_CONSOLE = 0   # ERole.eConsole — the plain "Default Device" in Settings


def _default_endpoint_id() -> str:
    from pycaw.pycaw import AudioUtilities

    enumerator = AudioUtilities.GetDeviceEnumerator()
    device = enumerator.GetDefaultAudioEndpoint(_E_CAPTURE, _E_CONSOLE)
    return str(device.GetId())


class DefaultMicWatcher:
    """Polls the default capture endpoint; calls
    recorder.refresh_default_device() when it changes."""

    def __init__(self, recorder, interval_s: float = 2.0, get_id=None):
        self._recorder = recorder
        self._interval = interval_s
        self._get_id = get_id or _default_endpoint_id
        self._last: str | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._watch, daemon=True, name="localflow-micwatch")

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def check_once(self) -> bool:
        """One poll step (factored out for tests). Returns True if the
        recorder was switched to a new device."""
        current = self._get_id()
        if self._last is None:      # first sighting is the baseline
            self._last = current
            return False
        if current == self._last:
            # Same device — use the tick to heal a silent-but-open stream
            # (wedged WASAPI endpoint; found live 3 Jul: 0.00s captures).
            self._recorder.heal_if_stale()
            return False
        # Deliberately NOT updating _last on the deferred paths below:
        # the change is retried on the next tick until the swap happens.
        if self._recorder.recording:
            return False
        if not self._recorder.refresh_default_device():
            return False
        self._last = current
        log.info("default mic changed -> now recording from %r",
                 self._recorder.device_name)
        return True

    def _watch(self) -> None:
        try:  # COM needs per-thread init; comtypes handles most of it
            import comtypes
            comtypes.CoInitialize()
        except Exception:
            pass
        while not self._stop.wait(self._interval):
            try:
                self.check_once()
            except Exception:
                # Transient COM/device errors (e.g. mid-unplug) — next tick
                # retries; the watcher must never die quietly mid-session.
                log.debug("mic watch tick failed", exc_info=True)
