"""M8 live check: opens the settings window from the RUNNING app's pill
menu with real mouse input and verifies a real top-level window appears.

Run with the tray app RUNNING (pill at its saved/default position must be
visible; "always" mode). The mouse WILL move.
  .venv\\Scripts\\python.exe scripts/m8_settings_check.py
"""

from __future__ import annotations

import ctypes

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    ctypes.windll.user32.SetProcessDPIAware()

import sys
import time
from ctypes import wintypes

from PIL import ImageGrab
from pynput.mouse import Button, Controller

from localflow.desktop import config
from localflow.desktop.overlay import (
    _BOTTOM_MARGIN, _CHIP_H, _CHIP_W, _MENU_GAP, _MENU_ROW_H, _MENU_W,
    system_scale,
)

u32 = ctypes.windll.user32
mouse = Controller()
S = system_scale()


def px(v: float, scale: float) -> int:
    return int(round(v * S * scale))


def click(x: int, y: int, button=Button.left) -> None:
    u32.SetCursorPos(int(x), int(y))
    time.sleep(0.2)
    mouse.press(button)
    time.sleep(0.08)
    mouse.release(button)
    time.sleep(0.3)


def settings_hwnd() -> int:
    return u32.FindWindowW(None, "LocalFlow Settings")


def main() -> int:
    cfg = config.load_config(config.find_config(None))
    scale = cfg.pill_size
    img = ImageGrab.grab()
    sw, sh = img.size

    # Pill anchor: saved position or default bottom-center (logical px).
    if cfg.overlay_position:
        cx = int(round(cfg.overlay_position[0] * S))
        bottom = int(round(cfg.overlay_position[1] * S))
    else:
        cx = sw // 2
        bottom = sh - int(round(_BOTTOM_MARGIN * S))
    chip_cy = bottom - px(_CHIP_H, scale) // 2

    if settings_hwnd():
        print("settings window already open - close it and rerun")
        return 1

    print(f"pill assumed at ({cx}, {chip_cy}); right-clicking...")
    click(cx, chip_cy, Button.right)

    # Menu rows sit above the pill: Unlock, Reset, Settings (row 2), Quit.
    pad, rh = px(6, scale), px(_MENU_ROW_H, scale)
    menu_h = 2 * pad + 4 * rh
    menu_top = bottom - px(_CHIP_H, scale) - px(_MENU_GAP, scale) - menu_h
    settings_row_y = menu_top + pad + 2 * rh + rh // 2
    click(cx, settings_row_y)

    deadline = time.time() + 5.0
    hwnd = 0
    while time.time() < deadline and not hwnd:
        time.sleep(0.2)
        hwnd = settings_hwnd()
    if not hwnd:
        print("FAIL: no 'LocalFlow Settings' window appeared")
        return 1
    rect = wintypes.RECT()
    u32.GetWindowRect(hwnd, ctypes.byref(rect))
    print(f"settings window open: hwnd={hwnd}, rect=({rect.left},{rect.top},"
          f"{rect.right},{rect.bottom})")

    # Second open must reuse the singleton, not stack a duplicate.
    click(cx, chip_cy, Button.right)
    click(cx, settings_row_y)
    time.sleep(1.0)
    second = settings_hwnd()
    ok_singleton = second == hwnd
    print(f"singleton reuse: {'ok' if ok_singleton else 'FAIL (new hwnd)'}")

    u32.PostMessageW(hwnd, 0x0010, 0, 0)          # WM_CLOSE - tidy up
    time.sleep(0.8)
    closed = settings_hwnd() == 0
    print(f"window closes cleanly: {'ok' if closed else 'FAIL'}")

    ok = bool(hwnd) and ok_singleton and closed
    print("m8 settings check", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
