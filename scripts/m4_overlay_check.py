"""Live overlay harness: drives WaveformOverlay through loading -> rec (with
synthetic mic levels) -> idle, and screenshots the pill's screen region to
verify it renders, the waveform draws, and idle collapses to the chip.

Run: .venv\\Scripts\\python.exe scripts/m4_overlay_check.py
"""

from __future__ import annotations

import ctypes

# Physical pixels everywhere: make this process DPI-aware BEFORE tk loads so
# tk coordinates and ImageGrab screenshots line up on a scaled display.
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    ctypes.windll.user32.SetProcessDPIAware()

import sys
import time

import numpy as np
from PIL import ImageGrab

from localflow.desktop.overlay import (
    _BG, _BOTTOM_MARGIN, _CHIP_H, _CHIP_W, _EXP_H, _EXP_W, _WAVE,
    WaveformOverlay, system_scale,
)

S = system_scale()


def _region(w: int, h: int) -> np.ndarray:
    """Grab the bottom-center box where a (w x h logical px) pill sits."""
    img = ImageGrab.grab()
    sw, sh = img.size
    pw, ph = int(w * S), int(h * S)
    x0, y0 = (sw - pw) // 2, sh - ph - int(_BOTTOM_MARGIN * S)
    return np.array(img.crop((x0, y0, x0 + pw, y0 + ph)))[:, :, :3].reshape(-1, 3)


def _count(pixels: np.ndarray, hexcolor: str) -> int:
    rgb = tuple(int(hexcolor[i:i + 2], 16) for i in (1, 3, 5))
    return int(np.all(pixels == rgb, axis=1).sum())


def main() -> int:
    overlay = WaveformOverlay(hotkey="alt+`")

    overlay.set_state("loading")
    # Tk cold-start can take >1s in its thread — poll until the pill shows.
    deadline = time.time() + 6.0
    loading_bg = 0
    while time.time() < deadline and loading_bg < 1000:
        time.sleep(0.25)
        loading_bg = _count(_region(_EXP_W, _EXP_H), _BG)

    overlay.set_state("rec")
    for i in range(45):  # ~1.5s of a synthetic speech-ish envelope
        overlay.push_level(0.02 + 0.03 * abs(np.sin(i / 3)))
        time.sleep(0.03)
    rec = _region(_EXP_W, _EXP_H)
    rec_bg, rec_bars = _count(rec, _BG), _count(rec, _WAVE)

    overlay.set_state("idle")
    time.sleep(0.4)
    chip_bg = _count(_region(_CHIP_W, _CHIP_H), _BG)
    big_bg = _count(_region(_EXP_W, _EXP_H), _BG)
    overlay.close()

    print(f"scale {S:.2f} | loading: {loading_bg} bg px | rec: {rec_bg} bg px, "
          f"{rec_bars} wave px | idle chip: {chip_bg} bg px "
          f"(expanded region: {big_bg})")
    ok = (loading_bg > 1000 and rec_bars > 100
          and chip_bg > 200 and big_bg < loading_bg // 2)
    print("overlay check", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
