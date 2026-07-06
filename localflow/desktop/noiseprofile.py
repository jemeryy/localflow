"""Noise-profile persistence (M9): the calibrated room-noise floor.

The floor itself is computed by core.estimate_noise_floor (pure); this
module only owns the file. Stored at %LOCALAPPDATA%\\LocalFlow\\
noise_profile.npz together with the mic's name: a profile calibrated on
one mic is wrong for another (different frequency response and gain), so
load_for_device() returns None on a name mismatch and reduce_noise falls
back to its per-clip estimate automatically.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import numpy as np

log = logging.getLogger("localflow.desktop.noiseprofile")

PROFILE_FILENAME = "noise_profile.npz"


def default_profile_path() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "LocalFlow"
    return base / PROFILE_FILENAME


def save_profile(floor: np.ndarray, device_name: str,
                 path: Path | None = None) -> Path:
    path = path or default_profile_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez(path, floor=np.asarray(floor, dtype=np.float32),
             device=np.array(device_name), saved_at=np.array(time.time()))
    log.info("noise profile saved for %r -> %s", device_name, path)
    return path


def load_profile(path: Path | None = None):
    """-> (floor, device_name, saved_at_epoch) or None if absent/corrupt."""
    path = path or default_profile_path()
    if not path.is_file():
        return None
    try:
        with np.load(path, allow_pickle=False) as data:
            return (np.asarray(data["floor"], dtype=np.float32),
                    str(data["device"]), float(data["saved_at"]))
    except Exception:
        log.exception("noise profile unreadable — ignoring it (%s)", path)
        return None


def load_for_device(device_name: str, path: Path | None = None):
    """The floor for THIS mic, or None (no profile / other mic / corrupt).
    Names are prefix-matched both ways: MME truncates device names to 31
    chars while WASAPI does not (same rule as the capture layer)."""
    loaded = load_profile(path)
    if loaded is None:
        return None
    floor, saved_device, _ = loaded
    if not (device_name.startswith(saved_device)
            or saved_device.startswith(device_name)):
        log.info("noise profile is for %r, active mic is %r — using the "
                 "per-clip estimate instead", saved_device, device_name)
        return None
    return floor


def clear_profile(path: Path | None = None) -> bool:
    path = path or default_profile_path()
    try:
        path.unlink()
        log.info("noise profile cleared (%s)", path)
        return True
    except FileNotFoundError:
        return False
