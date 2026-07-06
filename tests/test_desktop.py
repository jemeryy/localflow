"""Desktop-layer tests (M3). All headless-safe: no audio stream is opened,
no keys are sent, the clipboard is never touched (pyperclip is monkeypatched).
The live end-to-end injection check is scripts/m3_inject_check.py.
"""

from __future__ import annotations

import numpy as np
import pytest


def _fake_query_devices(device, kind):
    return {"name": "fake-mic", "default_samplerate": 16_000.0}


class _FakeStream:
    """Stands in for sd.InputStream so no real device is opened."""

    def __init__(self, **kwargs):
        self.active = False

    def start(self):
        self.active = True

    def stop(self):
        self.active = False

    def close(self):
        self.active = False


@pytest.fixture
def recorder(monkeypatch):
    from localflow.desktop import capture

    monkeypatch.setattr(capture.sd, "query_devices", _fake_query_devices)
    monkeypatch.setattr(capture.sd, "InputStream", _FakeStream)
    return capture.StreamRecorder(max_seconds=1.0, preroll_s=0.2)


def _chunk(value: float, frames: int = 1600) -> np.ndarray:
    return np.full((frames, 1), value, dtype=np.float32)  # 0.1s @16k


def test_stream_recorder_accumulates_chunks(recorder):
    recorder.start()
    assert recorder.recording
    for _ in range(5):
        recorder._on_chunk(_chunk(0.25), 1600, None, None)
    audio, rate = recorder.stop()
    assert not recorder.recording
    assert rate == 16_000
    assert len(audio) == 5 * 1600
    assert audio.dtype == np.float32
    assert float(audio.max()) == pytest.approx(0.25)


def test_stream_recorder_seeds_preroll_on_start(recorder):
    recorder.open()
    for i in range(5):  # idle audio before key-down; ring keeps ~0.2s
        recorder._on_chunk(_chunk(float(i)), 1600, None, None)
    recorder.start()
    recorder._on_chunk(_chunk(9.0), 1600, None, None)
    audio, _ = recorder.stop()
    # ring held the last two idle chunks (3, 4); older ones were discarded
    assert len(audio) == 3 * 1600
    assert float(audio[0]) == 3.0 and float(audio[-1]) == 9.0


def test_stream_recorder_caps_at_max_seconds(recorder):
    recorder.start()
    for _ in range(15):  # 1.5s offered against a 1.0s cap
        recorder._on_chunk(_chunk(0.0), 1600, None, None)
    audio, _ = recorder.stop()
    assert len(audio) == 16_000  # FR-3: exactly the cap, rest discarded


def test_stream_recorder_stop_without_start_is_safe(recorder):
    audio, rate = recorder.stop()
    assert len(audio) == 0 and rate == 16_000


def test_inject_empty_text_touches_nothing(monkeypatch):
    from localflow.desktop import inject

    monkeypatch.setattr(inject.pyperclip, "copy",
                        lambda s: pytest.fail("clipboard written for empty text"))
    monkeypatch.setattr(inject.pyperclip, "paste",
                        lambda: pytest.fail("clipboard read for empty text"))
    assert inject.inject_text("", mode="paste") is False  # FR-4 / AT-3
    assert inject.inject_text("", mode="type") is False


def test_paste_restores_saved_clipboard_after_settle(monkeypatch):
    from localflow.desktop import inject

    clip = {"value": "SAVED_BEFORE"}
    monkeypatch.setattr(inject.pyperclip, "paste", lambda: clip["value"])
    monkeypatch.setattr(inject.pyperclip, "copy",
                        lambda s: clip.update(value=s))
    monkeypatch.setattr(inject, "_wait_modifiers_released", lambda: None)
    monkeypatch.setattr(inject, "_send_ctrl_v", lambda: None)
    monkeypatch.setattr(inject.time, "sleep", lambda s: None)

    assert inject.inject_text("spoken words", mode="paste") is True
    assert clip["value"] == "SAVED_BEFORE"       # AT-2: clipboard restored


def test_paste_never_clobbers_a_copy_made_mid_settle(monkeypatch):
    """If the user copies something new while the transcript is settling,
    the restore must NOT overwrite it."""
    from localflow.desktop import inject

    clip = {"value": "SAVED_BEFORE"}
    monkeypatch.setattr(inject.pyperclip, "paste", lambda: clip["value"])
    monkeypatch.setattr(inject.pyperclip, "copy",
                        lambda s: clip.update(value=s))
    monkeypatch.setattr(inject, "_wait_modifiers_released", lambda: None)
    monkeypatch.setattr(inject, "_send_ctrl_v", lambda: None)
    # the settle sleep is where a user copy would land
    monkeypatch.setattr(
        inject.time, "sleep",
        lambda s: clip.update(value="USER_COPIED_THIS") if s >= 0.1 else None)

    inject.inject_text("spoken words", mode="paste", restore_delay_s=0.5)
    assert clip["value"] == "USER_COPIED_THIS"   # left alone, not "restored"


def test_inject_rejects_unknown_mode():
    from localflow.desktop.inject import inject_text

    with pytest.raises(ValueError, match="inject_mode"):
        inject_text("hello", mode="teleport")


def test_hotkey_combo_parses():
    from pynput import keyboard

    from localflow.desktop.app import to_pynput_combo

    assert to_pynput_combo("alt+`") == "<alt>+`"
    assert to_pynput_combo("ctrl+space") == "<ctrl>+<space>"
    assert to_pynput_combo("Ctrl+Shift+D") == "<ctrl>+<shift>+d"
    assert to_pynput_combo("f9") == "<f9>"
    assert len(keyboard.HotKey.parse(to_pynput_combo("alt+`"))) == 2
    with pytest.raises(ValueError):
        to_pynput_combo("  ")


def test_push_to_talk_fires_on_hold_and_release():
    """Simulate the OS event stream through the listener handlers (FR-1):
    left alt down, backquote down (with auto-repeats), backquote up. Never
    starts the real OS hook."""
    from pynput import keyboard

    from localflow.desktop.app import PushToTalkListener

    events: list[str] = []
    ptt = PushToTalkListener("alt+`",
                             lambda: events.append("start"),
                             lambda: events.append("stop"))
    backquote = keyboard.KeyCode.from_char("`")

    ptt._handle_press(keyboard.Key.alt_l)
    assert events == []                      # partial combo: not recording yet
    ptt._handle_press(backquote)
    assert events == ["start"]
    for _ in range(5):                       # OS auto-repeat while held
        ptt._handle_press(backquote)
    assert events == ["start"]               # still one recording
    ptt._handle_press(keyboard.KeyCode.from_char("x"))  # unrelated key
    assert events == ["start"]
    ptt._handle_release(backquote)
    assert events == ["start", "stop"]       # any combo key up ends the hold
    ptt._handle_release(keyboard.Key.alt_l)
    assert events == ["start", "stop"]       # no double-stop


# ---- M4 ----------------------------------------------------------------


def _bare_app(recorder=None, on_state=None):
    from localflow.desktop.app import LocalFlowApp

    # engine/vad are only touched inside _process, which these tests avoid
    return LocalFlowApp(engine=None, vad=None, recorder=recorder,
                        on_state=on_state)


def test_latency_summary_percentiles():
    app = _bare_app()
    assert app.latency_summary() is None     # nothing pasted yet
    app.latencies_ms = [400, 450, 500, 550, 600, 650, 700, 750, 800, 1250]
    summary = app.latency_summary()
    assert "10 dictations" in summary
    assert "p50 625ms" in summary            # np.percentile linear interp
    assert "p95" in summary


def test_state_callbacks_fire_on_hotkey_and_survive_errors(recorder):
    states: list[str] = []
    app = _bare_app(recorder=recorder, on_state=states.append)
    app.on_hotkey_down()
    app.on_hotkey_up()
    assert states == ["rec", "busy"]         # FR-2: red then amber

    # A broken state callback (e.g. tray died) must never reach the
    # pynput/worker threads.
    def boom(state):
        raise RuntimeError("tray gone")

    app.on_state = boom
    app._set_state("idle")                   # must not raise


def test_bar_heights_pads_scales_and_clamps():
    from localflow.desktop.overlay import bar_heights

    # empty -> all floor-height bars
    assert bar_heights([], 4, max_h=20.0) == [2.0, 2.0, 2.0, 2.0]
    # newest level lands rightmost, older/missing pad left
    bars = bar_heights([0.0, 0.05], 4, max_h=20.0)
    assert bars[0] == 2.0 and bars[1] == 2.0          # left padding at floor
    assert bars[3] > bars[2]                           # louder newest is taller
    # loud input clamps to max_h, never overshoots the capsule
    assert bar_heights([9.9], 1, max_h=20.0) == [20.0]
    # more levels than bars -> keeps only the newest n
    assert len(bar_heights([0.01] * 100, 44, max_h=20.0)) == 44


def test_recorder_reports_levels_only_while_recording(recorder):
    levels: list[float] = []
    recorder.on_level = levels.append

    recorder.open()
    recorder._on_chunk(_chunk(0.5), 1600, None, None)  # idle: preroll only
    assert levels == []
    recorder.start()
    recorder._on_chunk(_chunk(0.5), 1600, None, None)
    assert len(levels) == 1
    assert levels[0] == pytest.approx(0.5)             # RMS of a constant
    recorder.stop()

    # a broken UI callback must never disturb the audio path
    def boom(rms):
        raise RuntimeError("overlay gone")

    recorder.on_level = boom
    recorder.start()
    recorder._on_chunk(_chunk(0.5), 1600, None, None)  # must not raise
    audio, _ = recorder.stop()
    assert len(audio) > 0                              # audio still captured


def test_smooth_blends_neighbors_and_keeps_shape():
    from localflow.desktop.overlay import smooth

    out = smooth([0.0, 10.0, 0.0])
    assert len(out) == 3
    assert out[0] == 0.0 and out[2] == 0.0       # endpoints pass through
    assert 4.0 < out[1] < 10.0                    # peak softened, not erased
    assert smooth([5.0]) == [5.0]                 # short input untouched


def test_toggle_cleanup_states():
    app = _bare_app()
    assert app.cleanup is None and app.cleanup_active is False
    assert app.cleanup_available is False
    assert app.toggle_cleanup() is False    # no model, no loader -> stays off

    app.cleanup = object()                  # engine present
    app.cleanup_active = True               # (as after startup with config on)
    assert app.toggle_cleanup() is False    # click: off
    assert app.toggle_cleanup() is True     # click: back on


def _wait(predicate, timeout_s: float = 3.0) -> bool:
    import time

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_toggle_cleanup_lazy_loads_in_background():
    """First enable must NOT block (the tray click returns immediately) and
    must attach the engine once the loader finishes."""
    from localflow.desktop.app import LocalFlowApp

    fake_engine = object()
    app = LocalFlowApp(engine=None, vad=None, recorder=None,
                       cleanup_loader=lambda: fake_engine)
    assert app.cleanup_available is True
    assert app.cleanup is None and app.cleanup_active is False

    assert app.toggle_cleanup() is True          # intent ON, load kicked off
    assert _wait(lambda: app.cleanup is fake_engine)
    assert app.cleanup_active is True
    assert app.toggle_cleanup() is False         # OFF unloads the engine
    assert app.toggle_cleanup() is True          # ON reloads via the loader
    assert _wait(lambda: app.cleanup is fake_engine)


def test_toggle_cleanup_load_failure_stays_off():
    from localflow.desktop.app import LocalFlowApp

    def broken_loader():
        raise RuntimeError("model dir corrupted")

    app = LocalFlowApp(engine=None, vad=None, recorder=None,
                       cleanup_loader=broken_loader)
    assert app.toggle_cleanup() is True          # optimistic ON while loading
    assert _wait(lambda: not app._cleanup_loading)
    assert app.cleanup is None
    assert app.cleanup_active is False           # failure flips it back off


class _CloseableEngine:
    """Stands in for CleanupEngine; records close() (the memory release)."""

    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def test_toggle_cleanup_off_unloads_the_model():
    """Jeremy 3 Jul: 'the LLM cleanup shouldn't hold RAM — only when I use
    it'. Toggling OFF must release the engine, not just flip a flag; the
    loader re-arms the next ON."""
    from localflow.desktop.app import LocalFlowApp

    engines: list[_CloseableEngine] = []

    def loader():
        engines.append(_CloseableEngine())
        return engines[-1]

    app = LocalFlowApp(engine=None, vad=None, recorder=None,
                       cleanup_loader=loader)
    assert app.toggle_cleanup() is True
    assert _wait(lambda: app.cleanup is not None)

    assert app.toggle_cleanup() is False         # OFF -> unload
    assert app.cleanup is None
    assert engines[0].closed is True             # memory actually released

    assert app.toggle_cleanup() is True          # ON -> fresh load
    assert _wait(lambda: app.cleanup is not None)
    assert len(engines) == 2 and app.cleanup is engines[1]


def test_cleanup_toggled_off_mid_load_is_discarded():
    """Changing your mind while the model is still loading must not leave
    2-3GB attached in the background."""
    import threading

    from localflow.desktop.app import LocalFlowApp

    release = threading.Event()
    engine = _CloseableEngine()

    def slow_loader():
        release.wait(3.0)
        return engine

    app = LocalFlowApp(engine=None, vad=None, recorder=None,
                       cleanup_loader=slow_loader)
    assert app.toggle_cleanup() is True          # load kicked off
    assert app.toggle_cleanup() is False         # changed mind mid-load
    release.set()
    assert _wait(lambda: not app._cleanup_loading)
    assert app.cleanup is None                   # never attached...
    assert engine.closed is True                 # ...and freed on arrival


def test_toggle_recording_click_path(recorder):
    states: list[str] = []
    app = _bare_app(recorder=recorder, on_state=states.append)
    app.toggle_recording()                        # click 1: start
    assert recorder.recording
    app.toggle_recording()                        # click 2: stop + enqueue
    assert not recorder.recording
    assert app._work.qsize() == 1                 # utterance handed to worker
    assert states == ["rec", "busy"]


def test_mic_watcher_switch_flow(recorder, monkeypatch):
    from localflow.desktop import capture
    from localflow.desktop.micwatch import DefaultMicWatcher

    # PortAudio reinit is a no-op here; the fixture already fakes streams
    monkeypatch.setattr(capture.sd, "_terminate", lambda: None)
    monkeypatch.setattr(capture.sd, "_initialize", lambda: None)

    current = {"id": "mic-A"}
    watcher = DefaultMicWatcher(recorder, get_id=lambda: current["id"])

    assert watcher.check_once() is False   # first sighting = baseline
    assert watcher.check_once() is False   # unchanged -> no swap
    current["id"] = "mic-B"
    recorder.start()
    assert watcher.check_once() is False   # deferred while recording
    recorder.stop()
    assert watcher.check_once() is True    # swap once idle
    assert recorder._stream is not None    # stream reopened
    assert watcher.check_once() is False   # stable on the new device


def test_heal_if_stale_reopens_silent_stream(recorder, monkeypatch):
    """A stream that is open but delivering no frames (wedged WASAPI
    endpoint) must be detected and reopened — found live 3 Jul as repeated
    'recording stopped: 0.00s captured'."""
    from localflow.desktop import capture

    monkeypatch.setattr(capture.sd, "_terminate", lambda: None)
    monkeypatch.setattr(capture.sd, "_initialize", lambda: None)
    monkeypatch.setattr(capture, "_find_live_input",
                        lambda exclude_name, listen_s=1.5: (7, "working-mic"))

    assert recorder.heal_if_stale() is False       # no stream yet -> nothing to heal
    recorder.open()
    assert recorder.heal_if_stale() is False       # fresh stream isn't stale

    recorder._last_frame -= 10.0                   # simulate 10s of silence
    old_stream = recorder._stream
    assert recorder.stale() is True
    assert recorder.heal_if_stale() is True
    assert recorder._stream is not old_stream      # reopened

    recorder._last_frame -= 10.0                   # still silent (OS-level wedge)
    assert recorder.heal_if_stale() is False       # rate-limited: no reinit storm
    recorder._last_heal -= 60.0                    # backoff window elapsed
    assert recorder.heal_if_stale() is True        # heal 2 -> the fallback hunt

    recorder._on_chunk(_chunk(0.1), 1600, None, None)  # frames flowing again
    assert recorder.stale() is False

    recorder.start()                               # mid-recording is never stale
    recorder._last_frame -= 10.0
    assert recorder.stale() is False
    recorder.stop()


def test_mic_watcher_heals_silent_stream_on_unchanged_device(recorder, monkeypatch):
    from localflow.desktop import capture
    from localflow.desktop.micwatch import DefaultMicWatcher

    monkeypatch.setattr(capture.sd, "_terminate", lambda: None)
    monkeypatch.setattr(capture.sd, "_initialize", lambda: None)

    watcher = DefaultMicWatcher(recorder, get_id=lambda: "mic-A")
    watcher.check_once()                           # baseline
    recorder.open()
    recorder._last_frame -= 10.0                   # wedged: open but silent
    old_stream = recorder._stream
    assert watcher.check_once() is False           # no device change...
    assert recorder._stream is not old_stream      # ...but the stream was healed


def test_refresh_default_device_refuses_mid_recording(recorder, monkeypatch):
    from localflow.desktop import capture

    monkeypatch.setattr(capture.sd, "_terminate", lambda: None)
    monkeypatch.setattr(capture.sd, "_initialize", lambda: None)
    recorder.start()
    assert recorder.refresh_default_device() is False  # never mid-utterance
    recorder.stop()
    assert recorder.refresh_default_device() is True


def test_heal_escalates_to_fallback_after_repeated_silence(recorder, monkeypatch):
    """Two failed heals on a silent default -> hunt for any device that
    delivers audio and record from it instead (found live 3 Jul: Samsung USB
    wedged at the OS level while the Intel array worked fine)."""
    from localflow.desktop import capture

    monkeypatch.setattr(capture.sd, "_terminate", lambda: None)
    monkeypatch.setattr(capture.sd, "_initialize", lambda: None)
    probed: list[str] = []
    monkeypatch.setattr(
        capture, "_find_live_input",
        lambda exclude_name, listen_s=1.5:
            (probed.append(exclude_name), (7, "working-mic"))[1])

    recorder.open()
    recorder._last_frame -= 10.0                   # wedged: open but silent
    assert recorder.heal_if_stale() is True        # heal 1: plain reopen
    assert probed == [] and recorder.fallback_active is False

    recorder._last_frame -= 10.0                   # STILL silent after reopen
    recorder._last_heal -= 60.0                    # backoff window elapsed
    assert recorder.heal_if_stale() is True        # heal 2: fallback hunt
    assert probed == ["fake-mic"]                  # wedged device excluded
    assert recorder.fallback_active is True
    assert recorder.input_device == 7

    # frames flowing on the fallback -> healthy again, streak forgotten
    recorder._on_chunk(_chunk(0.1), 1600, None, None)
    assert recorder.heal_if_stale() is False
    assert recorder._heal_streak == 0


def test_fallback_with_no_working_mic_reopens_default(recorder, monkeypatch):
    from localflow.desktop import capture

    monkeypatch.setattr(capture.sd, "_terminate", lambda: None)
    monkeypatch.setattr(capture.sd, "_initialize", lambda: None)
    monkeypatch.setattr(capture, "_find_live_input",
                        lambda exclude_name, listen_s=1.5: None)

    recorder.open()
    for _ in range(2):                             # escalate to the hunt
        recorder._last_frame -= 10.0
        recorder._last_heal -= 60.0
        recorder.heal_if_stale()
    assert recorder.fallback_active is False       # nothing usable found...
    assert recorder.input_device is None           # ...still on the default
    assert recorder._stream is not None            # ...and it was reopened
    # so recovery-by-replug (endpoint change -> refresh) still works


def test_open_or_fallback_uses_configured_mic_when_it_opens(recorder):
    # Happy path: the configured mic opens, no hunt, no fallback flag.
    assert recorder.open_or_fallback() is None
    assert recorder.fallback_active is False
    assert recorder._stream is not None


def test_open_or_fallback_hunts_when_configured_mic_wont_open(recorder, monkeypatch):
    """A wedged/unplugged configured mic raises on stream.start at startup;
    open_or_fallback must hunt for a working device instead of propagating
    (regression: a pinned wedged headset killed the whole app on launch)."""
    from localflow.desktop import capture

    monkeypatch.setattr(capture.sd, "_terminate", lambda: None)
    monkeypatch.setattr(capture.sd, "_initialize", lambda: None)
    monkeypatch.setattr(capture, "_find_live_input",
                        lambda exclude_name, listen_s=1.5: (7, "working-mic"))

    class WedgedStart(_FakeStream):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self._device = kwargs.get("device")

        def start(self):
            if self._device != 7:      # only the working fallback opens
                raise capture.sd.PortAudioError("Unanticipated host error")
            self.active = True

    monkeypatch.setattr(capture.sd, "InputStream", WedgedStart)

    assert recorder.open_or_fallback() is None
    assert recorder.fallback_active is True
    assert recorder.input_device == 7


def test_recorder_falls_back_to_default_when_pin_is_not_an_input(monkeypatch):
    """A pinned index that now points at an OUTPUT endpoint (Samsung headset
    reconnected differently) must NOT crash construction - fall back to the
    system default so the app still starts and Settings stays reachable."""
    from localflow.desktop import capture

    def q(device, kind):
        if device == 14:
            raise ValueError("Not an input device: 'Headset Earphone'")
        return {"name": "default-mic", "default_samplerate": 16_000.0}

    monkeypatch.setattr(capture.sd, "query_devices", q)
    monkeypatch.setattr(capture.sd, "InputStream", _FakeStream)

    rec = capture.StreamRecorder(input_device=14)
    assert rec.input_device is None            # fell back to the system default
    assert rec.fallback_active is True
    assert rec.device_name == "default-mic"


def test_recorder_construction_is_fatal_only_with_no_input_device(monkeypatch):
    from localflow.desktop import capture

    def q(device, kind):
        raise ValueError("no input device on this machine")

    monkeypatch.setattr(capture.sd, "query_devices", q)
    with pytest.raises(ValueError):            # nothing to fall back to
        capture.StreamRecorder(input_device=14)


def test_open_or_fallback_returns_error_when_no_mic_opens(recorder, monkeypatch):
    # No input device anywhere delivers audio -> startup stays fatal.
    from localflow.desktop import capture

    monkeypatch.setattr(capture.sd, "_terminate", lambda: None)
    monkeypatch.setattr(capture.sd, "_initialize", lambda: None)
    monkeypatch.setattr(capture, "_find_live_input",
                        lambda exclude_name, listen_s=1.5: None)

    class AlwaysWedged(_FakeStream):
        def start(self):
            raise capture.sd.PortAudioError("host error")

    monkeypatch.setattr(capture.sd, "InputStream", AlwaysWedged)

    err = recorder.open_or_fallback()
    assert isinstance(err, capture.sd.PortAudioError)
    assert recorder.fallback_active is False


def test_default_change_ends_fallback(recorder, monkeypatch):
    """Replugging the wedged mic fires a default-endpoint change; the swap
    must drop the fallback and follow the default again."""
    from localflow.desktop import capture
    from localflow.desktop.micwatch import DefaultMicWatcher

    monkeypatch.setattr(capture.sd, "_terminate", lambda: None)
    monkeypatch.setattr(capture.sd, "_initialize", lambda: None)
    monkeypatch.setattr(capture, "_find_live_input",
                        lambda exclude_name, listen_s=1.5: (7, "working-mic"))

    current = {"id": "mic-A"}
    watcher = DefaultMicWatcher(recorder, get_id=lambda: current["id"])
    watcher.check_once()                           # baseline
    recorder.open()
    for _ in range(2):                             # wedge -> heal -> fallback
        recorder._last_frame -= 10.0
        recorder._last_heal -= 60.0
        watcher.check_once()
    assert recorder.fallback_active is True

    current["id"] = "mic-B"                        # replug / default changed
    assert watcher.check_once() is True
    assert recorder.fallback_active is False
    assert recorder.input_device is None           # following the default


def test_pinned_device_never_falls_back(monkeypatch):
    from localflow.desktop import capture

    monkeypatch.setattr(capture.sd, "query_devices", _fake_query_devices)
    monkeypatch.setattr(capture.sd, "InputStream", _FakeStream)
    monkeypatch.setattr(capture.sd, "_terminate", lambda: None)
    monkeypatch.setattr(capture.sd, "_initialize", lambda: None)
    monkeypatch.setattr(
        capture, "_find_live_input",
        lambda *a, **k: pytest.fail("a pinned mic must never probe others"))

    rec = capture.StreamRecorder(input_device=3, max_seconds=1.0)
    rec.open()
    for _ in range(3):                             # silent forever: heal only
        rec._last_frame -= 10.0
        rec._last_heal -= 60.0
        rec.heal_if_stale()
    assert rec.fallback_active is False
    assert rec.input_device == 3


def test_single_instance_mutex_detects_second_holder():
    from localflow.desktop.app import _already_running

    # First call creates the session-local mutex (this test process now
    # holds it); any later attempt — here, in-process — sees it exists.
    _already_running()
    assert _already_running() is True


def test_tray_state_images_are_distinct():
    from localflow.desktop.tray import make_state_images

    images = make_state_images()
    assert set(images) == {"idle", "rec", "busy"}
    centers = {s: img.getpixel((32, 32)) for s, img in images.items()}
    assert len(set(centers.values())) == 3   # three visually distinct dots
    assert all(c[3] == 255 for c in centers.values())  # opaque dot centers


# -- M6: background warmup gate ---------------------------------------------


def test_app_with_vad_is_warm_immediately():
    from localflow.desktop.app import LocalFlowApp

    app = LocalFlowApp(engine=None, vad=object(), recorder=None)
    assert app.warm.is_set()


def test_worker_waits_for_warm_event(monkeypatch):
    """M6: a dictation queued before warmup finishes must NOT reach the
    engine until app.warm is set (the engine must never be driven from two
    threads at once), and must then complete normally - never dropped."""
    import time
    import types

    from localflow.desktop import app as app_module

    transcribed: list[str] = []

    def fake_transcribe(audio, engine, cleanup=None, vad=None, preprocess=None):
        transcribed.append("call")
        return types.SimpleNamespace(raw_text="hello", clean_text=None,
                                     cleanup_ms=None, stt_ms=1, device="CPU")

    monkeypatch.setattr(app_module, "ensure_16k_mono", lambda a, r: a)
    monkeypatch.setattr(app_module, "transcribe_audio", fake_transcribe)
    monkeypatch.setattr(app_module, "inject_text", lambda *a, **k: True)

    app = app_module.LocalFlowApp(engine=None, vad=None, recorder=None)
    assert not app.warm.is_set()
    app.start_worker()
    app._work.put((np.ones(160, dtype=np.float32), 16_000, time.perf_counter()))

    time.sleep(0.25)                      # worker had ample time to run...
    assert transcribed == []              # ...but is parked on app.warm

    app.warm.set()
    assert _wait(lambda: transcribed == ["call"])   # released, ran once
    assert _wait(lambda: len(app.latencies_ms) == 1)  # pasted, not dropped


def test_preload_cleanup_loads_when_user_never_touched():
    from localflow.desktop.app import LocalFlowApp

    fake_engine = object()
    app = LocalFlowApp(engine=None, vad=None, recorder=None,
                       cleanup_loader=lambda: fake_engine)
    app.preload_cleanup()
    assert _wait(lambda: app.cleanup is fake_engine)
    assert app.cleanup_active is True


def test_preload_cleanup_never_overrides_a_user_toggle():
    """cleanup_enabled=true preload runs AFTER warmup; if the user already
    toggled cleanup on and back off during warmup, their OFF wins."""
    from localflow.desktop.app import LocalFlowApp

    app = LocalFlowApp(engine=None, vad=None, recorder=None,
                       cleanup_loader=lambda: object())
    assert app.toggle_cleanup() is True      # user: ON (load kicks off)
    assert app.toggle_cleanup() is False     # user: changed mind, OFF
    assert _wait(lambda: not app._cleanup_loading)
    app.preload_cleanup()                    # config preload arrives late
    assert _wait(lambda: not app._cleanup_loading)
    assert app.cleanup is None               # user's OFF was respected
    assert app.cleanup_active is False


# -- M7: pill v2 - click vs hold-to-drag, position clamping ------------------


def _arbiter(slop=6.0):
    from localflow.desktop.overlay import PressArbiter

    return PressArbiter(slop_px=slop)


def test_set_pill_size_enqueues_a_rescale_command():
    """Live pill_size change goes through the command queue (applied on the
    overlay's own UI thread) instead of the old tear-down-the-root rebuild
    that crashed natively with a settings window open over it. Headless-safe:
    no Tk root is created (__init__ bypassed)."""
    import queue

    from localflow.desktop.overlay import WaveformOverlay

    ov = WaveformOverlay.__new__(WaveformOverlay)
    ov._cmd = queue.Queue()
    ov.set_pill_size(1.5)
    assert ov._cmd.get_nowait() == ("rescale", 1.5)


def test_press_quick_release_is_a_click():
    a = _arbiter()
    a.press(100, 100)
    assert a.release(102, 101) == "click"        # within slop, before hold
    assert a.state == a.IDLE


def test_press_move_beyond_slop_cancels_everything():
    a = _arbiter()
    a.press(100, 100)
    assert a.motion(120, 100) is False           # beyond slop -> cancelled
    assert a.state == a.CANCELLED
    assert a.release(120, 100) == "none"         # release does nothing


def test_locked_press_never_drags():
    """Revised spec (6 Jul): no hold-to-drag. A locked press can only be a
    click or nothing, no matter how far the cursor travels."""
    a = _arbiter()
    a.press(100, 100)
    assert a.motion(500, 500) is False
    assert a.release(500, 500) == "none"
    assert a.state == a.IDLE


def test_unlocked_press_drags_immediately_then_release_ends_it():
    a = _arbiter()
    a.press(100, 100, unlocked=True)
    assert a.state == a.DRAGGING                 # no 1s hold needed
    assert a.motion(300, 400) is True            # dragging: move the window
    assert a.release(300, 400) == "drag_end"
    assert a.state == a.IDLE


def test_unlocked_click_without_moving_still_ends_the_drag():
    # One-shot unlock: even a zero-distance press-release saves + relocks.
    a = _arbiter()
    a.press(100, 100, unlocked=True)
    assert a.release(100, 100) == "drag_end"


def test_release_beyond_slop_while_pending_is_not_a_click():
    # A release that jumped past the slop without a motion event in between
    # must not count as a click.
    a = _arbiter()
    a.press(100, 100)
    assert a.release(150, 100) == "none"


def test_clamp_position_keeps_pill_on_the_virtual_screen():
    from localflow.desktop.overlay import clamp_position

    bounds = (0, 0, 1920, 1080)
    assert clamp_position(500, 500, 200, 50, bounds) == (500, 500)   # inside
    assert clamp_position(1900, 1070, 200, 50, bounds) == (1720, 1030)
    assert clamp_position(-50, -50, 200, 50, bounds) == (0, 0)
    # Second monitor left of primary: negative origin is legal space.
    neg = (-1920, 0, 3840, 1080)
    assert clamp_position(-1800, 100, 200, 50, neg) == (-1800, 100)
    assert clamp_position(-5000, 100, 200, 50, neg) == (-1920, 100)
    # Window wider than the screen: pin to the origin, never negative range.
    tiny = (0, 0, 100, 100)
    assert clamp_position(50, 50, 300, 300, tiny) == (0, 0)


def test_tray_menu_builds_with_m7_callbacks():
    """pystray validates action signatures at MenuItem construction - a bad
    callback shape crashes the tray at startup (found live 6 Jul). Building
    the full M7 menu must not raise."""
    from localflow.desktop.tray import TrayIcon

    state = {"mode": "always"}
    tray = TrayIcon(
        "model on GPU", None,
        cleanup_get=lambda: False, cleanup_toggle=lambda: True,
        visibility_get=lambda: state["mode"],
        visibility_set=lambda m: state.update(mode=m),
        reset_pill_position=lambda: None,
    )
    items = {str(i.text) for i in tray._icon.menu.items}
    assert "Show pill" in items
    assert "Reset pill position" in items


# -- M8: live-apply settings plumbing ----------------------------------------


def test_apply_settings_dispatches_live_and_returns_restart():
    from localflow.desktop import config as config_mod
    from localflow.desktop.app import apply_settings

    cfg = config_mod.Config()
    calls = {}
    hooks = {
        "hotkey": lambda v: calls.setdefault("hotkey", v),
        "preprocess": lambda: calls.setdefault("preprocess", True),
        "restore_delay": lambda v: calls.setdefault("restore_delay", v),
        "inject_mode": lambda v: calls.setdefault("inject_mode", v),
        "input_device": lambda v: calls.setdefault("input_device", v),
        "visibility": lambda v: calls.setdefault("visibility", v),
        "pill_size": lambda v: calls.setdefault("pill_size", v),
        "cleanup": lambda v: calls.setdefault("cleanup", v),
    }
    restart = apply_settings({
        "hotkey": "f9",
        "denoise_strength": 0.3,
        "restore_delay_ms": 700,
        "inject_mode": "type",
        "input_device": "Samsung",
        "overlay_visibility": "dictating",
        "pill_size": 1.5,
        "cleanup_enabled": True,
        "device": "NPU",              # restart-only
        "hot_words": ["Codex"],       # restart-only
    }, cfg, hooks)
    assert restart == {"device", "hot_words"}
    assert calls["hotkey"] == "f9"
    assert calls["preprocess"] is True
    assert calls["restore_delay"] == 0.7
    assert calls["inject_mode"] == "type"
    assert calls["input_device"] == "Samsung"
    assert calls["visibility"] == "dictating"
    assert calls["pill_size"] == 1.5
    assert calls["cleanup"] is True
    # cfg mutated so it stays the single source of truth
    assert cfg.hotkey == "f9" and cfg.device == "NPU"


def test_apply_settings_skips_unchanged_values():
    from localflow.desktop import config as config_mod
    from localflow.desktop.app import apply_settings

    cfg = config_mod.Config()
    hooks = {"hotkey": lambda v: pytest.fail("unchanged key dispatched")}
    assert apply_settings({"hotkey": cfg.hotkey}, cfg, hooks) == set()


def test_apply_settings_rebuilds_preprocess_once_for_the_group():
    from localflow.desktop import config as config_mod
    from localflow.desktop.app import apply_settings

    cfg = config_mod.Config()
    counter = {"n": 0}
    hooks = {"preprocess": lambda: counter.__setitem__("n", counter["n"] + 1)}
    apply_settings({"denoise_enabled": False, "normalize_audio": False,
                    "denoise_strength": 0.2}, cfg, hooks)
    assert counter["n"] == 1


def test_mic_test_suppresses_hotkey_down_and_its_release(recorder):
    """M8: while the settings mic test owns the recorder, a hotkey press is
    refused AND its release must not fire on_hotkey_up (the listener still
    tracks the combo as active - an unpaired release would stop and steal
    the test capture as a dictation)."""
    states: list[str] = []
    app = _bare_app(recorder=recorder, on_state=states.append)
    recorder.open()
    app.mic_test_active = True
    recorder.start()                          # the test owns this capture
    app.on_hotkey_down()                      # user presses the combo
    assert states == []                       # refused: no rec state
    app.on_hotkey_up()                        # user releases the combo
    assert app._work.qsize() == 0             # nothing stolen or enqueued
    assert recorder.recording                 # test capture still running
    app.toggle_recording()                    # pill click also refused
    assert recorder.recording
    app.mic_test_active = False
    app._hotkey_suppressed = False
    recorder.stop()
    # normal dictation works again afterwards
    app.on_hotkey_down()
    app.on_hotkey_up()
    assert app._work.qsize() == 1
    assert states == ["rec", "busy"]


def test_claim_mic_session_is_exclusive(recorder):
    """M9 review: Test mic / Calibrate / hotkey all want the recorder;
    exactly one owner may win, atomically."""
    app = _bare_app(recorder=recorder)
    assert app.claim_mic_session() is True
    assert app.claim_mic_session() is False      # second claimant loses
    app.mic_test_active = False                  # owner released (finally)
    recorder.start()                             # a dictation is running
    assert app.claim_mic_session() is False
    recorder.stop()
    assert app.claim_mic_session() is True


def test_mic_watcher_on_switch_fires_after_swap(recorder, monkeypatch):
    from localflow.desktop import capture
    from localflow.desktop.micwatch import DefaultMicWatcher

    monkeypatch.setattr(capture.sd, "_terminate", lambda: None)
    monkeypatch.setattr(capture.sd, "_initialize", lambda: None)

    fired: list[int] = []
    current = {"id": "mic-A"}
    watcher = DefaultMicWatcher(recorder, get_id=lambda: current["id"],
                                on_switch=lambda: fired.append(1))
    watcher.check_once()                         # baseline
    assert fired == []
    current["id"] = "mic-B"
    assert watcher.check_once() is True          # swapped...
    assert fired == [1]                          # ...and the app was told
