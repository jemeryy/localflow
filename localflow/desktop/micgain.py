"""Auto mic gain (Jeremy, 11 Jul): adjust the WINDOWS input level when real
dictations keep arriving too quiet or clipped - the same thing meeting apps'
"automatically adjust mic sensitivity" does. The preprocess normalize already
boosts digitally, but digital gain cannot recover signal-to-noise; raising
the capture level at the endpoint can.

Deliberately dumb: one step per dictation, driven only by dictations that
actually decoded speech (the app never calls this on silence, so a dead mic
cannot crank the slider). Default capture endpoint only - a pinned mic is
never touched. Every COM/driver failure logs at debug and becomes a no-op:
gain help must never take a dictation down.
"""

from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger("localflow.desktop.micgain")

# One lock for ALL pycaw/COM traffic in the process (micwatch's poll thread
# takes it too). Two threads doing WASAPI enumeration/activation at once is
# the prime suspect for the 13 Jul pythonw access-violation crashes in
# _ctypes.pyd (the comtypes layer); every crash landed while both this
# worker-thread path and the 2s micwatch poll were live.
COM_LOCK = threading.Lock()


def com_release(*pointers) -> None:
    """Release comtypes COM pointers NOW, on the calling (CoInitialized)
    thread, then zero them so a later __del__ — which cyclic GC can run on
    ANY thread, with no COM context (every 13-15 Jul native crash) — sees a
    NULL pointer and does nothing. Never raises; None entries are skipped."""
    import ctypes
    for ptr in pointers:
        try:
            if ptr:
                ptr.Release()
                ctypes.memset(ctypes.byref(ptr), 0, ctypes.sizeof(ptr))
        except Exception:
            log.debug("COM release failed", exc_info=True)

_TOO_QUIET_PEAK = 0.15   # raw dictation peak below this -> raise the level
_CLIP_PEAK = 0.99        # peak at the rail -> lower it (clipping garbles STT)
_STEP = 0.10             # one Windows-slider step (10 points) per dictation
_MIN_VOLUME = 0.05       # never turn the mic effectively off


def recommend_volume(peak: float, current: float) -> float | None:
    """New endpoint scalar (0-1) for a dictation that peaked at `peak` in
    the RAW mic audio, or None to leave the level alone. Pure - unit-tested.

    Quiet speech raises the level a step (converges in a few dictations);
    a peak at the rail lowers it a step. peak <= 0 means a dead mic, not a
    quiet one - never adjust on that."""
    if peak <= 0.0:
        return None
    if peak < _TOO_QUIET_PEAK and current < 1.0 - 1e-3:
        return min(1.0, round(current + _STEP, 2))
    if peak > _CLIP_PEAK and current > _MIN_VOLUME:
        return max(_MIN_VOLUME, round(current - _STEP, 2))
    return None


# Last endpoint level actually read, so the common no-op case (mic already at
# 100% and still quiet) never touches COM at all — fewer COM create/release
# cycles = fewer chances to hit the native crash above. The TTL re-reads the
# real slider now and then in case the user moved it by hand; a default-mic
# switch invalidates immediately (the cache belongs to ONE endpoint).
_CACHE_TTL_S = 900.0
_cached_scalar: float | None = None
_cached_at = 0.0


def invalidate_cache() -> None:
    """Forget the cached endpoint level (call when the default mic changes)."""
    global _cached_scalar
    _cached_scalar = None


def adjust_for_peak(peak: float) -> tuple[float, float] | None:
    """Apply recommend_volume to the default capture endpoint. Returns
    (old_scalar, new_scalar) when the level was changed, else None."""
    global _cached_scalar, _cached_at
    if (_cached_scalar is not None
            and time.monotonic() - _cached_at < _CACHE_TTL_S
            and recommend_volume(peak, _cached_scalar) is None):
        return None                     # nothing would change — skip COM
    try:
        with COM_LOCK:
            volume = _endpoint_volume()
            try:
                current = float(volume.GetMasterVolumeLevelScalar())
                new = recommend_volume(peak, current)
                if new is None or abs(new - current) < 0.005:
                    _cached_scalar, _cached_at = current, time.monotonic()
                    return None
                volume.SetMasterVolumeLevelScalar(float(new), None)
                _cached_scalar, _cached_at = new, time.monotonic()
                return current, new
            finally:
                com_release(volume)
    except Exception:
        log.debug("auto-gain adjustment failed", exc_info=True)
        return None


def _endpoint_volume():
    """IAudioEndpointVolume of the default capture endpoint (eConsole - the
    same role micwatch tracks). COM wants per-thread init; initializing an
    already-initialized thread is harmless."""
    import comtypes
    from ctypes import POINTER, cast

    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

    try:
        comtypes.CoInitialize()
    except Exception:
        pass
    enumerator = device = None
    try:
        enumerator = AudioUtilities.GetDeviceEnumerator()
        device = enumerator.GetDefaultAudioEndpoint(1, 0)   # eCapture, eConsole
        iface = device.Activate(IAudioEndpointVolume._iid_,
                                comtypes.CLSCTX_ALL, None)
        return cast(iface, POINTER(IAudioEndpointVolume))
    finally:
        com_release(device, enumerator)
