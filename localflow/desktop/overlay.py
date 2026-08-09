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

# Logical (96-dpi) sizes; scaled by system DPI x pill_size at draw time.
# Base sizes bumped ~20% on 6 Jul (Jeremy: bigger pill, easier to read);
# the `pill_size` config key scales further on top.
_EXP_W, _EXP_H = 340, 64      # expanded capsule
_CHIP_W, _CHIP_H = 150, 34    # idle chip
_BOTTOM_MARGIN = 90           # gap above the screen bottom
_BARS = 36                    # waveform bars
_TICK_MS = 33                 # ~30fps
_READY_FLASH_MS = 2500
_FLASH_MS = 1600              # transient confirmation (e.g. "Copied")
# A topmost override-redirect tool window can be hidden by Windows after a
# full-screen app, a lock/unlock, or a display change and NOT come back on its
# own (Jeremy: "the pill disappeared, had to re-show it from the tray"). Re-
# assert it on this cadence (ticks) so it self-heals without user action.
_HEAL_TICKS = 60              # ~2s at 33ms
_DRAG_SLOP_LOG = 6            # logical px of wiggle still counted as a click
_MENU_W = 190                 # right-click menu panel width (logical)
_MENU_ROW_H = 32              # logical height of one right-click menu row
_MENU_GAP = 8                 # gap between the menu panel and the pill

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
_DRAG_ARM = "#2e6b4c"         # capsule tint while position is unlocked
                              # (distinct from the chip dot green on purpose)
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


class PressArbiter:
    """Click vs drag decision logic (M7, revised 6 Jul). Pure — no Tk — so
    the paths get unit tests. Jeremy's spec: the pill is LOCKED by default
    and never drags; "Unlock position" in the right-click menu makes the
    NEXT press a drag (release saves the spot and relocks).

    - locked press:   release within the slop -> "click"; moving beyond the
                      slop cancels (the release does nothing).
    - unlocked press: dragging immediately; motion moves the window;
                      release -> "drag_end".
    """

    IDLE, PENDING, CANCELLED, DRAGGING = "idle", "pending", "cancelled", "dragging"

    def __init__(self, slop_px: float = 6.0):
        self.slop = float(slop_px)
        self.state = self.IDLE
        self._origin = (0.0, 0.0)

    def _within_slop(self, x: float, y: float) -> bool:
        return (abs(x - self._origin[0]) <= self.slop
                and abs(y - self._origin[1]) <= self.slop)

    def press(self, x: float, y: float, unlocked: bool = False) -> None:
        self.state = self.DRAGGING if unlocked else self.PENDING
        self._origin = (x, y)

    def motion(self, x: float, y: float) -> bool:
        """Feed drag motion; returns True while dragging (move the window)."""
        if self.state == self.PENDING and not self._within_slop(x, y):
            self.state = self.CANCELLED
        return self.state == self.DRAGGING

    def release(self, x: float, y: float) -> str:
        """-> 'click' | 'drag_end' | 'none'."""
        state, self.state = self.state, self.IDLE
        if state == self.PENDING and self._within_slop(x, y):
            return "click"
        if state == self.DRAGGING:
            return "drag_end"
        return "none"


def clamp_position(x: int, y: int, w: int, h: int,
                   bounds: tuple[int, int, int, int]) -> tuple[int, int]:
    """Keep the pill window fully inside the virtual screen (all monitors).
    A saved position from an unplugged monitor must never strand it."""
    vx, vy, vw, vh = bounds
    x = max(vx, min(x, vx + max(0, vw - w)))
    y = max(vy, min(y, vy + max(0, vh - h)))
    return x, y


def virtual_screen() -> tuple[int, int, int, int]:
    """(x, y, w, h) of the Windows virtual screen spanning every monitor."""
    import ctypes
    try:
        m = ctypes.windll.user32.GetSystemMetrics
        return m(76), m(77), m(78), m(79)  # SM_[XY]VIRTUALSCREEN, SM_C[XY]...
    except Exception:
        return 0, 0, 10_000, 10_000        # fail open: clamp becomes a no-op


class WaveformOverlay:
    """States: loading | warming | ready (flash) | rec | busy | menu | idle
    (chip). `warming` (M6): models are live but the background warmup pass
    is still running — dictation already works, just a touch slower.

    Visibility modes (M7, revised): "always" keeps the idle chip on
    screen; "dictating" withdraws the window the moment it goes idle (no
    linger — Jeremy: no brief re-show after the paste). "never" exists
    only for overlay_enabled=false / --no-overlay compat; no UI offers it.
    The Tk root and this thread exist in EVERY mode — only the window
    hides — so mode switches are instant and the settings window (M8)
    always has a UI thread to live on.

    Position (M7, revised): the pill is locked; right-click menu ->
    "Unlock position" makes the next left-drag move it (green tint while
    unlocked), release saves the anchor (center-x, bottom-y, logical px)
    through on_position_change and relocks. Clamped to the virtual screen
    at draw time.

    The right-click menu (revised round 2) is four items only — Unlock
    position, Reset position, Settings, Quit — drawn ABOVE the pill so it
    never covers it. Everything else lives in the tray menu and, from M8,
    the settings window. `pill_size` scales the whole pill (M8 exposes it
    in settings)."""

    def __init__(self, hotkey: str = "alt+`", settings_path=None,
                 on_toggle=None, on_quit=None, visibility: str = "always",
                 position: list[int] | tuple[int, int] | None = None,
                 pill_size: float = 1.0):
        self._hotkey = hotkey
        # Set (or reset) these any time before the user clicks:
        self.settings_path = settings_path
        self.on_toggle = on_toggle    # click: start/stop dictation
        self.on_quit = on_quit        # menu Quit: ask the app to shut down
        self.on_copy_last = None      # menu Copy: returns the copied text|None
        self.on_narrate = None        # menu Narrate: speak the selection aloud
        self.on_position_change = None  # called with [x, y] on drag, None on reset
        self.on_settings = None       # menu Settings row; None -> open the toml
        self.on_restart = None        # menu Restart row; None -> row hidden
        # M8: app-provided factory building the settings window on THIS
        # overlay's UI thread (called with the Tk root). None -> toml.
        self.settings_factory = None
        self._pill_size = min(2.0, max(0.5, float(pill_size)))
        self._visibility = visibility if visibility in (
            "always", "dictating", "never") else "always"
        self._pos = tuple(position) if position else None  # (cx, by) logical
        self._cmd: queue.Queue = queue.Queue()
        self._levels: collections.deque = collections.deque(maxlen=_BARS)
        self._state = "idle"
        # Startup loading bar (0..1) + its label. Only two states are ever
        # shown to the user: "loading" (this bar) and the usable idle chip.
        self._progress = 0.0
        self._progress_label = "Starting..."
        self._thread = threading.Thread(
            target=self._ui_thread, daemon=True, name="localflow-overlay")
        self._thread.start()

    # -- any-thread API ----------------------------------------------------

    def push_level(self, rms: float) -> None:
        self._levels.append(float(rms))

    def set_state(self, state: str) -> None:
        self._cmd.put(state)

    def set_progress(self, fraction: float, label: str = "") -> None:
        """Startup loading bar: fraction 0..1 with an optional label. Puts the
        pill in the 'loading' state (the bar eases toward the fraction). The app
        calls this as each startup stage completes; set_state('idle') ends it."""
        frac = max(0.0, min(1.0, float(fraction)))
        self._cmd.put(("progress", (frac, str(label))))

    def set_visibility(self, mode: str) -> None:
        """'always' | 'dictating' | 'never' — takes effect immediately."""
        if mode in ("always", "dictating", "never"):
            self._cmd.put(("visibility", mode))

    def reset_position(self) -> None:
        """Back to default bottom-center; reports None to on_position_change."""
        self._cmd.put(("reset_pos", None))

    def set_pill_size(self, size: float) -> None:
        """Live pill_size change (0.5-2.0). Rescales and re-lays-out on the
        UI thread — never tears the root down, so it is safe to call while a
        settings window is open over the pill."""
        self._cmd.put(("rescale", size))

    def flash(self, message: str) -> None:
        """Transient message in the pill from any thread (e.g. 'Nothing
        heard' after a dictation produced no text - Jeremy 11 Jul: a silent
        failure was invisible). Ignored while recording or in a menu so it
        can never stomp a live dictation's display."""
        self._cmd.put(("flash", str(message)))

    def open_settings(self) -> None:
        """Open the settings window on the UI thread (tray + pill menu)."""
        self._cmd.put(("open_settings", None))

    def close(self) -> None:
        self._cmd.put("quit")
        self._thread.join(timeout=2.0)

    # -- UI thread only ----------------------------------------------------

    def _ui_thread(self) -> None:
        # Initialize COM on this thread and never uninitialize it (the app
        # hard-exits with os._exit). Without this, when Python's cyclic GC
        # happens to finalize a pycaw/comtypes object (from micwatch/micgain)
        # ON this Tk thread, comtypes' Release runs with no COM context and
        # access-violates in _ctypes (the 13-14 Jul native crash, caught mid
        # menu-hover). A COM-initialized thread makes that Release safe.
        try:
            import comtypes
            comtypes.CoInitialize()
        except Exception:
            log.debug("overlay CoInitialize failed", exc_info=True)
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
        heal = {"n": 0}                      # visibility self-heal countdown
        prog = {"v": 0.0}                    # eased loading-bar fill

        # UI element sizes scale with DPI x pill_size; SCREEN coordinates
        # (the saved anchor) scale with DPI only. Held in a mutable cell so a
        # live pill_size change (the ("rescale", size) command) can update it
        # and re-layout on THIS thread — no root teardown, no worker-thread Tk.
        scale = {"U": S * self._pill_size}

        def px(v: float) -> int:
            return int(round(v * scale["U"]))

        def fnt(pt: int) -> tuple[str, int]:
            return ("Segoe UI", max(8, int(round(pt * self._pill_size))))

        def place_xy(w: int, h: int) -> tuple[int, int]:
            """Window top-left for the current anchor. Custom anchor is
            (center-x, bottom-y) in logical px so the chip and the expanded
            capsule stay visually planted on the same spot; always clamped
            to the virtual screen (monitor changes can't strand the pill)."""
            if self._pos is not None:
                x = int(round(self._pos[0] * S)) - w // 2
                y = int(round(self._pos[1] * S)) - h
            else:
                x = (screen_w - w) // 2
                y = screen_h - int(round(_BOTTOM_MARGIN * S)) - h
            return clamp_position(x, y, w, h, virtual_screen())

        def layout(w_log: float, h_log: float) -> None:
            w, h = px(w_log), px(h_log)
            resized = (size["w"], size["h"]) != (w, h)
            size["w"], size["h"] = w, h
            if arbiter.state != PressArbiter.DRAGGING:
                x, y = place_xy(w, h)
                root.geometry(f"{w}x{h}+{x}+{y}")
            if resized:
                canvas.configure(width=w, height=h)
            canvas.delete("all")
            draw_capsule()

        def rounded_rect(x0: int, y0: int, x1: int, y1: int, fill: str,
                         tags: str) -> None:
            hh = y1 - y0
            r = hh // 2
            canvas.create_oval(x0, y0, x0 + hh, y1, fill=fill, outline=fill,
                               tags=tags)
            canvas.create_oval(x1 - hh, y0, x1, y1, fill=fill, outline=fill,
                               tags=tags)
            canvas.create_rectangle(x0 + r, y0, x1 - r, y1, fill=fill,
                                    outline=fill, tags=tags)

        def draw_capsule() -> None:
            w, h = size["w"], size["h"]
            # Green tint = position unlocked, the next drag moves the pill.
            fill = (_DRAG_ARM if drag_ui["unlocked"]
                    else _BG_HOVER if hover["on"] else _BG)
            rounded_rect(0, 0, w, h, fill, "capsule")

        def show() -> None:
            root.deiconify()
            root.lift()
            root.attributes("-topmost", True)

        def sync_visibility() -> None:
            """Show or hide the WINDOW for the current mode + state (M7,
            revised: idle in "dictating" mode hides IMMEDIATELY — no
            linger). The Tk root and thread stay alive in every mode. An
            unlocked pill is always shown (it must be draggable)."""
            if drag_ui["unlocked"]:
                show()
                return
            mode = self._visibility
            if mode == "never":
                root.withdraw()
                return
            if mode == "dictating" and self._state == "idle":
                root.withdraw()
                return
            show()

        def center_text(message: str, fill: str = _TEXT) -> None:
            canvas.create_text(size["w"] // 2, size["h"] // 2, text=message,
                               fill=fill, font=fnt(11), tags="text")

        def draw_chip_at(x0: int, y0: int, h: int) -> None:
            r = px(5)
            cy = y0 + h // 2
            dot_x = x0 + px(18)
            canvas.create_oval(dot_x - r, cy - r, dot_x + r, cy + r,
                               fill=_DOT_RUN, outline=_DOT_RUN, tags="text")
            canvas.create_text(x0 + px(32), cy, anchor="w", text="LocalFlow",
                               fill=_TEXT_DIM, font=fnt(10), tags="text")

        def draw_chip() -> None:
            draw_chip_at(0, 0, size["h"])

        # Right-click menu (revised round 2): four items, drawn ABOVE the
        # pill in the same window so the pill itself is never covered.
        # "unlock" toggles: while unlocked the pill drags freely (multiple
        # drags) until "Lock position" is picked (Jeremy, round 3).
        def menu_items() -> tuple[tuple[str, str], ...]:
            # Narrate row appears only when read-aloud is wired (on_narrate set).
            narrate = (("narrate", "Narrate selection"),) \
                if self.on_narrate is not None else ()
            restart = (("restart", "Restart"),) \
                if self.on_restart is not None else ()
            return (("copy", "Copy last dictation"),
                    *narrate,
                    ("unlock", "Lock position" if drag_ui["unlocked"]
                     else "Unlock position"),
                    ("reset", "Reset position"),
                    ("settings", "Settings"),
                    *restart,
                    ("quit", "Quit"))

        menu_geo: dict[str, tuple[int, int, int, int]] = {}

        def highlight_menu(hot: str | None) -> None:
            for key, (_x0, _y0, _x1, _y1) in menu_geo.items():
                color = _MENU_BTN_HOVER if key == hot else _MENU_BTN
                canvas.itemconfig(f"rect_{key}", fill=color, outline=color)

        def layout_menu() -> None:
            """Menu panel on top, the idle chip at the bottom of the same
            window — the window stays bottom-anchored, so the chip sits
            exactly where the pill was."""
            items = menu_items()
            pad, rh = px(6), px(_MENU_ROW_H)
            menu_h = 2 * pad + len(items) * rh
            gap = px(_MENU_GAP)
            chip_w, chip_h = px(_CHIP_W), px(_CHIP_H)
            w = max(px(_MENU_W), chip_w)
            h = menu_h + gap + chip_h
            resized = (size["w"], size["h"]) != (w, h)
            size["w"], size["h"] = w, h
            if arbiter.state != PressArbiter.DRAGGING:
                x, y = place_xy(w, h)
                root.geometry(f"{w}x{h}+{x}+{y}")
            if resized:
                canvas.configure(width=w, height=h)
            canvas.delete("all")
            canvas.create_rectangle(0, 0, w, menu_h, fill=_BG, outline=_BG,
                                    tags="menu")
            menu_geo.clear()
            y0 = pad
            for key, label in items:
                box = (pad, y0, w - pad, y0 + rh - px(4))
                menu_geo[key] = box
                canvas.create_rectangle(*box, fill=_MENU_BTN, outline=_MENU_BTN,
                                        tags=("menu", f"rect_{key}"))
                canvas.create_text(box[0] + px(10), (box[1] + box[3]) // 2,
                                   anchor="w", text=label, fill=_TEXT,
                                   font=fnt(10), tags="menu")
                y0 += rh
            cx0 = (w - chip_w) // 2
            rounded_rect(cx0, h - chip_h, cx0 + chip_w, h,
                         _BG_HOVER if hover["on"] else _BG, "capsule")
            draw_chip_at(cx0, h - chip_h, chip_h)
            # No auto-dismiss timer (stale timers from a previous open were
            # closing a REOPENED menu - found live 6 Jul). The menu closes
            # on: a row action, clicking the chip below, right-clicking the
            # pill again, or any mouse press outside the window (polled in
            # tick(), since a NOACTIVATE window gets no outside events).

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

        def draw_loading() -> None:
            # Startup loading bar (Jeremy: pill up immediately, a bar showing how
            # close to usable, only "loading" vs "usable" shown). Rendered in the
            # SAME chip footprint as the final idle pill so there is no size jump
            # when it becomes usable. prog["v"] eases toward self._progress each
            # tick so the fill glides. Labels are kept short to fit the chip.
            canvas.delete("load")
            w, h = size["w"], size["h"]
            label = self._progress_label or "Loading..."
            canvas.create_text(w // 2, int(h * 0.34), text=label, fill=_TEXT_DIM,
                               font=fnt(9), tags="load")
            bx0, bx1 = px(14), w - px(14)
            by, bh = int(h * 0.66), px(3)
            canvas.create_rectangle(bx0, by - bh, bx1, by + bh, fill=_MENU_BTN,
                                    outline=_MENU_BTN, tags="load")
            fx = bx0 + int((bx1 - bx0) * max(0.0, min(1.0, prog["v"])))
            if fx > bx0:
                canvas.create_rectangle(bx0, by - bh, fx, by + bh, fill=_DOT_RUN,
                                        outline=_DOT_RUN, tags="load")

        def apply(state: str) -> None:
            if state == "ready" and self._state in ("rec", "busy", "menu"):
                # The post-warmup "ready" flash (M6) arrives from the warmup
                # thread; if a dictation or the menu is on screen, it loses.
                return
            if state == "idle" and self._state == "flash":
                # The worker sends "idle" right behind a no-output flash —
                # the flash must survive it (end_flash lands idle itself).
                return
            self._state = state
            if state == "loading":
                layout(_CHIP_W, _CHIP_H)     # same size as the final idle chip
                draw_loading()
            elif state == "warming":
                layout(_EXP_W, _EXP_H)
                center_text(f"Warming up - {self._hotkey} works", _TEXT_DIM)
            elif state == "ready":
                layout(_EXP_W, _EXP_H)
                center_text(f"Ready - {self._hotkey} or click")
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
                layout_menu()
            else:                            # idle -> persistent chip
                layout(_CHIP_W, _CHIP_H)
                draw_chip()
            sync_visibility()

        def end_flash() -> None:
            if self._state == "flash":
                self._state = "idle"    # leave "flash" so apply's guard
                apply("idle")           # doesn't swallow this idle

        def show_flash(message: str, fill: str = _TEXT) -> None:
            # Transient confirmation (e.g. after "Copy last dictation"): the
            # expanded capsule with a message, then back to idle. Not a real
            # state-machine entry — a genuine state command from the queue
            # overrides it (except "idle", which the flash outlives), and
            # clicks are ignored while it shows.
            self._state = "flash"
            layout(_EXP_W, _EXP_H)
            center_text(message, fill)
            sync_visibility()
            root.after(_FLASH_MS, end_flash)

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
            """Which menu row (if any) the pixel is on. Pure geometry —
            Tk's 'current' item tag proved unreliable on this NOACTIVATE
            window (found live 3 Jul: Settings clicks silently did nothing)."""
            for key, (x0, y0, x1, y1) in menu_geo.items():
                if x0 <= x <= x1 and y0 <= y <= y1:
                    return key
            return None

        # -- click vs unlocked-drag (M7, revised) ------------------------
        arbiter = PressArbiter(slop_px=px(_DRAG_SLOP_LOG))
        drag_ui = {"unlocked": False, "win": (0, 0), "cur": (0, 0)}

        def on_press(event) -> None:
            # Menu presses are always clicks - otherwise an unlocked pill
            # would start dragging when the user tries to hit
            # "Lock position" in the menu itself.
            arbiter.press(event.x_root, event.y_root,
                          unlocked=drag_ui["unlocked"]
                          and self._state != "menu")
            drag_ui["cur"] = (event.x_root, event.y_root)
            drag_ui["win"] = (root.winfo_x(), root.winfo_y())

        def on_b1motion(event) -> None:
            if arbiter.motion(event.x_root, event.y_root):
                dx = event.x_root - drag_ui["cur"][0]
                dy = event.y_root - drag_ui["cur"][1]
                root.geometry(f"+{drag_ui['win'][0] + dx}"
                              f"+{drag_ui['win'][1] + dy}")

        def notify_position(pos) -> None:
            if self.on_position_change is None:
                return
            try:
                self.on_position_change(pos)
            except Exception:
                log.exception("overlay position callback failed")

        def on_release(event) -> None:
            outcome = arbiter.release(event.x_root, event.y_root)
            if outcome == "click":
                on_click(event)
                return
            if outcome == "drag_end":
                # Persist the anchor (window center-bottom, logical px).
                # The pill STAYS unlocked - Jeremy: relock explicitly via
                # right-click "Lock position", so it can be nudged again.
                w, h = size["w"], size["h"]
                x, y = clamp_position(root.winfo_x(), root.winfo_y(), w, h,
                                      virtual_screen())
                self._pos = (int(round((x + w / 2) / S)),
                             int(round((y + h) / S)))
                apply(self._state)           # re-layout at the final spot
                notify_position(list(self._pos))

        def on_click(event) -> None:
            st = self._state
            if st == "menu":
                hit = menu_hit(event.x, event.y)
                if hit == "copy":
                    copied = None
                    if self.on_copy_last is not None:
                        try:
                            copied = self.on_copy_last()
                        except Exception:
                            log.exception("overlay copy callback failed")
                    show_flash("Copied last dictation" if copied
                               else "No dictation yet",
                               _TEXT if copied else _TEXT_DIM)
                elif hit == "narrate":
                    # Pill never takes focus (WS_EX_NOACTIVATE), so the source
                    # app keeps its selection - Ctrl+C in on_narrate copies it.
                    apply("idle")
                    call_safely(self.on_narrate, "narrate")
                elif hit == "unlock":
                    # Toggle: unlock -> drag freely (green tint) until
                    # "Lock position" is picked here again.
                    drag_ui["unlocked"] = not drag_ui["unlocked"]
                    apply("idle")
                elif hit == "reset":
                    self._pos = None
                    apply("idle")
                    notify_position(None)
                elif hit == "settings":
                    apply("idle")
                    # M8 wires on_settings to the real settings window;
                    # until then fall back to opening the toml.
                    if self.on_settings is not None:
                        call_safely(self.on_settings, "settings")
                    else:
                        open_settings()
                elif hit == "restart":
                    # Same as tray Restart: the teardown spawns the fresh
                    # instance, so this just asks the app to shut down.
                    call_safely(self.on_restart, "restart")
                elif hit == "quit":
                    call_safely(self.on_quit, "quit")
                else:                        # off any row -> dismiss
                    apply("idle")
            elif st in ("idle", "ready", "warming", "rec", "flash"):
                # toggle dictation; focus stays on the paste target thanks
                # to WS_EX_NOACTIVATE
                call_safely(self.on_toggle, "toggle")
            # loading/busy: ignore clicks

        def on_rclick(_event) -> None:
            if self._state == "menu":        # right-click again = close
                apply("idle")
            elif self._state in ("idle", "ready", "warming", "busy", "flash"):
                apply("menu")

        def on_motion(event) -> None:
            if self._state != "menu":
                return
            highlight_menu(menu_hit(event.x, event.y))

        def set_hover(on: bool) -> None:
            hover["on"] = on
            if drag_ui["unlocked"]:
                return                       # never repaint over the cue
            fill = _BG_HOVER if on else _BG
            canvas.itemconfig("capsule", fill=fill, outline=fill)

        def on_leave(_event) -> None:
            set_hover(False)
            if self._state == "menu":
                highlight_menu(None)   # keep the menu open; just drop the hover

        # Button-1 now arbitrates click vs hold-to-drag; the click action
        # fires on RELEASE (within slop, under 1s) instead of on press.
        # <B1-Motion> is more specific than <Motion>, so Tk routes held-
        # button movement to the drag handler only.
        canvas.bind("<Button-1>", on_press)
        canvas.bind("<B1-Motion>", on_b1motion)
        canvas.bind("<ButtonRelease-1>", on_release)
        canvas.bind("<Button-3>", on_rclick)
        canvas.bind("<Motion>", on_motion)
        canvas.bind("<Enter>", lambda e: set_hover(True))
        canvas.bind("<Leave>", on_leave)

        menu_out = {"pressed": False}

        def poll_outside_click() -> None:
            """Close the open menu on any mouse press OUTSIDE the window.
            The window is WS_EX_NOACTIVATE, so outside presses never reach
            Tk - poll the global button state on the UI tick instead."""
            try:
                from ctypes import windll, wintypes, byref
                u32 = windll.user32
                down = ((u32.GetAsyncKeyState(0x01) | u32.GetAsyncKeyState(0x02))
                        & 0x8000)
                if not down:
                    menu_out["pressed"] = False
                    return
                if menu_out["pressed"]:
                    return                   # same press, already assessed
                menu_out["pressed"] = True
                pt = wintypes.POINT()
                u32.GetCursorPos(byref(pt))
                x0, y0 = root.winfo_rootx(), root.winfo_rooty()
                if not (x0 <= pt.x < x0 + size["w"]
                        and y0 <= pt.y < y0 + size["h"]):
                    apply("idle")
            except Exception:
                log.exception("outside-click poll failed")

        def should_be_visible() -> bool:
            """Whether the window is meant to be on screen right now — the same
            decision sync_visibility() makes, factored out for the self-heal."""
            if drag_ui["unlocked"]:
                return True
            mode = self._visibility
            if mode == "never":
                return False
            if mode == "dictating" and self._state == "idle":
                return False
            return True

        def heal_visibility() -> None:
            # Windows can quietly hide a topmost tool window (full-screen app,
            # lock/unlock, display change) and it never returns on its own —
            # the fix Jeremy used, re-picking "Always" from the tray, re-runs
            # deiconify+lift+topmost. Do that here on a slow heartbeat:
            #  - unmapped (winfo_viewable 0): re-apply the state (re-place + show)
            #  - mapped but possibly buried: reclaim the topmost band
            # This matches the always-on-top guarantee and is the same
            # re-assert already done on every state change.
            if not should_be_visible():
                return
            try:
                if not root.winfo_viewable():
                    log.info("pill was hidden by the OS — re-showing it")
                    apply(self._state)
                else:
                    root.attributes("-topmost", True)
            except Exception:
                log.debug("overlay visibility heal failed", exc_info=True)

        def tick() -> None:
            try:
                try:
                    while True:
                        cmd = self._cmd.get_nowait()
                        if cmd == "quit":
                            root.destroy()
                            return
                        if isinstance(cmd, tuple):     # M7 ops, not states
                            op, arg = cmd
                            if op == "visibility":
                                self._visibility = arg
                                sync_visibility()
                            elif op == "rescale":
                                # Live pill_size change (M8), on the overlay's
                                # own thread: update the scale cell and re-layout
                                # the current state. Replaces the old tear-down-
                                # the-whole-root rebuild, which crashed natively
                                # when a settings window was open over it.
                                self._pill_size = max(0.5, min(2.0, float(arg)))
                                scale["U"] = S * self._pill_size
                                arbiter.slop = px(_DRAG_SLOP_LOG)
                                size["w"] = size["h"] = 0   # force resize
                                apply(self._state)
                            elif op == "reset_pos":
                                self._pos = None
                                apply(self._state)     # snap to bottom-center
                                notify_position(None)
                            elif op == "flash":
                                # Never over a live dictation, a menu, or the
                                # startup bar - those displays win.
                                if self._state not in ("rec", "menu",
                                                       "loading"):
                                    show_flash(arg, _TEXT_DIM)
                            elif op == "open_settings":
                                if self.settings_factory is not None:
                                    try:
                                        self.settings_factory(root)
                                    except Exception:
                                        log.exception("settings window failed")
                                else:
                                    open_settings()    # escape hatch: the toml
                            elif op == "progress":
                                self._progress, self._progress_label = arg
                                # Never clobber an active dictation/menu with the
                                # loading bar (a late startup tick during a first
                                # dictation); just record it.
                                if self._state not in ("rec", "busy", "menu",
                                                       "loading"):
                                    prog["v"] = 0.0    # start empty on entry
                                    apply("loading")
                            continue
                        apply(cmd)
                except queue.Empty:
                    pass
                if self._state == "rec":
                    draw_wave()
                elif self._state == "loading":
                    prog["v"] += (self._progress - prog["v"]) * 0.25  # ease fill
                    draw_loading()
                elif self._state == "menu":
                    poll_outside_click()
                heal["n"] += 1
                if heal["n"] >= _HEAL_TICKS:
                    heal["n"] = 0
                    heal_visibility()
            except Exception:
                # A draw/command error must never stop the loop — that would
                # freeze the pill until restart. Log and keep ticking.
                log.exception("overlay tick failed — recovering")
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
