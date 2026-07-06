"""M7 live pill harness: drives the REAL overlay with REAL mouse input.

Exercises, end to end on the actual desktop: click-to-dictate, right-click
menu (opens above the pill, chip stays visible), no self-closing menu,
outside-click dismiss, right-click dismiss, unlock -> multi-drag -> lock,
reset position, and the "dictating" visibility mode.

Run with the tray app STOPPED (two pills would overlap):
  .venv\\Scripts\\python.exe scripts/m7_pill_check.py
The mouse WILL move for ~20 seconds.
"""

from __future__ import annotations

import ctypes

try:  # physical pixels everywhere, BEFORE tk loads
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    ctypes.windll.user32.SetProcessDPIAware()

import sys
import time

import numpy as np
from PIL import ImageGrab
from pynput.mouse import Button, Controller

from localflow.desktop.overlay import (
    _BG, _BG_HOVER, _BOTTOM_MARGIN, _CHIP_H, _CHIP_W, _DRAG_ARM, _EXP_H,
    _EXP_W, _MENU_BTN, _MENU_GAP, _MENU_ROW_H, _MENU_W, WaveformOverlay,
    system_scale,
)

S = system_scale()
mouse = Controller()
FAILURES: list[str] = []
_u32 = ctypes.windll.user32


def set_cursor(x: int, y: int) -> None:
    # pynput's position setter lands wildly off on this machine (verified
    # 6 Jul: asked (600,600), got (397,475)); SetCursorPos is exact. Only
    # the button events go through pynput.
    _u32.SetCursorPos(int(x), int(y))


def px(v: float) -> int:
    return int(round(v * S))  # pill_size=1.0 in this harness


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'ok' if ok else 'FAIL'}] {name}" + (f" ({detail})" if detail else ""))
    if not ok:
        FAILURES.append(name)


def grab(x0: int, y0: int, w: int, h: int) -> np.ndarray:
    img = ImageGrab.grab()
    return np.array(img.crop((x0, y0, x0 + w, y0 + h)))[:, :, :3].reshape(-1, 3)


def count(pixels: np.ndarray, hexcolor: str) -> int:
    rgb = tuple(int(hexcolor[i:i + 2], 16) for i in (1, 3, 5))
    return int(np.all(pixels == rgb, axis=1).sum())


def real_window_rect(x: int, y: int):
    """hwnd + GetWindowRect of whatever is at screen point (x, y)."""
    from ctypes import wintypes
    pt = wintypes.POINT(int(x), int(y))
    hwnd = _u32.WindowFromPoint(pt)
    r = wintypes.RECT()
    _u32.GetWindowRect(hwnd, ctypes.byref(r))
    return hwnd, (r.left, r.top, r.right, r.bottom)


def wait_state(overlay: WaveformOverlay, want: str, timeout: float = 2.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if overlay._state == want:
            time.sleep(0.15)          # let the redraw land before pixel checks
            return True
        time.sleep(0.03)
    return False


def click(x: int, y: int, button=Button.left) -> None:
    set_cursor(x, y)
    time.sleep(0.15)
    mouse.press(button)
    time.sleep(0.08)
    mouse.release(button)
    time.sleep(0.2)


def drag(x0: int, y0: int, x1: int, y1: int) -> None:
    set_cursor(x0, y0)
    time.sleep(0.15)
    mouse.press(Button.left)
    time.sleep(0.1)
    steps = 10
    for i in range(1, steps + 1):
        set_cursor(x0 + (x1 - x0) * i // steps,
                   y0 + (y1 - y0) * i // steps)
        time.sleep(0.03)
    mouse.release(Button.left)
    time.sleep(0.3)


def chip_rect_at_default(sw: int, sh: int) -> tuple[int, int, int, int]:
    w, h = px(_CHIP_W), px(_CHIP_H)
    return (sw - w) // 2, sh - int(round(_BOTTOM_MARGIN * S)) - h, w, h


def menu_window(sw: int, sh: int) -> dict:
    """Mirror layout_menu()'s math for the DEFAULT anchor."""
    pad, rh = px(6), px(_MENU_ROW_H)
    menu_h = 2 * pad + 4 * rh
    gap = px(_MENU_GAP)
    chip_h = px(_CHIP_H)
    w = max(px(_MENU_W), px(_CHIP_W))
    h = menu_h + gap + chip_h
    x = (sw - w) // 2
    y = sh - int(round(_BOTTOM_MARGIN * S)) - h
    return {"x": x, "y": y, "w": w, "h": h, "pad": pad, "rh": rh,
            "menu_h": menu_h}


def menu_row_center(m: dict, row: int) -> tuple[int, int]:
    return (m["x"] + m["w"] // 2,
            m["y"] + m["pad"] + row * m["rh"] + m["rh"] // 2)


def main() -> int:
    img = ImageGrab.grab()
    sw, sh = img.size
    toggles: list[float] = []
    positions: list[object] = []

    overlay = WaveformOverlay(hotkey="alt+`",
                              on_toggle=lambda: toggles.append(time.time()))
    overlay.on_position_change = positions.append

    print("m7 pill harness (mouse will move)...")
    time.sleep(2.5)                              # Tk cold start
    cx0, cy0, cw, ch = chip_rect_at_default(sw, sh)
    chip_cx, chip_cy = cx0 + cw // 2, cy0 + ch // 2

    print("- idle chip")
    check("chip visible at bottom-center",
          count(grab(cx0, cy0, cw, ch), _BG) > 200)

    print("- click toggles dictation")
    click(chip_cx, chip_cy)
    time.sleep(0.3)
    check("left click fired on_toggle once", len(toggles) == 1,
          f"{len(toggles)} calls")

    print("- right-click menu")
    click(chip_cx, chip_cy, Button.right)
    check("menu state entered", wait_state(overlay, "menu"))
    m = menu_window(sw, sh)
    _hw, real = real_window_rect(chip_cx, chip_cy)
    check("menu window geometry matches the layout math",
          real == (m["x"], m["y"], m["x"] + m["w"], m["y"] + m["h"]),
          f"real {real}")
    check("menu rows drawn above",
          count(grab(m["x"], m["y"], m["w"], m["menu_h"]), _MENU_BTN) > 500)
    under = grab(cx0, cy0, cw, ch)
    check("chip still visible under the menu",   # hover fill counts too -
          count(under, _BG) + count(under, _BG_HOVER) > 150)  # cursor is on it

    print("- menu must NOT close by itself (3.5s)")
    time.sleep(3.5)
    check("menu still open after 3.5s", overlay._state == "menu")

    print("- outside click closes the menu")
    click(chip_cx, m["y"] - 250)
    check("closed by outside click", wait_state(overlay, "idle"))

    print("- right-click reopens, right-click again closes")
    click(chip_cx, chip_cy, Button.right)
    check("reopened", wait_state(overlay, "menu"))
    click(chip_cx, chip_cy, Button.right)
    check("closed by second right-click", wait_state(overlay, "idle"))

    print("- unlock -> drag -> drag again (stays unlocked)")
    click(chip_cx, chip_cy, Button.right)
    wait_state(overlay, "menu")
    click(*menu_row_center(m, 0))                # "Unlock position"
    check("menu closed after unlock", wait_state(overlay, "idle"))
    check("unlocked tint on the chip",
          count(grab(cx0, cy0, cw, ch), _DRAG_ARM) > 100)
    n_clicks = len(toggles)
    drag(chip_cx, chip_cy, chip_cx + 180, chip_cy - 160)
    check("drag saved a position", len(positions) == 1
          and isinstance(positions[0], list), f"{positions}")
    check("dragging did not toggle dictation", len(toggles) == n_clicks)
    if positions and isinstance(positions[0], list):
        nx, ny = positions[0]
        ncx0 = int(round(nx * S)) - cw // 2
        ncy0 = int(round(ny * S)) - ch
        check("pill visible at the new spot (still unlocked-green)",
              count(grab(ncx0, ncy0, cw, ch), _DRAG_ARM) > 100)
        drag(ncx0 + cw // 2, ncy0 + ch // 2,
             ncx0 + cw // 2 - 120, ncy0 + ch // 2 + 60)
        check("second drag also saved (still unlocked)", len(positions) == 2)

    print("- lock again from the menu")
    if len(positions) >= 2 and isinstance(positions[1], list):
        lx, ly = positions[1]
        lcx = int(round(lx * S))
        lcy0 = int(round(ly * S)) - ch
        lcx0 = lcx - cw // 2
        click(lcx, lcy0 + ch // 2, Button.right)
        check("menu at new spot", wait_state(overlay, "menu"))
        # menu window is anchored to the moved pill now
        mm = {"x": lcx - max(px(_MENU_W), px(_CHIP_W)) // 2,
              "y": int(round(ly * S)) - menu_window(sw, sh)["h"],
              "w": max(px(_MENU_W), px(_CHIP_W)),
              "pad": px(6), "rh": px(_MENU_ROW_H),
              "menu_h": 2 * px(6) + 4 * px(_MENU_ROW_H)}
        click(*menu_row_center(mm, 0))           # "Lock position"
        wait_state(overlay, "idle")
        check("tint gone after lock",
              count(grab(lcx0, lcy0, cw, ch), _DRAG_ARM) < 20)
        n = len(toggles)
        click(lcx, lcy0 + ch // 2)
        time.sleep(0.3)
        check("click dictates again after lock", len(toggles) == n + 1)

        print("- reset position from the menu")
        click(lcx, lcy0 + ch // 2, Button.right)
        wait_state(overlay, "menu")
        click(*menu_row_center(mm, 1))           # "Reset position"
        wait_state(overlay, "idle")
        check("reset reported None", positions[-1] is None)
        check("chip back at bottom-center",
              count(grab(cx0, cy0, cw, ch), _BG) > 200)

    print("- 'dictating' visibility mode")
    overlay.set_visibility("dictating")
    time.sleep(0.5)
    check("hidden while idle", count(grab(cx0, cy0, cw, ch), _BG) < 50)
    overlay.set_state("rec")
    time.sleep(0.5)
    ex0 = (sw - px(_EXP_W)) // 2
    ey0 = sh - int(round(_BOTTOM_MARGIN * S)) - px(_EXP_H)
    check("visible while recording",
          count(grab(ex0, ey0, px(_EXP_W), px(_EXP_H)), _BG) > 300)
    overlay.set_state("idle")
    time.sleep(0.5)
    check("hidden again right after idle",
          count(grab(cx0, cy0, cw, ch), _BG) < 50)
    overlay.set_visibility("always")
    time.sleep(0.5)
    check("back for 'always'", count(grab(cx0, cy0, cw, ch), _BG) > 200)

    overlay.close()
    print(f"\n{'PASS - all checks green' if not FAILURES else 'FAIL: ' + ', '.join(FAILURES)}")
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    sys.exit(main())
