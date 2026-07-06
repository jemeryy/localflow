"""Persistent on-screen pill (FR-2, Wispr-Flow-style, Jeremy's request).

Always visible at bottom-center so you can SEE LocalFlow is running:

- idle: a small chip — green dot + "LocalFlow" wordmark
- loading / ready: expanded capsule with status text
- recording: expanded capsule, pulsing red dot + live smoothed waveform
- transcribing: same capsule, amber frozen waveform

Mouse (the window never takes focus — WS_EX_NOACTIVATE — so clicks leave
the paste target focused):
- left-click: toggle dictation (start recording / stop + transcribe)
- right-click: inline Settings | Quit menu drawn in the capsule

Threading: tkinter is single-threaded, so a dedicated thread owns the Tk
root and all UI. Public methods only enqueue: set_state() from the
hotkey-listener/worker threads, push_level() from the PortAudio callback
(deque.append is atomic — no lock on the audio path). The UI thread drains
both on a ~30fps after() tick. Any overlay failure degrades to "no
overlay"; it can never take dictation down with it.
"""

from __future__ import annotations

import collections
import logging
import math
import queue
import threading

log = logging.getLogger("localflow.desktop.overlay")

# Logical (96-dpi) sizes; scaled by the system DPI at draw time.
_EXP_W, _EXP_H = 280, 56      # expanded capsule
_CHIP_W, _CHIP_H = 116, 26    # idle chip
_BOTTOM_MARGIN = 90           # gap above the screen bottom
_BARS = 36                    # waveform bars
_TICK_MS = 33                 # ~30fps
_READY_FLASH_MS = 2500
_MENU_TIMEOUT_MS = 6000

_BG = "#1c1c22"
_BG_HOVER = "#2a2a33"
_WAVE = "#ececf0"             # near-white bars, Wispr-style
_DOT_REC = "#e8564a"
_BAR_BUSY = "#f5a623"
_DOT_RUN = "#57c785"          # green "running" dot on the idle chip
_TEXT = "#e6e6ea"
_TEXT_DIM = "#9a9aa4"
_MENU_BTN = "#33333d"         # right-click menu button background
_MENU_BTN_HOVER = "#45454f"   # ...highlighted under the cursor
_COLORKEY = "#ff00fe"         # unlikely-onscreen color -> transparent corners


def bar_heights(levels, n_bars: int, max_h: float, floor: float = 2.0) -> list[float]:
    """Map the most recent RMS levels to bar half-heights, newest rightmost,
    zero-padded on the left. Sqrt-compressed so a quiet mic (M2: the Samsung
    headset runs low) still shows life; clamped to max_h."""
    recent = list(levels)[-n_bars:]
    padded = [0.0] * (n_bars - len(recent)) + recent
    out = []
    for v in padded:
        h = min(1.0, (max(0.0, v) * 18.0) ** 0.5) * max_h
        out.append(max(floor, h))
    return out


def smooth(heights: list[float], k: float = 0.25) -> list[float]:
    """3-tap spatial blend so adjacent bars flow into a wave instead of a
    comb. Endpoints pass through; length is preserved."""
    n = len(heights)
    if n < 3:
        return list(heights)
    out = [heights[0]]
    for i in range(1, n - 1):
        out.append(heights[i - 1] * k + heights[i] * (1 - 2 * k) + heights[i + 1] * k)
    out.append(heights[-1])
    return out


def system_scale() -> float:
    """Physical px per logical 96-dpi px (1.5 on a 150% display). The UI
    thread makes the process DPI-aware first so Tk renders crisp."""
    import ctypes
    try:
        return ctypes.windll.user32.GetDpiForSystem() / 96.0
    except Exception:
        return 1.0


class WaveformOverlay:
    """States: loading | ready (flash) | rec | busy | menu | idle (chip)."""

    def __init__(self, hotkey: str = "alt+`", settings_path=None,
                 on_toggle=None, on_quit=None):
        self._hotkey = hotkey
        # Set (or reset) these any time before the user clicks:
        self.settings_path = settings_path
        self.on_toggle = on_toggle    # click: start/stop dictation
        self.on_quit = on_quit        # menu Quit: ask the app to shut down
        self._cmd: queue.Queue = queue.Queue()
        self._levels: collections.deque = collections.deque(maxlen=_BARS)
        self._state = "idle"
        self._thread = threading.Thread(
            target=self._ui_thread, daemon=True, name="localflow-overlay")
        self._thread.start()

    # -- any-thread API ----------------------------------------------------

    def push_level(self, rms: float) -> None:
        self._levels.append(float(rms))

    def set_state(self, state: str) -> None:
        self._cmd.put(state)

    def close(self) -> None:
        self._cmd.put("quit")
        self._thread.join(timeout=2.0)

    # -- UI thread only ----------------------------------------------------

    def _ui_thread(self) -> None:
        try:
            self._run_ui()
        except Exception:
            log.exception("overlay UI died — dictation continues without it")

    def _run_ui(self) -> None:
        import ctypes
        try:  # crisp text/shapes on scaled displays; harmless if it fails
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass

        import tkinter as tk

        S = system_scale()
        root = tk.Tk()
        root.withdraw()
        root.overrideredirect(True)          # borderless, no taskbar entry
        root.attributes("-topmost", True)
        try:
            root.attributes("-transparentcolor", _COLORKEY)
        except tk.TclError:
            pass                             # square corners beat no overlay
        canvas = tk.Canvas(root, bg=_COLORKEY, highlightthickness=0)
        canvas.pack(fill="both", expand=True)
        screen_w, screen_h = root.winfo_screenwidth(), root.winfo_screenheight()
        self._no_activate(root)

        size = {"w": 0, "h": 0}
        disp: list[float] = []               # displayed bar heights (eased)
        frame = {"n": 0}
        hover = {"on": False}

        def px(v: float) -> int:
            return int(round(v * S))

        def layout(w_log: float, h_log: float) -> None:
            w, h = px(w_log), px(h_log)
            if (size["w"], size["h"]) != (w, h):
                size["w"], size["h"] = w, h
                x = (screen_w - w) // 2
                y = screen_h - px(_BOTTOM_MARGIN) - h
                root.geometry(f"{w}x{h}+{x}+{y}")
                canvas.configure(width=w, height=h)
            canvas.delete("all")
            draw_capsule()

        def draw_capsule() -> None:
            w, h = size["w"], size["h"]
            fill = _BG_HOVER if hover["on"] else _BG
            r = h // 2
            canvas.create_oval(0, 0, h, h, fill=fill, outline=fill, tags="capsule")
            canvas.create_oval(w - h, 0, w, h, fill=fill, outline=fill, tags="capsule")
            canvas.create_rectangle(r, 0, w - r, h, fill=fill, outline=fill,
                                    tags="capsule")

        def show() -> None:
            root.deiconify()
            root.lift()
            root.attributes("-topmost", True)

        def center_text(message: str, fill: str = _TEXT) -> None:
            canvas.create_text(size["w"] // 2, size["h"] // 2, text=message,
                               fill=fill, font=("Segoe UI", 10), tags="text")

        def draw_chip() -> None:
            h = size["h"]
            r = px(4)
            cx = px(15)
            canvas.create_oval(cx - r, h // 2 - r, cx + r, h // 2 + r,
                               fill=_DOT_RUN, outline=_DOT_RUN, tags="text")
            canvas.create_text(px(26), h // 2, anchor="w", text="LocalFlow",
                               fill=_TEXT_DIM, font=("Segoe UI", 9), tags="text")

        menu_keys = (("settings", "Settings"), ("quit", "Quit"))

        def menu_boxes() -> dict:
            """Left half = Settings, right half = Quit. Full-height boxes so
            the click target is the whole half of the pill, not just the glyphs
            (a near-miss used to fall through and close the menu)."""
            w, h = size["w"], size["h"]
            pad, gap = px(8), px(6)
            top, bot = px(7), h - px(7)
            mid = w // 2
            return {
                "settings": (pad, top, mid - gap // 2, bot),
                "quit": (mid + gap // 2, top, w - pad, bot),
            }

        def highlight_menu(hot: str | None) -> None:
            for key, _label in menu_keys:
                color = _MENU_BTN_HOVER if key == hot else _MENU_BTN
                canvas.itemconfig(f"rect_{key}", fill=color, outline=color)

        def draw_menu() -> None:
            canvas.delete("menu")
            boxes = menu_boxes()
            for key, label in menu_keys:
                x0, y0, x1, y1 = boxes[key]
                canvas.create_rectangle(
                    x0, y0, x1, y1, fill=_MENU_BTN, outline=_MENU_BTN,
                    tags=("menu", f"hit_{key}", f"rect_{key}"))
                canvas.create_text(
                    (x0 + x1) // 2, (y0 + y1) // 2, text=label, fill=_TEXT,
                    font=("Segoe UI", 10), tags=("menu", f"hit_{key}"))
            # Auto-dismiss fallback. The menu deliberately does NOT close on
            # mouse-leave any more — the pill is tiny and the cursor routinely
            # skims off it on the way to a button, which used to snap it shut.
            root.after(_MENU_TIMEOUT_MS,
                       lambda: apply("idle") if self._state == "menu" else None)

        def draw_wave() -> None:
            canvas.delete("wave")
            w, h = size["w"], size["h"]
            mid = h / 2
            rec = self._state == "rec"
            left = px(38) if rec else px(24)
            right = w - px(24)
            targets = bar_heights(self._levels, _BARS, max_h=mid - px(10),
                                  floor=px(2))
            if len(disp) != _BARS:           # first frame: snap
                disp[:] = targets
            else:                            # fast attack, slow decay easing
                for i, t in enumerate(targets):
                    k = 0.6 if t > disp[i] else 0.3
                    disp[i] += (t - disp[i]) * k
            heights = smooth(disp)
            step = (right - left) / _BARS
            color = _WAVE if rec else _BAR_BUSY
            width = max(2, px(3))
            for i, bh in enumerate(heights):
                x = left + i * step + step / 2
                canvas.create_line(x, mid - bh, x, mid + bh, fill=color,
                                   width=width, capstyle="round", tags="wave")
            if rec:                          # breathing red recording dot
                frame["n"] += 1
                r = px(4) + px(1.5) * math.sin(frame["n"] / 6)
                cx = px(19)
                canvas.create_oval(cx - r, mid - r, cx + r, mid + r,
                                   fill=_DOT_REC, outline=_DOT_REC, tags="wave")

        def apply(state: str) -> None:
            self._state = state
            if state == "loading":
                layout(_EXP_W, _EXP_H)
                center_text("LocalFlow - loading models...")
            elif state == "ready":
                layout(_EXP_W, _EXP_H)
                center_text(f"Ready - hold {self._hotkey} or click to dictate")
                root.after(_READY_FLASH_MS,
                           lambda: apply("idle") if self._state == "ready" else None)
            elif state == "rec":
                self._levels.clear()
                disp.clear()
                layout(_EXP_W, _EXP_H)
            elif state == "busy":
                layout(_EXP_W, _EXP_H)
                draw_wave()
            elif state == "menu":
                layout(_EXP_W, _EXP_H)
                draw_menu()
            else:                            # idle -> persistent chip
                layout(_CHIP_W, _CHIP_H)
                draw_chip()
            show()

        def call_safely(cb, what: str) -> None:
            if cb is None:
                return
            try:
                cb()
            except Exception:
                log.exception("overlay %s callback failed", what)

        def open_settings() -> None:
            path = self.settings_path
            if path is None:
                return
            try:
                from localflow.desktop.config import open_settings_editor
                open_settings_editor(path)
            except Exception:
                log.exception("could not open settings file %s", path)

        def menu_hit(x: int, y: int) -> str | None:
            """Which menu button (if any) the pixel is on. Pure geometry —
            Tk's 'current' item tag proved unreliable on this NOACTIVATE
            window (found live 3 Jul: Settings clicks silently did nothing)."""
            for key, (x0, y0, x1, y1) in menu_boxes().items():
                if x0 <= x <= x1 and y0 <= y <= y1:
                    return key
            return None

        def on_click(event) -> None:
            st = self._state
            if st == "menu":
                hit = menu_hit(event.x, event.y)
                if hit == "settings":
                    apply("idle")
                    open_settings()
                elif hit == "quit":
                    call_safely(self.on_quit, "quit")
                else:                        # clicked off a button -> dismiss
                    apply("idle")
            elif st in ("idle", "ready", "rec"):
                # toggle dictation; focus stays on the paste target thanks
                # to WS_EX_NOACTIVATE
                call_safely(self.on_toggle, "toggle")
            # loading/busy: ignore clicks

        def on_rclick(_event) -> None:
            if self._state in ("idle", "ready", "busy"):
                apply("menu")

        def on_motion(event) -> None:
            if self._state != "menu":
                return
            highlight_menu(menu_hit(event.x, event.y))

        def set_hover(on: bool) -> None:
            hover["on"] = on
            fill = _BG_HOVER if on else _BG
            canvas.itemconfig("capsule", fill=fill, outline=fill)

        def on_leave(_event) -> None:
            set_hover(False)
            if self._state == "menu":
                highlight_menu(None)   # keep the menu open; just drop the hover

        canvas.bind("<Button-1>", on_click)
        canvas.bind("<Button-3>", on_rclick)
        canvas.bind("<Motion>", on_motion)
        canvas.bind("<Enter>", lambda e: set_hover(True))
        canvas.bind("<Leave>", on_leave)

        def tick() -> None:
            try:
                while True:
                    state = self._cmd.get_nowait()
                    if state == "quit":
                        root.destroy()
                        return
                    apply(state)
            except queue.Empty:
                pass
            if self._state == "rec":
                draw_wave()
            root.after(_TICK_MS, tick)

        apply("idle")                        # visible from the first moment
        root.after(_TICK_MS, tick)
        root.mainloop()
        # Tcl must be torn down by the thread that created it. tkinter keeps
        # a module-global ref (_default_root); if the MAIN thread's exit GC
        # frees it, Tcl aborts the whole process with "Tcl_AsyncDelete:
        # async handler deleted by the wrong thread". Drop every ref and
        # collect HERE, on the UI thread.
        if getattr(tk, "_default_root", None) is root:
            tk._default_root = None
        del canvas, root
        import gc
        gc.collect()

    @staticmethod
    def _no_activate(root) -> None:
        # WS_EX_NOACTIVATE + WS_EX_TOOLWINDOW: never steals focus (FR-2),
        # never shows in alt-tab. Without this, clicking the pill would
        # yank focus off the paste target — the whole point is that the
        # target app keeps focus while you dictate.
        import ctypes
        root.update_idletasks()
        user32 = ctypes.windll.user32
        hwnd = user32.GetParent(root.winfo_id()) or root.winfo_id()
        GWL_EXSTYLE = -20
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE,
                              style | 0x08000000 | 0x00000080)
