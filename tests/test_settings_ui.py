"""Settings-window helper tests (M8). Headless-safe by design: only the
pure helpers are exercised - no Tk root is ever created here. pynput key
OBJECTS are constructed (that needs no display or hook), but no Listener
ever starts. The window itself is verified live per the M8 checklist.
"""

from __future__ import annotations

import time

import pytest
from pynput import keyboard

from localflow.desktop import settings_ui as ui


# -- environment presets -------------------------------------------------

def test_presets_set_the_audio_controls():
    # (denoise_strength, speech sensitivity, normalize) - M9 added the
    # sensitivity value from the v1.1 plan's starting guesses.
    assert ui.PRESETS["Quiet room"] == (0.3, 0.5, True)
    assert ui.PRESETS["Noisy room"] == (0.75, 0.6, True)
    assert ui.PRESETS["Headset"] == (0.2, 0.45, True)


def test_preset_name_for_exact_matches():
    assert ui.preset_name_for(0.3, 0.5, True) == "Quiet room"
    assert ui.preset_name_for(0.75, 0.6, True) == "Noisy room"
    assert ui.preset_name_for(0.2, 0.45, True) == "Headset"


def test_preset_name_for_tolerance():
    # slider floats land near, not on, the preset value
    assert ui.preset_name_for(0.301, 0.5, True) == "Quiet room"
    assert ui.preset_name_for(0.291, 0.499, True) == "Quiet room"
    assert ui.preset_name_for(0.32, 0.5, True) == "Custom"


def test_preset_name_for_every_value_must_match():
    assert ui.preset_name_for(0.3, 0.5, False) == "Custom"   # normalize off
    assert ui.preset_name_for(0.3, 0.6, True) == "Custom"    # wrong sens


def test_preset_name_for_unmatched_is_custom():
    assert ui.preset_name_for(0.6, 0.5, True) == "Custom"


# -- hotkey capture conversion --------------------------------------------

def test_key_name_of_plain_char():
    assert ui.key_name_of(keyboard.KeyCode.from_char("d")) == "d"
    assert ui.key_name_of(keyboard.KeyCode.from_char("D")) == "d"
    assert ui.key_name_of(keyboard.KeyCode.from_char("`")) == "`"


def test_key_name_of_named_keys():
    assert ui.key_name_of(keyboard.Key.f9) == "f9"
    assert ui.key_name_of(keyboard.Key.space) == "space"


def test_key_name_of_control_char_maps_back_to_letter():
    # with ctrl held, Windows delivers '\x04' instead of 'd'
    assert ui.key_name_of(keyboard.KeyCode.from_char("\x04")) == "d"


def test_key_name_of_vk_fallback():
    assert ui.key_name_of(keyboard.KeyCode.from_vk(0x44)) == "d"
    assert ui.key_name_of(keyboard.KeyCode.from_vk(0x37)) == "7"


def test_key_name_of_unusable_key_is_none():
    assert ui.key_name_of(keyboard.KeyCode.from_vk(0xAD)) is None  # media key


def test_modifier_of():
    assert ui.modifier_of(keyboard.Key.ctrl_l) == "ctrl"
    assert ui.modifier_of(keyboard.Key.ctrl) == "ctrl"
    assert ui.modifier_of(keyboard.Key.alt_gr) == "alt"
    assert ui.modifier_of(keyboard.Key.shift_r) == "shift"
    assert ui.modifier_of(keyboard.Key.f9) is None
    assert ui.modifier_of(keyboard.KeyCode.from_char("a")) is None


def test_format_captured_orders_modifiers_canonically():
    assert ui.format_captured(["shift", "ctrl"], "d") == "ctrl+shift+d"
    assert ui.format_captured(["alt"], "`") == "alt+`"
    assert ui.format_captured([], "f9") == "f9"


def test_format_captured_round_trips_through_app_parser():
    """The captured combo must be exactly what to_pynput_combo expects."""
    from localflow.desktop.app import to_pynput_combo
    combo = ui.format_captured(["ctrl", "shift"], "d")
    assert keyboard.HotKey.parse(to_pynput_combo(combo))


# -- mic combobox preselection ---------------------------------------------

_MICS = [
    ("System default (follow Windows)", None),
    ("Mic Array (Realtek)", 3),
    ("Samsung USB Headset", 7),
]


def test_mic_selection_default_follows_windows():
    rows, idx = ui.mic_selection(_MICS, None)
    assert idx == 0 and rows == _MICS


def test_mic_selection_matches_int_index():
    rows, idx = ui.mic_selection(_MICS, 7)
    assert idx == 2 and rows == _MICS


def test_mic_selection_digit_string_counts_as_index():
    _rows, idx = ui.mic_selection(_MICS, "3")
    assert idx == 1


def test_mic_selection_name_substring_skips_default_row():
    # 'default' must never match the row-0 "System default" label
    rows, idx = ui.mic_selection(_MICS, "default")
    assert idx == len(rows) - 1
    assert rows[idx] == ("(pinned: default)", "default")


def test_mic_selection_name_substring_case_insensitive():
    _rows, idx = ui.mic_selection(_MICS, "samsung")
    assert idx == 2


def test_mic_selection_unknown_pin_gets_explicit_row():
    rows, idx = ui.mic_selection(_MICS, 42)
    assert rows[idx] == ("(pinned: 42)", 42)
    assert idx == len(rows) - 1


# -- dirty collection and type coercion --------------------------------------

def test_collect_changes_empty_when_untouched():
    initial = {"hotkey": "alt+`", "pill_size": 1.0, "denoise_enabled": True}
    assert ui.collect_changes(initial, {"hotkey": "alt+`", "pill_size": 1.0}) == {}


def test_collect_changes_only_dirty_keys():
    initial = {"hotkey": "alt+`", "pill_size": 1.0}
    current = {"hotkey": "f9", "pill_size": 1.0}
    assert ui.collect_changes(initial, current) == {"hotkey": "f9"}


def test_collect_changes_derives_denoise_off_at_zero_strength():
    initial = {"denoise_strength": 0.6, "denoise_enabled": True}
    changes = ui.collect_changes(initial, {"denoise_strength": 0.0})
    assert changes == {"denoise_strength": 0.0, "denoise_enabled": False}


def test_collect_changes_derives_denoise_on_from_slider():
    initial = {"denoise_strength": 0.0, "denoise_enabled": False}
    changes = ui.collect_changes(initial, {"denoise_strength": 0.4})
    assert changes == {"denoise_strength": 0.4, "denoise_enabled": True}


def test_collect_changes_no_denoise_flag_when_state_unchanged():
    initial = {"denoise_strength": 0.6, "denoise_enabled": True}
    changes = ui.collect_changes(initial, {"denoise_strength": 0.4})
    assert changes == {"denoise_strength": 0.4}


def test_collect_changes_untouched_slider_never_flips_denoise():
    # cfg can say denoise_enabled=false with a nonzero strength on disk;
    # an Apply that never moved the slider must not rewrite either key
    initial = {"denoise_strength": 0.6, "denoise_enabled": False}
    assert ui.collect_changes(initial, {"denoise_strength": 0.6}) == {}


def test_coerce_changes_pins_types():
    out = ui.coerce_changes({
        "restore_delay_ms": "650",
        "denoise_strength": "0.4",
        "pill_size": 1,
        "cleanup_enabled": 1,
        "normalize_audio": 0,
        "hotkey": "f9",
    })
    assert out["restore_delay_ms"] == 650 and isinstance(out["restore_delay_ms"], int)
    assert out["denoise_strength"] == pytest.approx(0.4)
    assert isinstance(out["denoise_strength"], float)
    assert isinstance(out["pill_size"], float)
    assert out["cleanup_enabled"] is True
    assert out["normalize_audio"] is False
    assert out["hotkey"] == "f9"


def test_coerce_changes_passes_input_device_through():
    assert ui.coerce_changes({"input_device": None}) == {"input_device": None}
    assert ui.coerce_changes({"input_device": 7}) == {"input_device": 7}
    assert ui.coerce_changes({"input_device": "Samsung"}) == {"input_device": "Samsung"}


# -- history row labels (M10) ------------------------------------------------

def _entry(ts=0.0, final="hello world", corrected=None):
    """A history entry shaped like app.history_entries() rows."""
    return {"ts": ts, "raw_text": final, "clean_text": final,
            "final_text": final, "corrected_text": corrected,
            "device": "GPU", "stt_ms": 500}


def _at(hour, minute):
    """A ts whose local time is hour:minute today - the label formats
    with time.localtime, so the test must build the ts the same way."""
    return time.mktime((2026, 7, 6, hour, minute, 0, 0, 0, -1))


def test_history_row_label_time_prefix_and_text():
    assert ui.history_row_label(_entry(ts=_at(14, 32))) == "14:32  hello world"


def test_history_row_label_marks_corrected_rows():
    # shows the ORIGINAL final text; the '*' is what says 'edited'
    label = ui.history_row_label(_entry(ts=_at(9, 5), corrected="Hello."))
    assert label == "09:05  * hello world"


def test_history_row_label_truncation_boundary():
    at_limit = "x" * 60
    assert ui.history_row_label(_entry(final=at_limit)).endswith(at_limit)
    over = ui.history_row_label(_entry(final="x" * 61))
    assert over.endswith("x" * 57 + "...")
    assert len(over.split("  ", 1)[1]) == 60   # cap holds after truncation


def test_history_row_label_flattens_newlines():
    assert ui.history_row_label(_entry(final="one\ntwo  three")).endswith(
        "one two three")


def test_history_row_label_empty_text_is_just_the_time():
    when = time.strftime("%H:%M", time.localtime(0.0))
    assert ui.history_row_label(_entry(ts=0.0, final="")) == f"{when}  "


# -- dictionary rows (M10) -----------------------------------------------------

def test_rule_rows_sorted_by_source():
    rows = ui.rule_rows({"zeta": "z", "alpha": "A", "mid": "m"})
    assert rows == ["alpha  ->  A", "mid  ->  m", "zeta  ->  z"]


def test_rule_rows_empty():
    assert ui.rule_rows({}) == []


def test_suggestion_rows_format():
    rows = ui.suggestion_rows([("cloud ex", "Codex", 3), ("jim", "Gem", 2)])
    assert rows == ["cloud ex -> Codex  (seen 3x)", "jim -> Gem  (seen 2x)"]


def test_suggestion_rows_empty():
    assert ui.suggestion_rows([]) == []


# -- context construction (must match app.wire_settings_ui exactly) ----------

def test_settings_context_accepts_the_app_contract():
    ctx = ui.SettingsContext(
        cfg=object(), apply=lambda c: set(), list_mics=lambda: [],
        mic_name=lambda: "m", fallback_active=lambda: False,
        latest_level=lambda: 0.0, test_mic=lambda cb: True,
        validate_hotkey=lambda c: None, reset_pill_position=lambda: None,
        open_config_file=lambda: None, status_text="whisper on GPU",
        cleanup_available=True, cleanup_active=lambda: False)
    assert ctx.status_text == "whisper on GPU"
    assert ctx.cleanup_available is True
    assert ctx.cleanup_active() is False


def test_settings_context_m10_fields_default_to_none():
    # pre-M10 wiring omits them; the window skips both sections on None
    ctx = ui.SettingsContext(
        cfg=object(), apply=lambda c: set(), list_mics=lambda: [],
        mic_name=lambda: "m", fallback_active=lambda: False,
        latest_level=lambda: 0.0, test_mic=lambda cb: True,
        validate_hotkey=lambda c: None, reset_pill_position=lambda: None,
        open_config_file=lambda: None, status_text="s",
        cleanup_available=False, cleanup_active=lambda: False)
    assert ctx.history_entries is None
    assert ctx.save_correction is None
    assert ctx.clear_history is None
    assert ctx.suggestions is None


# -- open_settings singleton, stale-window recovery ----------------------

class _FakeTop:
    """Stands in for a Toplevel whose Tk root was torn down. winfo_exists
    raises the exception open_settings must survive (no real Tk needed)."""

    def __init__(self, exc):
        self._exc = exc

    def winfo_exists(self):
        raise self._exc


class _FakeWin:
    def __init__(self, exc):
        self.top = _FakeTop(exc)


@pytest.fixture(autouse=True)
def _reset_holder():
    ui._holder["win"] = None
    yield
    ui._holder["win"] = None


@pytest.mark.parametrize("exc", [
    RuntimeError("main thread is not in main loop"),  # the pill-rebuild crash
    ui.tk.TclError("application has been destroyed"),
])
def test_open_settings_rebuilds_after_root_torn_down(monkeypatch, exc):
    # A pill_size Apply destroys the overlay root under the old window; the
    # next open must drop the stale singleton and build a fresh window
    # instead of letting the winfo_exists error escape (regression: only
    # TclError was caught, so RuntimeError crashed every reopen).
    ui._holder["win"] = _FakeWin(exc)
    built = []
    monkeypatch.setattr(ui, "_SettingsWindow",
                        lambda root, ctx: built.append((root, ctx)) or "fresh")

    ui.open_settings("new_root", "ctx")

    assert built == [("new_root", "ctx")]
    assert ui._holder["win"] == "fresh"


def test_open_settings_refocuses_a_live_window(monkeypatch):
    # A still-alive window is deiconified/lifted, NOT rebuilt.
    calls = []

    class LiveTop:
        def winfo_exists(self):
            return True

        def deiconify(self):
            calls.append("deiconify")

        def lift(self):
            calls.append("lift")

        def focus_force(self):
            calls.append("focus")

    live = type("W", (), {"top": LiveTop()})()
    ui._holder["win"] = live
    monkeypatch.setattr(ui, "_SettingsWindow",
                        lambda root, ctx: pytest.fail("should not rebuild"))

    ui.open_settings("root", "ctx")

    assert calls == ["deiconify", "lift", "focus"]
    assert ui._holder["win"] is live
