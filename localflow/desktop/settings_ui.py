"""M8 settings window: a normal, focusable Toplevel for editing localflow.toml.

Threading model: the window lives on the overlay's Tk UI thread - the app
calls open_settings() with the overlay's (hidden, overrideredirect) root via
the overlay command queue, and every widget touch stays on that thread.
Worker threads (ctx.apply, the mic test, the pynput hotkey capture) never
call widget methods directly; they marshal back with widget.after(). ctx.apply
runs on a worker thread on purpose: a pill_size change tears down and rebuilds
the overlay's whole Tk root, which destroys THIS window mid-apply. Completion
callbacks therefore treat a dead widget (TclError) as normal, not an error.

The pure helpers up top (PRESETS, preset_name_for, key_name_of, modifier_of,
format_captured, mic_selection, collect_changes, coerce_changes,
history_row_label, rule_rows, suggestion_rows) carry the logic that gets
unit-tested; nothing in them touches Tk or pynput listeners.
"""

from __future__ import annotations

import logging
import threading
import time
import tkinter as tk
from tkinter import ttk

from pynput import keyboard

log = logging.getLogger("localflow.desktop.settings_ui")

_METER_W, _METER_H = 140, 10
_WRAP = 380                     # wraplength for transcript / status labels

_RED = "#c62828"
_WARN = "#b26a00"
_DIM = "#777777"
_METER_BG = "#22252a"
_METER_BAR = "#57c785"          # same green family as the pill's run dot

_VIS_LABELS = {"always": "Always", "dictating": "While dictating"}
_VIS_BY_LABEL = {v: k for k, v in _VIS_LABELS.items()}


# -- pure helpers (unit-tested, no Tk) -----------------------------------

# Environment presets set the audio controls (denoise strength, speech
# sensitivity, normalize). Values are first guesses from the v1.1 plan;
# M9 tunes them live.
PRESETS: dict[str, tuple[float, float, bool]] = {
    "Quiet room": (0.3, 0.5, True),
    "Noisy room": (0.75, 0.6, True),
    "Headset": (0.2, 0.45, True),
}


def preset_name_for(denoise_strength: float, sensitivity: float,
                    normalize: bool) -> str:
    """Which preset matches the current audio values (tolerance 0.01), else
    'Custom'. Used both to initialize the combobox and to flip it back when
    a hand-moved slider happens to land exactly on a preset."""
    for name, (strength, sens, norm) in PRESETS.items():
        if (abs(strength - denoise_strength) <= 0.01
                and abs(sens - sensitivity) <= 0.01
                and norm == bool(normalize)):
            return name
    return "Custom"


_MOD_NAMES = {
    "ctrl": "ctrl", "ctrl_l": "ctrl", "ctrl_r": "ctrl",
    "alt": "alt", "alt_l": "alt", "alt_r": "alt", "alt_gr": "alt",
    "shift": "shift", "shift_l": "shift", "shift_r": "shift",
}
_MOD_ORDER = ("ctrl", "alt", "shift")
# The win key is not a supported modifier and makes a poor main key - the
# capture loop skips it entirely and keeps listening.
_CAPTURE_IGNORED = {"cmd", "cmd_l", "cmd_r"}


def modifier_of(key) -> str | None:
    """pynput key -> canonical modifier name ('ctrl'/'alt'/'shift'), or None
    for anything that is not a supported modifier."""
    name = getattr(key, "name", None)
    return _MOD_NAMES.get(name) if name else None


def key_name_of(key) -> str | None:
    """pynput key -> the config-format key name ('d', 'f9', 'space', '`').

    KeyCode.char wins, lowercased; while ctrl is held Windows delivers the
    control character instead of the letter ('\\x04' for ctrl+d), so those
    map back to the letter. Named keys use Key.name ('f9', 'space'). Falls
    back to the virtual-key code for letters/digits when char is missing.
    None means the key has no usable name (dead keys, media keys)."""
    char = getattr(key, "char", None)
    if char:
        if len(char) == 1 and 1 <= ord(char) <= 26:
            return chr(ord(char) + 96)      # '\x04' -> 'd'
        return char.lower()
    name = getattr(key, "name", None)
    if name:
        return name
    vk = getattr(key, "vk", None)
    if vk is not None:
        if 0x41 <= vk <= 0x5A:              # A..Z
            return chr(vk).lower()
        if 0x30 <= vk <= 0x39:              # 0..9
            return chr(vk)
    return None


def format_captured(mods: list[str], key_name: str) -> str:
    """Held modifiers + main key -> the config combo format, modifiers in
    canonical ctrl/alt/shift order: (['shift', 'ctrl'], 'd') -> 'ctrl+shift+d'.
    This is the inverse of app.to_pynput_combo's input format."""
    ordered = [m for m in _MOD_ORDER if m in mods]
    return "+".join(ordered + [key_name])


def mic_selection(mics: list[tuple[str, object]],
                  configured) -> tuple[list[tuple[str, object]], int]:
    """(rows, preselect index) for the mic combobox given cfg.input_device.

    None follows the system default (row 0). Ints match the device index;
    strings match a case-insensitive name substring (same semantics as
    StreamRecorder), skipping the row-0 default entry so a pin like
    'default' cannot false-match it. A pin that matches nothing gets an
    explicit '(pinned: ...)' row so the config state stays visible."""
    rows = list(mics)
    if configured in (None, ""):
        return rows, 0
    if isinstance(configured, str) and configured.isdigit():
        configured = int(configured)
    if isinstance(configured, int):
        for i, (_label, value) in enumerate(rows):
            if value == configured:
                return rows, i
    else:
        needle = str(configured).lower()
        for i, (label, _value) in enumerate(rows[1:], start=1):
            if needle in label.lower():
                return rows, i
    rows.append((f"(pinned: {configured})", configured))
    return rows, len(rows) - 1


def collect_changes(initial: dict, current: dict) -> dict:
    """Only the values the user actually changed since the window opened.

    denoise_enabled has no checkbox of its own - it derives from the
    strength slider (0 means off, per the config comment), and is only
    written when the slider moved AND the on/off state actually flips."""
    missing = object()
    changes = {k: v for k, v in current.items() if initial.get(k, missing) != v}
    if "denoise_strength" in changes:
        enabled = changes["denoise_strength"] > 0.0
        if enabled != initial.get("denoise_enabled"):
            changes["denoise_enabled"] = enabled
    return changes


_COERCERS = {
    "denoise_strength": float,
    "vad_threshold": float,
    "restore_delay_ms": int,
    "pill_size": float,
    "cleanup_enabled": bool,
    "denoise_enabled": bool,
    "normalize_audio": bool,
    "overlay_visibility": str,
    "inject_mode": str,
    "hotkey": str,
    "device": str,
}


def coerce_changes(changes: dict) -> dict:
    """Pin the exact types ctx.apply expects. Tk variables hand back strings
    and 0/1 ints more often than not; input_device passes through untouched
    (it is legitimately int, str, or None)."""
    out = {}
    for key, value in changes.items():
        coerce = _COERCERS.get(key)
        out[key] = coerce(value) if coerce is not None and value is not None else value
    return out


# M10 history/dictionary list formatting. The cap keeps a row on one
# Listbox line at the window's fixed width; 50 rows is plenty to find a
# recent dictation without making the refresh rebuild feel slow.
_HIST_TEXT_MAX = 60
_HIST_MAX_ROWS = 50


def history_row_label(entry: dict) -> str:
    """One history Listbox row: 'HH:MM  text'. Shows the ORIGINAL final
    text even when a correction exists - the leading '*' is what says
    'edited', and the correction itself appears in the edit box on
    selection. Newlines are flattened so a row can never wrap."""
    when = time.strftime("%H:%M", time.localtime(float(entry.get("ts") or 0.0)))
    text = " ".join(str(entry.get("final_text") or "").split())
    if len(text) > _HIST_TEXT_MAX:
        text = text[:_HIST_TEXT_MAX - 3] + "..."
    mark = "* " if entry.get("corrected_text") else ""
    return f"{when}  {mark}{text}"


def rule_rows(rules: dict) -> list[str]:
    """Dictionary Listbox lines, sorted by source so row order is stable
    across rebuilds regardless of dict insertion order (selection indexes
    map back through the same sort)."""
    return [f"{source}  ->  {rules[source]}" for source in sorted(rules)]


def suggestion_rows(suggestions) -> list[str]:
    """Suggestion Listbox lines from mine_substitutions-shaped
    (source, target, count) tuples."""
    return [f"{source} -> {target}  (seen {count}x)"
            for source, target, count in suggestions]


# -- context handed over from app._run() ---------------------------------

class SettingsContext:
    """Everything the window needs from the running app, as plain attributes.
    Constructed keyword-only in app.wire_settings_ui - the field list there
    and here must stay in lockstep."""

    def __init__(self, *, cfg, apply, list_mics, mic_name, fallback_active,
                 latest_level, test_mic, validate_hotkey, reset_pill_position,
                 open_config_file, status_text, cleanup_available,
                 cleanup_active, calibrate=None, clear_calibration=None,
                 calibration_status=None, history_entries=None,
                 save_correction=None, clear_history=None, suggestions=None):
        self.cfg = cfg
        self.apply = apply
        self.list_mics = list_mics
        self.mic_name = mic_name
        self.fallback_active = fallback_active
        self.latest_level = latest_level
        self.test_mic = test_mic
        self.validate_hotkey = validate_hotkey
        self.reset_pill_position = reset_pill_position
        self.open_config_file = open_config_file
        self.status_text = status_text
        self.cleanup_available = cleanup_available
        self.cleanup_active = cleanup_active
        # M9: room-noise calibration (None on older wiring = buttons stay
        # disabled, same as the M8 placeholders).
        self.calibrate = calibrate
        self.clear_calibration = clear_calibration
        self.calibration_status = calibration_status
        # M10: history panel + dictionary miner (None -> sections hidden).
        self.history_entries = history_entries
        self.save_correction = save_correction
        self.clear_history = clear_history
        self.suggestions = suggestions


# -- the window (overlay UI thread only) ----------------------------------

# Singleton holder. The referenced window can die two ways: the user closes
# it, or a pill_size rebuild tears down the whole Tk root under it - so the
# holder is only ever trusted after a winfo_exists check under TclError.
_holder: dict = {"win": None}


def open_settings(root, ctx) -> None:
    """Open (or re-focus) the settings window. Must be called on the
    overlay's Tk UI thread with its hidden root."""
    win = _holder.get("win")
    if win is not None:
        try:
            if win.top.winfo_exists():
                win.top.deiconify()
                win.top.lift()
                win.top.focus_force()
                return
        except tk.TclError:
            pass    # its root was torn down (pill rebuild) - build fresh
        _holder["win"] = None
    try:
        _holder["win"] = _SettingsWindow(root, ctx)
    except Exception:
        _holder["win"] = None
        log.exception("settings window failed to build")


class _SettingsWindow:

    def __init__(self, root, ctx: SettingsContext):
        self.ctx = ctx
        self._capturing = False
        self._capture_listener = None
        self._applying_preset = False
        self._has_cleanup = bool(ctx.cleanup_available)

        top = tk.Toplevel(root)
        self.top = top
        top.title("LocalFlow Settings")
        # Deliberately NOT the pill's WS_EX_NOACTIVATE styling: this is a
        # regular window with a title bar that takes focus like any other.
        top.resizable(False, True)
        top.protocol("WM_DELETE_WINDOW", self._on_close)
        top.bind("<Destroy>", self._on_destroyed)

        # The stacked sections outgrow a 1600px screen at 165% DPI (found by
        # scripts/m8_settings_check.py: the window bottom, with Apply/Close,
        # landed off-screen). The body scrolls inside a height-capped canvas
        # and the bottom bar stays pinned to the window.
        bottombar = ttk.Frame(top, padding=(10, 6, 10, 10))
        bottombar.pack(side="bottom", fill="x")
        outer = ttk.Frame(top, padding=(10, 10, 10, 0))
        outer.pack(side="top", fill="both", expand=True)
        canvas = tk.Canvas(outer, highlightthickness=0, borderwidth=0)
        vsb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        main = ttk.Frame(canvas)
        main_id = canvas.create_window((0, 0), window=main, anchor="nw")

        def work_area() -> tuple[int, int, int, int]:
            """(left, top, right, bottom) excluding the taskbar - the window
            must never hide Apply/Close behind it."""
            try:
                import ctypes
                from ctypes import wintypes
                rect = wintypes.RECT()
                ctypes.windll.user32.SystemParametersInfoW(
                    48, 0, ctypes.byref(rect), 0)  # SPI_GETWORKAREA
                return rect.left, rect.top, rect.right, rect.bottom
            except Exception:
                return 0, 0, top.winfo_screenwidth(), top.winfo_screenheight()

        def sync_scroll(_event=None) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))
            width = main.winfo_reqwidth()
            canvas.configure(width=width)
            canvas.itemconfigure(main_id, width=width)
            _l, wa_top, _r, wa_bottom = work_area()
            usable = ((wa_bottom - wa_top) - bottombar.winfo_reqheight()
                      - 140)                     # title bar + paddings
            canvas.configure(height=max(300, min(main.winfo_reqheight(), usable)))

        def clamp_into_work_area() -> None:
            top.update_idletasks()
            _l, wa_top, _r, wa_bottom = work_area()
            h = top.winfo_height()
            y = top.winfo_y()
            if y + h > wa_bottom:
                top.geometry(f"+{top.winfo_x()}+{max(wa_top, wa_bottom - h - 8)}")

        main.bind("<Configure>", sync_scroll)
        top.bind("<MouseWheel>", lambda e: canvas.yview_scroll(
            -int(e.delta / 120), "units"))
        def clamp_safely() -> None:
            try:
                clamp_into_work_area()
            except tk.TclError:
                pass                         # window died first - fine

        top.after(150, clamp_safely)

        self._build_status(main)
        self._build_microphone(main)
        self._build_preset(main)
        self._build_audio(main)
        self._build_hotkey(main)
        self._build_behaviour(main)
        self._build_cleanup(main)
        self._build_history(main)
        self._build_dictionary(main)
        self._build_advanced(main)
        self._build_bottom(bottombar)

        # Snapshot exactly what the widgets show now: Apply writes only what
        # differs from this, so a cfg quirk (visibility "never" shown as
        # Always, a name pin resolved to a device row) is never written back
        # unless the user actually touches the control.
        self._initial = self._read_values()
        self._initial["denoise_enabled"] = bool(ctx.cfg.denoise_enabled)

        self._poll_status()
        self._poll_level()

        top.deiconify()
        top.lift()
        top.focus_force()

    # -- sections ---------------------------------------------------------

    def _build_status(self, parent) -> None:
        cfg_box = ttk.Frame(parent)
        cfg_box.pack(fill="x", pady=(0, 6))
        ttk.Label(cfg_box, text=str(self.ctx.status_text)).pack(anchor="w")
        self.mic_line = ttk.Label(cfg_box, text="Mic: ...", foreground=_DIM)
        self.mic_line.pack(anchor="w")
        self.fb_line = ttk.Label(cfg_box, text="", foreground=_WARN)
        self.fb_line.pack(anchor="w")

    def _build_microphone(self, parent) -> None:
        box = ttk.LabelFrame(parent, text="Microphone", padding=8)
        box.pack(fill="x", pady=(0, 6))
        try:
            mics = self.ctx.list_mics()
        except Exception:
            log.exception("mic listing failed")
            mics = [("System default (follow Windows)", None)]
        self._mic_rows, self._mic_index = mic_selection(
            mics, self.ctx.cfg.input_device)
        self.mic_combo = ttk.Combobox(
            box, state="readonly", width=44,
            values=[label for label, _v in self._mic_rows])
        self.mic_combo.current(self._mic_index)
        self.mic_combo.pack(anchor="w")

        row = ttk.Frame(box)
        row.pack(fill="x", pady=(6, 0))
        ttk.Label(row, text="Level").pack(side="left")
        self.level_canvas = tk.Canvas(
            row, width=_METER_W, height=_METER_H, bg=_METER_BG,
            highlightthickness=1, highlightbackground=_DIM)
        self.level_canvas.pack(side="left", padx=(6, 12))
        self._level_bar = self.level_canvas.create_rectangle(
            1, 1, 1, _METER_H - 1, fill=_METER_BAR, outline="")
        self.test_btn = ttk.Button(row, text="Test mic",
                                   command=self._safe(self._on_test_mic))
        self.test_btn.pack(side="left")
        self.test_label = ttk.Label(box, text="", wraplength=_WRAP,
                                    foreground=_DIM)
        self.test_label.pack(anchor="w", pady=(4, 0))

    def _build_preset(self, parent) -> None:
        box = ttk.LabelFrame(parent, text="Environment preset", padding=8)
        box.pack(fill="x", pady=(0, 6))
        self.preset_var = tk.StringVar(value=preset_name_for(
            round(float(self.ctx.cfg.denoise_strength), 2),
            round(float(self.ctx.cfg.vad_threshold), 2),
            bool(self.ctx.cfg.normalize_audio)))
        combo = ttk.Combobox(box, state="readonly", width=20,
                             textvariable=self.preset_var,
                             values=list(PRESETS) + ["Custom"])
        combo.pack(anchor="w")
        combo.bind("<<ComboboxSelected>>",
                   self._safe(self._on_preset_selected))

    def _build_audio(self, parent) -> None:
        box = ttk.LabelFrame(parent, text="Audio tuning", padding=8)
        box.pack(fill="x", pady=(0, 6))

        row = ttk.Frame(box)
        row.pack(fill="x")
        ttk.Label(row, text="Denoise strength", width=18).pack(side="left")
        self.denoise_var = tk.DoubleVar(
            value=round(float(self.ctx.cfg.denoise_strength), 2))
        ttk.Scale(row, from_=0.0, to=1.0, variable=self.denoise_var,
                  length=180,
                  command=self._safe(self._on_denoise_moved)).pack(side="left")
        self.denoise_val = ttk.Label(
            row, text=f"{self.denoise_var.get():.2f}", width=5)
        self.denoise_val.pack(side="left", padx=(6, 0))

        row = ttk.Frame(box)
        row.pack(fill="x", pady=(4, 0))
        ttk.Label(row, text="Speech sensitivity", width=18).pack(side="left")
        self.sens_var = tk.DoubleVar(
            value=round(float(self.ctx.cfg.vad_threshold), 2))
        ttk.Scale(row, from_=0.3, to=0.7, variable=self.sens_var, length=180,
                  command=self._safe(self._on_sens_moved)).pack(side="left")
        self.sens_val = ttk.Label(row, text=f"{self.sens_var.get():.2f}",
                                  width=5)
        self.sens_val.pack(side="left", padx=(6, 0))
        ttk.Label(box, text="lower = quiet speech accepted, "
                            "higher = background noise gated",
                  foreground=_DIM).pack(anchor="w")

        self.normalize_var = tk.BooleanVar(
            value=bool(self.ctx.cfg.normalize_audio))
        ttk.Checkbutton(box, text="Normalize volume",
                        variable=self.normalize_var,
                        command=self._safe(self._on_audio_touched)
                        ).pack(anchor="w", pady=(4, 0))

        # M9: room-noise calibration. Buttons stay disabled when the app
        # wiring predates the feature (ctx.calibrate is None).
        row = ttk.Frame(box)
        row.pack(fill="x", pady=(4, 0))
        self.calibrate_btn = ttk.Button(
            row, text="Calibrate room noise",
            command=self._safe(self._on_calibrate))
        self.calibrate_btn.pack(side="left")
        self.clear_cal_btn = ttk.Button(
            row, text="Clear calibration",
            command=self._safe(self._on_clear_calibration))
        self.clear_cal_btn.pack(side="left", padx=(6, 0))
        if self.ctx.calibrate is None:
            self.calibrate_btn.state(["disabled"])
            self.clear_cal_btn.state(["disabled"])
        self.cal_status = ttk.Label(box, text=self._calibration_text(),
                                    foreground=_DIM, wraplength=_WRAP)
        self.cal_status.pack(anchor="w", pady=(4, 0))

    def _build_hotkey(self, parent) -> None:
        box = ttk.LabelFrame(parent, text="Hotkey", padding=8)
        box.pack(fill="x", pady=(0, 6))
        row = ttk.Frame(box)
        row.pack(fill="x")
        self.hotkey_var = tk.StringVar(value=self.ctx.cfg.hotkey)
        ttk.Entry(row, textvariable=self.hotkey_var, state="readonly",
                  width=20).pack(side="left")
        self.hotkey_btn = ttk.Button(row, text="Change...",
                                     command=self._safe(self._start_capture))
        self.hotkey_btn.pack(side="left", padx=(6, 0))
        self.hotkey_note = ttk.Label(box, text="", wraplength=_WRAP,
                                     foreground=_DIM)
        self.hotkey_note.pack(anchor="w", pady=(4, 0))

    def _build_behaviour(self, parent) -> None:
        box = ttk.LabelFrame(parent, text="Behaviour", padding=8)
        box.pack(fill="x", pady=(0, 6))

        row = ttk.Frame(box)
        row.pack(fill="x")
        ttk.Label(row, text="Show pill", width=18).pack(side="left")
        # cfg "never" exists only for --no-overlay compat; the UI shows
        # Always and only writes overlay_visibility if the user changes it
        # (the initial-snapshot diff takes care of that).
        shown = _VIS_LABELS.get(self.ctx.cfg.overlay_visibility, "Always")
        self.visibility_var = tk.StringVar(value=shown)
        ttk.Combobox(row, state="readonly", width=16,
                     textvariable=self.visibility_var,
                     values=list(_VIS_LABELS.values())).pack(side="left")

        row = ttk.Frame(box)
        row.pack(fill="x", pady=(4, 0))
        ttk.Label(row, text="Pill size", width=18).pack(side="left")
        self.pill_var = tk.DoubleVar(
            value=round(float(self.ctx.cfg.pill_size), 2))
        ttk.Scale(row, from_=0.5, to=2.0, variable=self.pill_var, length=180,
                  command=self._safe(self._on_pill_moved)).pack(side="left")
        self.pill_val = ttk.Label(row, text=f"{self.pill_var.get():.2f}",
                                  width=5)
        self.pill_val.pack(side="left", padx=(6, 0))
        ttk.Label(box, text="applies live - the pill restarts itself",
                  foreground=_DIM).pack(anchor="w")

        ttk.Button(box, text="Reset pill position",
                   command=self._safe(self._on_reset_position)
                   ).pack(anchor="w", pady=(4, 0))

        row = ttk.Frame(box)
        row.pack(fill="x", pady=(4, 0))
        ttk.Label(row, text="Paste method", width=18).pack(side="left")
        self.inject_var = tk.StringVar(value=self.ctx.cfg.inject_mode)
        ttk.Combobox(row, state="readonly", width=8,
                     textvariable=self.inject_var,
                     values=["paste", "type"]).pack(side="left")

        row = ttk.Frame(box)
        row.pack(fill="x", pady=(4, 0))
        ttk.Label(row, text="Clipboard restore delay (ms)",
                  width=24).pack(side="left")
        self.delay_var = tk.StringVar(
            value=str(int(self.ctx.cfg.restore_delay_ms)))
        ttk.Spinbox(row, from_=100, to=2000, increment=50, width=7,
                    textvariable=self.delay_var).pack(side="left")

    def _build_cleanup(self, parent) -> None:
        box = ttk.LabelFrame(parent, text="Cleanup", padding=8)
        box.pack(fill="x", pady=(0, 6))
        if not self._has_cleanup:
            ttk.Label(box, text="Cleanup model not found on disk",
                      foreground=_DIM).pack(anchor="w")
            return
        try:
            active = bool(self.ctx.cleanup_active())
        except Exception:
            active = False
        self.cleanup_var = tk.BooleanVar(value=active)
        ttk.Checkbutton(box, text="Cleanup with local LLM (slower, cleaner)",
                        variable=self.cleanup_var).pack(anchor="w")
        ttk.Label(box, text="loads ~2-3GB into GPU memory while on",
                  foreground=_DIM).pack(anchor="w")

    def _build_history(self, parent) -> None:
        # M10: skipped entirely on pre-M10 wiring (the ctx fields are None),
        # same gate as ctx.calibrate for the M9 buttons. All four M10 fields
        # arrive together from app.wire_settings_ui.
        if self.ctx.history_entries is None:
            return
        box = ttk.LabelFrame(parent, text="History", padding=8)
        box.pack(fill="x", pady=(0, 6))

        self.history_var = tk.BooleanVar(
            value=bool(self.ctx.cfg.history_enabled))
        self.history_check = ttk.Checkbutton(
            box, text="Keep dictation history", variable=self.history_var,
            command=self._safe(self._on_history_toggled))
        self.history_check.pack(anchor="w")
        # Sits ABOVE the list: the list may still show leftovers from before
        # the toggle went off, and that is fine until Clear history.
        self.history_off_note = ttk.Label(box, text="", foreground=_DIM)
        self.history_off_note.pack(anchor="w")

        lb_row = ttk.Frame(box)
        lb_row.pack(fill="x", pady=(4, 0))
        self.history_list = tk.Listbox(lb_row, height=6, exportselection=False)
        hsb = ttk.Scrollbar(lb_row, orient="vertical",
                            command=self.history_list.yview)
        self.history_list.configure(yscrollcommand=hsb.set)
        self.history_list.pack(side="left", fill="x", expand=True)
        hsb.pack(side="right", fill="y")
        self.history_list.bind("<<ListboxSelect>>",
                               self._safe(self._on_history_selected))

        row = ttk.Frame(box)
        row.pack(fill="x", pady=(4, 0))
        ttk.Button(row, text="Refresh",
                   command=self._safe(self._refresh_history)).pack(side="left")
        ttk.Button(row, text="Clear history",
                   command=self._safe(self._on_clear_history)
                   ).pack(side="left", padx=(6, 0))

        ttk.Label(box, text="Select a row, fix the text, save - repeated "
                            "fixes become dictionary suggestions",
                  foreground=_DIM, wraplength=_WRAP).pack(anchor="w",
                                                          pady=(4, 0))
        row = ttk.Frame(box)
        row.pack(fill="x")
        self.correction_var = tk.StringVar()
        ttk.Entry(row, textvariable=self.correction_var).pack(
            side="left", fill="x", expand=True)
        ttk.Button(row, text="Save correction",
                   command=self._safe(self._on_save_correction)
                   ).pack(side="left", padx=(6, 0))
        self.hist_status = ttk.Label(box, text="", foreground=_DIM,
                                     wraplength=_WRAP)
        self.hist_status.pack(anchor="w", pady=(2, 0))

        self._hist_entries: list[dict] = []
        self._selected_ts: float | None = None
        self._update_history_note()
        self._refresh_history()

    def _build_dictionary(self, parent) -> None:
        if self.ctx.suggestions is None:
            return
        box = ttk.LabelFrame(parent, text="Dictionary", padding=8)
        box.pack(fill="x", pady=(0, 6))
        # Working copy: Add/Remove/Accept edit this dict and persist the
        # WHOLE thing through ctx.apply, so the window never diffs
        # rule-by-rule against what is on disk.
        self._rules: dict[str, str] = dict(self.ctx.cfg.replacements or {})
        self._rule_sources: list[str] = []

        lb_row = ttk.Frame(box)
        lb_row.pack(fill="x")
        self.rules_list = tk.Listbox(lb_row, height=5, exportselection=False)
        rsb = ttk.Scrollbar(lb_row, orient="vertical",
                            command=self.rules_list.yview)
        self.rules_list.configure(yscrollcommand=rsb.set)
        self.rules_list.pack(side="left", fill="x", expand=True)
        rsb.pack(side="right", fill="y")

        row = ttk.Frame(box)
        row.pack(fill="x", pady=(6, 0))
        self.heard_var = tk.StringVar()
        self.replace_var = tk.StringVar()
        ttk.Label(row, text="heard").pack(side="left")
        ttk.Entry(row, textvariable=self.heard_var,
                  width=16).pack(side="left", padx=(4, 8))
        ttk.Label(row, text="replace with").pack(side="left")
        ttk.Entry(row, textvariable=self.replace_var,
                  width=16).pack(side="left", padx=(4, 8))
        self.add_rule_btn = ttk.Button(row, text="Add",
                                       command=self._safe(self._on_add_rule))
        self.add_rule_btn.pack(side="left")
        self.remove_rule_btn = ttk.Button(
            box, text="Remove selected",
            command=self._safe(self._on_remove_rule))
        self.remove_rule_btn.pack(anchor="w", pady=(4, 0))
        self.dict_status = ttk.Label(box, text="", foreground=_DIM,
                                     wraplength=_WRAP)
        self.dict_status.pack(anchor="w", pady=(2, 0))

        # Suggestions sub-block, packed LAST in the box so hide/re-show via
        # pack_forget/pack can never land it out of order.
        self.sugg_note = ttk.Label(box, text="Suggested from your corrections:",
                                   foreground=_DIM)
        self.sugg_note.pack(anchor="w", pady=(6, 0))
        self.sugg_row = ttk.Frame(box)
        self.sugg_list = tk.Listbox(self.sugg_row, height=3,
                                    exportselection=False)
        ssb = ttk.Scrollbar(self.sugg_row, orient="vertical",
                            command=self.sugg_list.yview)
        self.sugg_list.configure(yscrollcommand=ssb.set)
        self.sugg_list.pack(side="left", fill="x", expand=True)
        ssb.pack(side="right", fill="y")
        self.accept_sugg_btn = ttk.Button(
            box, text="Accept selected",
            command=self._safe(self._on_accept_suggestion))
        self._sugg_items: list[tuple[str, str, int]] = []

        self._refresh_rules()
        self._refresh_suggestions()

    def _build_advanced(self, parent) -> None:
        box = ttk.LabelFrame(parent, text="Advanced", padding=8)
        box.pack(fill="x", pady=(0, 6))
        row = ttk.Frame(box)
        row.pack(fill="x")
        ttk.Label(row, text="Compute device", width=18).pack(side="left")
        shown_device = str(self.ctx.cfg.device).upper()
        if shown_device not in ("GPU", "NPU", "CPU"):
            shown_device = "GPU"
        self.device_var = tk.StringVar(value=shown_device)
        ttk.Combobox(row, state="readonly", width=6,
                     textvariable=self.device_var,
                     values=["GPU", "NPU", "CPU"]).pack(side="left")
        ttk.Button(box, text="Open settings file",
                   command=self._safe(self._on_open_config)
                   ).pack(anchor="w", pady=(4, 0))
        ttk.Label(box, text="Restart needed for: compute device, model "
                            "folders, cache folder, hot words",
                  foreground=_DIM, wraplength=_WRAP).pack(anchor="w",
                                                          pady=(4, 0))

    def _build_bottom(self, parent) -> None:
        bar = ttk.Frame(parent)
        bar.pack(fill="x", pady=(4, 0))
        self.apply_btn = ttk.Button(bar, text="Apply",
                                    command=self._safe(self._on_apply))
        self.apply_btn.pack(side="left")
        ttk.Button(bar, text="Close",
                   command=self._safe(self._on_close)).pack(side="left",
                                                            padx=(6, 0))
        self.bottom_status = ttk.Label(parent, text="", wraplength=_WRAP)
        self.bottom_status.pack(anchor="w", pady=(4, 0))

    # -- plumbing -----------------------------------------------------------

    def _safe(self, fn):
        """Wrap a widget callback so an exception can never escape into Tk's
        callback chain (it would kill the overlay's whole mainloop)."""
        def run(*_args):
            try:
                fn()
            except tk.TclError:
                pass    # window died under the handler (pill rebuild) - fine
            except Exception:
                log.exception("settings handler failed")
        return run

    def _alive(self) -> bool:
        try:
            return bool(self.top.winfo_exists())
        except tk.TclError:
            return False

    def _post(self, fn, *args) -> None:
        """Marshal a worker-thread completion onto the Tk thread. The window
        (or the whole root) may already be gone - a pill_size apply rebuilds
        it - so every failure mode here is expected and swallowed."""
        def run():
            try:
                fn(*args)
            except tk.TclError:
                pass
            except Exception:
                log.exception("settings completion failed")
        try:
            self.top.after(0, run)
        except (tk.TclError, RuntimeError):
            pass

    def _set_status(self, text: str, error: bool = False) -> None:
        self.bottom_status.config(text=text, foreground=_RED if error else "")

    # -- live polls (Tk thread, self-rearming) ------------------------------

    def _poll_status(self) -> None:
        if not self._alive():
            return
        try:
            try:
                name = str(self.ctx.mic_name())
            except Exception:
                name = "(unknown)"
            try:
                fallback = bool(self.ctx.fallback_active())
            except Exception:
                fallback = False
            self.mic_line.config(text="Mic: " + name)
            self.fb_line.config(text="Fallback mic active" if fallback else "")
            self.top.after(500, self._poll_status)
        except tk.TclError:
            pass

    def _poll_level(self) -> None:
        if not self._alive():
            return
        try:
            try:
                rms = float(self.ctx.latest_level())
            except Exception:
                rms = 0.0
            # Same compression as the pill's bars: full scale well below 1.0
            # RMS so normal speech visibly moves the bar.
            frac = min(1.0, (max(0.0, rms) * 18.0) ** 0.5)
            self.level_canvas.coords(
                self._level_bar, 1, 1,
                1 + int((_METER_W - 2) * frac), _METER_H - 1)
            self.top.after(50, self._poll_level)
        except tk.TclError:
            pass

    # -- microphone test -----------------------------------------------------

    def _on_test_mic(self) -> None:
        def on_done(text: str) -> None:
            # fires on the mic-test worker thread
            self._post(self._show_test_result, text)
        try:
            started = self.ctx.test_mic(on_done)
        except Exception:
            log.exception("mic test failed to start")
            self.test_label.config(text="(mic test failed to start)")
            return
        if not started:
            self.test_label.config(
                text="Dictation in progress - try again in a moment.")
            return
        self.test_btn.state(["disabled"])
        self.test_label.config(
            text="Listening for about 3 seconds - say something...")

    def _show_test_result(self, text: str) -> None:
        self.test_btn.state(["!disabled"])
        self.test_label.config(text=text)

    # -- presets / audio -----------------------------------------------------

    def _on_preset_selected(self) -> None:
        preset = PRESETS.get(self.preset_var.get())
        if preset is None:
            return      # Custom picked by hand - leave the controls alone
        # The scale's command fires when its variable is set; the flag stops
        # that programmatic move from flipping the combobox straight back.
        self._applying_preset = True
        try:
            strength, sensitivity, normalize = preset
            self.denoise_var.set(strength)
            self.sens_var.set(sensitivity)
            self.normalize_var.set(normalize)
            self.denoise_val.config(text=f"{strength:.2f}")
            self.sens_val.config(text=f"{sensitivity:.2f}")
        finally:
            self._applying_preset = False

    def _on_denoise_moved(self) -> None:
        self.denoise_val.config(text=f"{self.denoise_var.get():.2f}")
        self._on_audio_touched()

    def _on_sens_moved(self) -> None:
        self.sens_val.config(text=f"{self.sens_var.get():.2f}")
        self._on_audio_touched()

    def _on_audio_touched(self) -> None:
        if self._applying_preset:
            return
        self.preset_var.set(preset_name_for(
            round(float(self.denoise_var.get()), 2),
            round(float(self.sens_var.get()), 2),
            bool(self.normalize_var.get())))

    def _on_pill_moved(self) -> None:
        self.pill_val.config(text=f"{self.pill_var.get():.2f}")

    # -- room-noise calibration (M9) ---------------------------------------

    def _calibration_text(self) -> str:
        if self.ctx.calibration_status is None:
            return "(calibration not wired)"
        try:
            return str(self.ctx.calibration_status())
        except Exception:
            log.exception("calibration status failed")
            return ""

    def _on_calibrate(self) -> None:
        if self.ctx.calibrate is None:
            return
        self.calibrate_btn.state(["disabled"])
        self.cal_status.config(text="Recording 2s - stay QUIET...",
                               foreground=_WARN)

        def done(message: str) -> None:
            # arrives on the calibration worker thread - marshal to Tk;
            # the window may be gone (pill rebuild), which is fine
            def show() -> None:
                self.cal_status.config(
                    text=f"{message}  |  {self._calibration_text()}",
                    foreground=_DIM)
                self.calibrate_btn.state(["!disabled"])
            try:
                self.top.after(0, self._safe(show))
            except tk.TclError:
                pass

        started = self.ctx.calibrate(done)
        if not started:
            self.cal_status.config(
                text="A dictation is running - try again in a moment",
                foreground=_WARN)
            self.calibrate_btn.state(["!disabled"])

    def _on_clear_calibration(self) -> None:
        if self.ctx.clear_calibration is None:
            return
        try:
            note = str(self.ctx.clear_calibration())
        except Exception:
            log.exception("clear calibration failed")
            note = "(clear failed - see the log)"
        self.cal_status.config(text=f"{note}  |  {self._calibration_text()}",
                               foreground=_DIM)

    # -- history panel (M10) -------------------------------------------------

    def _update_history_note(self) -> None:
        self.history_off_note.config(
            text="" if self.history_var.get()
            else "History is off - nothing is stored")

    def _on_history_toggled(self) -> None:
        enabled = bool(self.history_var.get())
        # ctx.apply can take seconds (it also persists the toml) - keep it
        # off the Tk thread, same shape as _on_calibrate.
        self.history_check.state(["disabled"])
        self.hist_status.config(text="Saving...", foreground=_DIM)

        def work() -> None:
            try:
                self.ctx.apply({"history_enabled": enabled})
            except Exception as e:
                log.exception("history toggle failed")
                self._post(self._history_toggle_done,
                           " ".join(str(e).split())[:200], True)
                return
            self._post(self._history_toggle_done,
                       "History on." if enabled else "History off.", False)

        threading.Thread(target=work, daemon=True,
                         name="localflow-history-toggle").start()

    def _history_toggle_done(self, message: str, error: bool) -> None:
        self.history_check.state(["!disabled"])
        self.hist_status.config(text=message,
                                foreground=_RED if error else _DIM)
        if error:
            # The checkbox was disabled for the whole apply, so its var
            # still holds the attempted value - flip it back to reality.
            self.history_var.set(not self.history_var.get())
        self._update_history_note()

    def _refresh_history(self) -> None:
        try:
            entries = list(self.ctx.history_entries())[:_HIST_MAX_ROWS]
        except Exception:
            log.exception("history load failed")
            entries = []
        self._hist_entries = entries
        self._selected_ts = None
        self.correction_var.set("")
        self.history_list.delete(0, "end")
        for entry in entries:
            self.history_list.insert("end", history_row_label(entry))

    def _on_history_selected(self) -> None:
        sel = self.history_list.curselection()
        if not sel:
            return
        entry = self._hist_entries[sel[0]]
        self._selected_ts = float(entry.get("ts") or 0.0)
        self.correction_var.set(entry.get("corrected_text")
                                or entry.get("final_text") or "")

    def _on_save_correction(self) -> None:
        # Deliberately ON the Tk thread: set_correction is a small local
        # file rewrite, nothing like the seconds-long ctx.apply.
        if self._selected_ts is None:
            self.hist_status.config(text="Select an entry first.",
                                    foreground=_DIM)
            return
        text = self.correction_var.get().strip()
        if not text:
            return
        try:
            ok = bool(self.ctx.save_correction(self._selected_ts, text))
        except Exception:
            log.exception("save correction failed")
            ok = False
        if not ok:
            self.hist_status.config(
                text="Could not save (history off or the entry is gone).",
                foreground=_RED)
            return
        self.hist_status.config(text="Correction saved.", foreground=_DIM)
        self._refresh_history()
        # A new correction can push a substitution over the miner's
        # threshold - the suggestions list must reflect that immediately.
        if hasattr(self, "sugg_list"):
            self._refresh_suggestions()

    def _on_clear_history(self) -> None:
        try:
            self.ctx.clear_history()
        except Exception:
            log.exception("clear history failed")
            self.hist_status.config(text="(clear failed - see the log)",
                                    foreground=_RED)
            return
        self.hist_status.config(text="History cleared.", foreground=_DIM)
        self._refresh_history()

    # -- dictionary (M10) ------------------------------------------------------

    def _refresh_rules(self) -> None:
        self._rule_sources = sorted(self._rules)
        self.rules_list.delete(0, "end")
        for line in rule_rows(self._rules):
            self.rules_list.insert("end", line)

    def _refresh_suggestions(self) -> None:
        try:
            items = list(self.ctx.suggestions())
        except Exception:
            log.exception("suggestions failed")
            items = []
        # ctx.suggestions filters against the PERSISTED dictionary, which
        # lags while a rules apply is still on its worker thread - filter
        # against the working copy too so an accepted rule vanishes at once.
        covered = {source.lower() for source in self._rules}
        items = [(s, t, n) for s, t, n in items if s.lower() not in covered]
        self._sugg_items = items
        self.sugg_list.delete(0, "end")
        for line in suggestion_rows(items):
            self.sugg_list.insert("end", line)
        if items:
            self.sugg_note.config(text="Suggested from your corrections:")
            self.sugg_row.pack(fill="x", pady=(2, 0))
            self.accept_sugg_btn.pack(anchor="w", pady=(4, 0))
        else:
            self.sugg_note.config(
                text="Suggested from your corrections: none yet")
            self.sugg_row.pack_forget()
            self.accept_sugg_btn.pack_forget()

    def _on_add_rule(self) -> None:
        source = self.heard_var.get().strip()
        target = self.replace_var.get().strip()
        if not source or not target:
            return
        self._rules[source] = target        # overwrite is the update path
        self.heard_var.set("")
        self.replace_var.set("")
        self._refresh_rules()
        self._refresh_suggestions()         # a manual add may cover one
        self._persist_rules()

    def _on_remove_rule(self) -> None:
        sel = self.rules_list.curselection()
        if not sel:
            return
        self._rules.pop(self._rule_sources[sel[0]], None)
        self._refresh_rules()
        self._refresh_suggestions()
        self._persist_rules()

    def _on_accept_suggestion(self) -> None:
        sel = self.sugg_list.curselection()
        if not sel:
            return
        source, target, _count = self._sugg_items[sel[0]]
        self._rules[source] = target
        self._refresh_rules()
        self._refresh_suggestions()
        self._persist_rules()

    def _persist_rules(self) -> None:
        rules = dict(self._rules)           # snapshot before the thread runs
        for btn in (self.add_rule_btn, self.remove_rule_btn,
                    self.accept_sugg_btn):
            btn.state(["disabled"])
        self.dict_status.config(text="Saving...", foreground=_DIM)

        def work() -> None:
            try:
                self.ctx.apply({"replacements": rules})
            except Exception as e:
                log.exception("dictionary save failed")
                self._post(self._rules_saved,
                           " ".join(str(e).split())[:200], True)
                return
            self._post(self._rules_saved, "Saved.", False)

        threading.Thread(target=work, daemon=True,
                         name="localflow-dict-save").start()

    def _rules_saved(self, message: str, error: bool) -> None:
        self.dict_status.config(text=message,
                                foreground=_RED if error else _DIM)
        for btn in (self.add_rule_btn, self.remove_rule_btn,
                    self.accept_sugg_btn):
            btn.state(["!disabled"])

    # -- hotkey capture --------------------------------------------------------

    def _start_capture(self) -> None:
        if self._capturing:
            return
        self._capturing = True
        self.hotkey_btn.config(text="press keys... (Esc cancels)")
        self.hotkey_note.config(text="", foreground=_DIM)

        mods: set[str] = set()
        done = {"fired": False}

        def finish(combo: str | None) -> None:
            # runs on the pynput listener thread - marshal to Tk
            if done["fired"]:
                return
            done["fired"] = True
            self._post(self._end_capture, combo)

        def on_press(key):
            try:
                if key == keyboard.Key.esc:
                    finish(None)
                    return False
                mod = modifier_of(key)
                if mod is not None:
                    mods.add(mod)
                    return None
                name = key_name_of(key)
                if name is None or name in _CAPTURE_IGNORED:
                    return None     # unusable key - keep listening
                finish(format_captured(list(mods), name))
                return False        # one combo per capture: stop now
            except Exception:
                log.exception("hotkey capture failed")
                finish(None)
                return False

        def on_release(key):
            try:
                mod = modifier_of(key)
                if mod is not None:
                    mods.discard(mod)
            except Exception:
                pass

        listener = keyboard.Listener(on_press=on_press, on_release=on_release)
        self._capture_listener = listener
        listener.start()

    def _end_capture(self, combo: str | None) -> None:
        self._capturing = False
        listener, self._capture_listener = self._capture_listener, None
        if listener is not None:
            try:
                listener.stop()
            except Exception:
                pass
        self.hotkey_btn.config(text="Change...")
        if combo is None:
            self.hotkey_note.config(text="Cancelled.", foreground=_DIM)
            return
        try:
            self.ctx.validate_hotkey(combo)
        except Exception:
            self.hotkey_note.config(text="Bad combo: " + combo,
                                    foreground=_RED)
            return
        self.hotkey_var.set(combo)
        self.hotkey_note.config(text="New hotkey: " + combo +
                                     " (Apply to save)", foreground=_DIM)

    # -- buttons ------------------------------------------------------------

    def _on_reset_position(self) -> None:
        self.ctx.reset_pill_position()
        self._set_status("Pill position reset.")

    def _on_open_config(self) -> None:
        self.ctx.open_config_file()

    def _read_values(self) -> dict:
        """Current widget values, keyed and typed like Config fields.
        Raises ValueError with a user-readable message on unparsable input."""
        values: dict = {}
        idx = self.mic_combo.current()
        if idx < 0:
            idx = self._mic_index
        values["input_device"] = self._mic_rows[idx][1]
        values["denoise_strength"] = round(float(self.denoise_var.get()), 2)
        values["vad_threshold"] = round(float(self.sens_var.get()), 2)
        values["normalize_audio"] = bool(self.normalize_var.get())
        values["hotkey"] = self.hotkey_var.get()
        values["overlay_visibility"] = _VIS_BY_LABEL.get(
            self.visibility_var.get(), "always")
        values["pill_size"] = round(float(self.pill_var.get()), 2)
        values["inject_mode"] = self.inject_var.get()
        try:
            values["restore_delay_ms"] = int(self.delay_var.get().strip())
        except ValueError:
            raise ValueError(
                "Clipboard restore delay must be a whole number of ms.")
        values["device"] = self.device_var.get()
        if self._has_cleanup:
            values["cleanup_enabled"] = bool(self.cleanup_var.get())
        return values

    def _on_apply(self) -> None:
        try:
            current = self._read_values()
        except ValueError as e:
            self._set_status(str(e), error=True)
            return
        changes = coerce_changes(collect_changes(self._initial, current))
        if not changes:
            self._set_status("Nothing to apply.")
            return
        self.apply_btn.state(["disabled"])
        self._set_status("Applying...")

        def work() -> None:
            # ctx.apply may take seconds and, for pill_size, destroys the
            # very root this window lives on - it must never run on the Tk
            # thread, and the completions below may land on a dead window.
            try:
                restart = self.ctx.apply(changes)
            except Exception as e:
                log.exception("settings apply failed")
                self._post(self._apply_failed, str(e))
                return
            self._post(self._apply_done, changes, restart)

        threading.Thread(target=work, daemon=True,
                         name="localflow-settings-apply").start()

    def _apply_done(self, changes: dict, restart: set) -> None:
        self._initial.update(changes)   # a re-Apply must not resend these
        text = "Saved."
        if restart:
            text += " Takes effect after restart: " + ", ".join(sorted(restart))
        self._set_status(text)
        self.apply_btn.state(["!disabled"])

    def _apply_failed(self, message: str) -> None:
        self._set_status(" ".join(message.split())[:300], error=True)
        self.apply_btn.state(["!disabled"])

    # -- teardown -------------------------------------------------------------

    def _stop_capture_listener(self) -> None:
        listener, self._capture_listener = self._capture_listener, None
        if listener is not None:
            try:
                listener.stop()
            except Exception:
                pass

    def _on_close(self) -> None:
        self._stop_capture_listener()
        try:
            self.top.destroy()
        except tk.TclError:
            pass

    def _on_destroyed(self, event) -> None:
        # <Destroy> fires for every child widget too - only act on the
        # Toplevel itself. Covers both the Close path and a root teardown
        # (pill rebuild) so a capture listener can never outlive the window.
        if event.widget is self.top:
            self._stop_capture_listener()
