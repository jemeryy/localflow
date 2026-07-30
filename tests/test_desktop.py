"""Desktop-layer tests (M3). All headless-safe: no audio stream is opened,
no keys are sent, the clipboard is never touched (pyperclip is monkeypatched).
The live end-to-end injection check is scripts/m3_inject_check.py.
"""

from __future__ import annotations

import time as _time

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


def test_stream_recorder_unlimited_never_caps(monkeypatch):
    """max_seconds <= 0 = unlimited: the callback keeps every frame however
    long the key is held (memory grows, no discard)."""
    from localflow.desktop import capture

    monkeypatch.setattr(capture.sd, "query_devices", _fake_query_devices)
    monkeypatch.setattr(capture.sd, "InputStream", _FakeStream)
    rec = capture.StreamRecorder(max_seconds=0, preroll_s=0.2)
    assert rec._max_frames is None
    rec.start()
    for _ in range(30):  # 3s against a 1s-ish machine cap would truncate
        rec._on_chunk(_chunk(0.5), 1600, None, None)
    audio, _ = rec.stop()
    assert len(audio) == 30 * 1600            # nothing discarded


def test_set_max_seconds_updates_cap_live(recorder):
    """set_max_seconds recomputes the frame limit against the current rate;
    0 flips to unlimited. Takes effect on the NEXT dictation."""
    recorder.set_max_seconds(2.0)
    assert recorder._max_frames == 2 * 16_000
    recorder.start()
    for _ in range(25):  # 2.5s against the new 2.0s cap
        recorder._on_chunk(_chunk(0.0), 1600, None, None)
    audio, _ = recorder.stop()
    assert len(audio) == 2 * 16_000
    recorder.set_max_seconds(0)
    assert recorder._max_frames is None


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


def _hold_mode(app, tap_max_s=0.0):
    """Drive this app's hotkey as a HOLD (v1.1 push-to-talk). tap_max_s=0
    makes every key-up a hold release, so a down/up pair still means "one
    dictation" without a real 0.3s wait (M11). The tap-to-toggle path has
    its own tests."""
    app._gate.tap_max_s = tap_max_s
    return app


def _bare_app(recorder=None, on_state=None, tap_max_s=0.0):
    from localflow.desktop.app import LocalFlowApp

    # engine/vad are only touched inside _process, which these tests avoid
    return _hold_mode(LocalFlowApp(engine=None, vad=None, recorder=recorder,
                                   on_state=on_state), tap_max_s)


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


def test_no_output_hook_fires_when_dictation_yields_no_text():
    """Jeremy 11 Jul: a dictation that produces nothing (silence gate,
    garbage decode, dead mic) must give visible feedback - the pill flash
    hook fires on every no-output path, with a mic-specific message when
    the recorder delivered zero frames."""
    import time as _time

    app = _bare_app()
    notes: list[str] = []
    app.on_no_output = notes.append

    app._finish_dictation([], 0, "?", _time.perf_counter(), had_audio=True)
    assert notes and "heard" in notes[-1].lower()      # silence/gated path

    app._finish_dictation([], 0, "?", _time.perf_counter(), had_audio=False)
    assert "mic" in notes[-1].lower()                  # dead-mic path

    app.on_no_output = None                            # unwired: no crash
    app._finish_dictation([], 0, "?", _time.perf_counter(), had_audio=True)

    def boom(message):
        raise RuntimeError("overlay gone")

    app.on_no_output = boom                            # broken: must not raise
    app._finish_dictation([], 0, "?", _time.perf_counter(), had_audio=True)


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

    def fake_transcribe(audio, engine, cleanup=None, vad=None, preprocess=None,
                        filter_hallucinations=True):
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
        "max_utterance": lambda v: calls.setdefault("max_utterance", v),
        "segment": lambda v: calls.setdefault("segment", v),
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
        "max_utterance_s": 0.0,
        "segment_seconds": 45.0,
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
    assert calls["max_utterance"] == 0.0
    assert calls["segment"] == 45.0
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


# -- Tier 1: VAD-segmented streaming ----------------------------------------


def _unlimited_recorder(monkeypatch):
    """A recorder with no per-utterance cap, so drain/snapshot tests are not
    truncated by max_seconds while feeding many chunks."""
    from localflow.desktop import capture

    monkeypatch.setattr(capture.sd, "query_devices", _fake_query_devices)
    monkeypatch.setattr(capture.sd, "InputStream", _FakeStream)
    return capture.StreamRecorder(max_seconds=0, preroll_s=0.2)


def test_drain_segment_keeps_remainder(monkeypatch):
    """drain_segment hands back the front of the buffer and KEEPS the tail as
    the next segment's start, so consecutive segments are contiguous."""
    rec = _unlimited_recorder(monkeypatch)
    rec.start()
    for i in range(10):                          # chunks tagged 0..9
        rec._on_chunk(_chunk(float(i)), 1600, None, None)
    assert rec.buffered_frames() == 10 * 1600

    seg = rec.drain_segment(4 * 1600)            # take the first four chunks
    assert len(seg) == 4 * 1600
    assert float(seg[0]) == 0.0 and float(seg[-1]) == 3.0
    assert rec.buffered_frames() == 6 * 1600     # tail kept

    audio, _ = rec.stop()                        # remainder flows on unbroken
    assert len(audio) == 6 * 1600
    assert float(audio[0]) == 4.0 and float(audio[-1]) == 9.0


def test_drain_segment_empty_when_not_recording(monkeypatch):
    """A cut that races stop() must return nothing, so no stray segment is
    ever enqueued after the on-release final."""
    rec = _unlimited_recorder(monkeypatch)
    assert len(rec.drain_segment(1600)) == 0     # never started
    rec.start()
    rec._on_chunk(_chunk(1.0), 1600, None, None)
    rec.stop()                                   # recording ends, buffer emptied
    assert len(rec.drain_segment(1600)) == 0


def test_snapshot_is_a_copy_and_does_not_stop(monkeypatch):
    rec = _unlimited_recorder(monkeypatch)
    rec.start()
    for _ in range(3):
        rec._on_chunk(_chunk(0.5), 1600, None, None)
    snap = rec.snapshot()
    assert len(snap) == 3 * 1600
    assert rec.recording                         # still going
    rec._on_chunk(_chunk(0.5), 1600, None, None)
    assert len(rec.snapshot()) == 4 * 1600       # live buffer grew...
    assert len(snap) == 3 * 1600                 # ...the old copy did not


def test_emit_segment_guards_id_and_recording(recorder):
    """emit_segment only drains for the current, still-recording dictation -
    a stale id (a cut from a prior hold) or a finished hold is refused."""
    app = _bare_app(recorder=recorder)
    recorder.open()
    assert app.emit_segment(1600, app._dictation_id) is False  # not recording
    app.on_hotkey_down()
    recorder._on_chunk(_chunk(1.0), 1600, None, None)
    assert app.emit_segment(1600, 999) is False                # wrong dictation
    assert app._work.qsize() == 0
    assert app.emit_segment(1600, app._dictation_id) is True
    assert app._work.qsize() == 1


def test_multi_segment_dictation_joins_and_pastes_once(recorder, monkeypatch):
    """Mid-dictation segments are transcribed during the hold and their texts
    accumulate; the on-release final joins them and pastes ONCE, in order."""
    import types

    from localflow.desktop import app as app_module

    pasted: list[str] = []

    def fake_transcribe(audio, engine, cleanup=None, vad=None, preprocess=None,
                        filter_hallucinations=True):
        # encode the segment's first sample so ordering is checkable
        return types.SimpleNamespace(
            raw_text=f"part{int(round(float(audio[0])))}", clean_text=None,
            cleanup_ms=None, stt_ms=5, device="CPU")

    monkeypatch.setattr(app_module, "ensure_16k_mono", lambda a, r: a)
    monkeypatch.setattr(app_module, "transcribe_audio", fake_transcribe)
    monkeypatch.setattr(app_module, "inject_text",
                        lambda text, **k: pasted.append(text) or True)

    app = _hold_mode(app_module.LocalFlowApp(engine=object(), vad=None,
                                             recorder=recorder))
    app.warm.set()
    app.start_worker()
    recorder.open()

    app.on_hotkey_down()                          # dictation begins
    recorder._on_chunk(_chunk(1.0), 1600, None, None)
    assert app.emit_segment(1600, app._dictation_id) is True     # part1
    recorder._on_chunk(_chunk(2.0), 1600, None, None)
    assert app.emit_segment(1600, app._dictation_id) is True     # part2
    recorder._on_chunk(_chunk(3.0), 1600, None, None)            # final tail
    app.on_hotkey_up()                            # part3 + flush

    assert _wait(lambda: len(pasted) == 1)
    assert pasted[0] == "part1 part2 part3"
    assert app.latencies_ms and len(app.latencies_ms) == 1       # one paste


def test_segment_worker_cleanup_runs_once_on_joined_text(recorder, monkeypatch):
    """Cleanup is applied ONCE to the joined transcript, not per segment."""
    import types

    from localflow.desktop import app as app_module

    cleaned: list[str] = []
    pasted: list[str] = []

    def fake_transcribe(audio, engine, cleanup=None, vad=None, preprocess=None,
                        filter_hallucinations=True):
        assert cleanup is None                    # segments never carry cleanup
        return types.SimpleNamespace(
            raw_text=f"p{int(round(float(audio[0])))}", clean_text=None,
            cleanup_ms=None, stt_ms=3, device="CPU")

    class Cleanup:
        def clean(self, text):
            cleaned.append(text)
            return text.upper()

    monkeypatch.setattr(app_module, "ensure_16k_mono", lambda a, r: a)
    monkeypatch.setattr(app_module, "transcribe_audio", fake_transcribe)
    monkeypatch.setattr(app_module, "inject_text",
                        lambda text, **k: pasted.append(text) or True)

    app = _hold_mode(app_module.LocalFlowApp(engine=object(), vad=None,
                                             recorder=recorder,
                                             cleanup=Cleanup()))
    app.warm.set()
    app.start_worker()
    recorder.open()

    app.on_hotkey_down()
    recorder._on_chunk(_chunk(1.0), 1600, None, None)
    assert app.emit_segment(1600, app._dictation_id) is True
    recorder._on_chunk(_chunk(2.0), 1600, None, None)
    app.on_hotkey_up()

    assert _wait(lambda: len(pasted) == 1)
    assert cleaned == ["p1 p2"]                   # cleaned once, joined
    assert pasted[0] == "P1 P2"


def test_last_text_records_final_transcript(recorder, monkeypatch):
    """app.last_text holds the last dictation's final text so the pill's
    "Copy last dictation" can recover it — set independent of history."""
    import types

    from localflow.desktop import app as app_module

    def fake_transcribe(audio, engine, cleanup=None, vad=None, preprocess=None,
                        filter_hallucinations=True):
        return types.SimpleNamespace(raw_text="hello world", clean_text=None,
                                     cleanup_ms=None, stt_ms=5, device="CPU")

    monkeypatch.setattr(app_module, "ensure_16k_mono", lambda a, r: a)
    monkeypatch.setattr(app_module, "transcribe_audio", fake_transcribe)
    monkeypatch.setattr(app_module, "inject_text", lambda text, **k: True)

    app = _hold_mode(app_module.LocalFlowApp(engine=object(), vad=None,
                                             recorder=recorder))
    app.warm.set()
    app.start_worker()
    recorder.open()

    assert app.last_text is None
    app.on_hotkey_down()
    recorder._on_chunk(_chunk(1.0), 1600, None, None)
    app.on_hotkey_up()
    assert _wait(lambda: app.last_text == "hello world")


def test_last_text_survives_a_failed_paste(recorder, monkeypatch):
    """The recover-the-text case: even when injection reports failure (the
    target wasn't focused), last_text still holds the transcript."""
    import types

    from localflow.desktop import app as app_module

    def fake_transcribe(audio, engine, cleanup=None, vad=None, preprocess=None,
                        filter_hallucinations=True):
        return types.SimpleNamespace(raw_text="lost words", clean_text=None,
                                     cleanup_ms=None, stt_ms=5, device="CPU")

    monkeypatch.setattr(app_module, "ensure_16k_mono", lambda a, r: a)
    monkeypatch.setattr(app_module, "transcribe_audio", fake_transcribe)
    monkeypatch.setattr(app_module, "inject_text", lambda text, **k: False)

    app = _hold_mode(app_module.LocalFlowApp(engine=object(), vad=None,
                                             recorder=recorder))
    app.warm.set()
    app.start_worker()
    recorder.open()
    app.on_hotkey_down()
    recorder._on_chunk(_chunk(1.0), 1600, None, None)
    app.on_hotkey_up()
    assert _wait(lambda: app.last_text == "lost words")
    assert not app.latencies_ms                   # nothing pasted


def test_filter_hallucinations_flag_reaches_transcribe(recorder, monkeypatch):
    """app.filter_hallucinations is threaded through to transcribe_audio so a
    live settings toggle takes effect on the next dictation."""
    import types

    from localflow.desktop import app as app_module

    seen: dict = {}

    def fake_transcribe(audio, engine, cleanup=None, vad=None, preprocess=None,
                        filter_hallucinations=True):
        seen["flag"] = filter_hallucinations
        return types.SimpleNamespace(raw_text="hi", clean_text=None,
                                     cleanup_ms=None, stt_ms=1, device="CPU")

    monkeypatch.setattr(app_module, "ensure_16k_mono", lambda a, r: a)
    monkeypatch.setattr(app_module, "transcribe_audio", fake_transcribe)
    monkeypatch.setattr(app_module, "inject_text", lambda text, **k: True)

    app = _hold_mode(app_module.LocalFlowApp(engine=object(), vad=None,
                                             recorder=recorder))
    app.filter_hallucinations = False
    app.warm.set()
    app.start_worker()
    recorder.open()
    app.on_hotkey_down()
    recorder._on_chunk(_chunk(1.0), 1600, None, None)
    app.on_hotkey_up()
    assert _wait(lambda: seen.get("flag") is False)


def test_worker_waits_for_backgrounded_engine(recorder, monkeypatch):
    """M11: the app is now built with engine=None (the model loads on the
    background startup thread). The worker must block on app.warm until the
    engine arrives, never transcribing with a None engine, then flush."""
    import time
    import types

    from localflow.desktop import app as app_module

    def fake_transcribe(audio, engine, cleanup=None, vad=None, preprocess=None,
                        filter_hallucinations=True):
        assert engine is not None      # never driven before the engine is set
        return types.SimpleNamespace(raw_text="deferred", clean_text=None,
                                     cleanup_ms=None, stt_ms=1, device="CPU")

    monkeypatch.setattr(app_module, "ensure_16k_mono", lambda a, r: a)
    monkeypatch.setattr(app_module, "transcribe_audio", fake_transcribe)
    pasted: list[str] = []
    monkeypatch.setattr(app_module, "inject_text",
                        lambda text, **k: pasted.append(text) or True)

    app = _hold_mode(app_module.LocalFlowApp(None, None, recorder))  # engine=None
    assert not app.warm.is_set()                          # not usable yet
    app.start_worker()
    recorder.open()
    app.on_hotkey_down()
    recorder._on_chunk(_chunk(1.0), 1600, None, None)
    app.on_hotkey_up()                                    # queued behind warm
    time.sleep(0.1)
    assert pasted == []                                   # engine not ready -> waits
    # the "startup thread" completes: engine + vad arrive, then warm is set
    app.engine = object()
    app.warm.set()
    assert _wait(lambda: pasted == ["deferred"])


def test_apply_settings_filter_hallucinations_is_live():
    from localflow.desktop import config as config_mod
    from localflow.desktop.app import apply_settings

    cfg = config_mod.Config()
    calls = {}
    hooks = {"filter_hallucinations": lambda v: calls.setdefault("v", v)}
    restart = apply_settings({"filter_hallucinations": False}, cfg, hooks)
    assert restart == set()
    assert calls["v"] is False
    assert cfg.filter_hallucinations is False


def test_apply_settings_stt_language_needs_restart():
    from localflow.desktop import config as config_mod
    from localflow.desktop.app import apply_settings

    cfg = config_mod.Config()
    restart = apply_settings({"stt_language": "fr"}, cfg, {})
    assert restart == {"stt_language"}
    assert cfg.stt_language == "fr"


def test_segmenter_emits_a_cut_at_silence():
    """The Segmenter finds a silence in a long buffer and calls emit with the
    mapped native-frame cut. Uses a fake recorder + a stub VAD so no model or
    mic is needed."""
    import numpy as np

    from localflow.core.audio import TARGET_RATE
    from localflow.desktop.segmenter import Segmenter

    rate = 16_000

    class FakeRecorder:
        recording = True
        rate = 16_000

        def buffered_frames(self):
            return 30 * rate

        def snapshot(self):
            return np.zeros(30 * rate, dtype=np.float32)  # 30s (content unused)

    class StubVad:
        # speech 0-10s then 12-20s: an internal gap at 10-12s (cut ~11s) and a
        # trailing gap 20-30s (cut ~25s) -> the last wins.
        def speech_spans(self, audio16):
            return [(0, 10 * TARGET_RATE), (12 * TARGET_RATE, 20 * TARGET_RATE)]

    emitted: list[tuple[int, int]] = []
    seg = Segmenter(FakeRecorder(), StubVad(),
                    emit=lambda cut, did: emitted.append((cut, did)) or True,
                    current_id=lambda: 7, segment_seconds=25.0)
    seg._tick()
    assert len(emitted) == 1
    cut, did = emitted[0]
    assert did == 7
    assert cut == (20 * rate + 30 * rate) // 2    # trailing-gap midpoint, 25s


def test_segmenter_dormant_when_disabled_or_too_short():
    from localflow.desktop.segmenter import Segmenter

    class FakeRecorder:
        recording = True
        rate = 16_000

        def buffered_frames(self):
            return 5 * 16_000                     # under any sane target

    calls: list = []
    seg = Segmenter(FakeRecorder(), object(),
                    emit=lambda *a: calls.append(a) or True,
                    current_id=lambda: 1, segment_seconds=0)  # off
    seg._tick()
    assert calls == []                            # disabled -> never scans
    seg.set_segment_seconds(30.0)
    seg._tick()
    assert calls == []                            # 5s buffer < 30s target


# ---- M12: streaming preview engine (interim results, revised in place) ---


def _livetyper(texts, window_s=5.0, dictating=True, screen=None):
    """A LiveTyper over a fake recorder and a stub engine that returns
    `texts` one per decode (the last one repeats). No model, no mic.
    `screen` (a one-element list) plays the focused app: each patch is
    applied exactly as backspaces + typing would (M13)."""
    import threading

    import numpy as np

    from localflow.desktop.livetyper import LiveTyper

    class FakeRecorder:
        rate = 16_000

        def snapshot(self):
            return np.zeros(int(window_s * 16_000), dtype=np.float32)

    class StubEngine:
        calls = 0

        def transcribe(self, audio, timestamps=False, rebuild_on_failure=True):
            assert timestamps is False        # preview is the fast path
            assert rebuild_on_failure is False   # a preview never rebuilds
            text = texts[min(self.calls, len(texts) - 1)]
            self.calls += 1
            return text, {}

    def on_patch(n_back, text):
        s = screen[0]
        assert n_back <= len(s)               # never backspace past our text
        screen[0] = s[:len(s) - n_back] + text if n_back else s + text

    lt = LiveTyper(FakeRecorder(), engine_lock=threading.Lock(),
                   is_dictating=lambda: dictating, current_id=lambda: 1,
                   engine=StubEngine(),
                   on_patch=None if screen is None else on_patch)
    return lt


def test_livetyper_types_words_once_two_decodes_agree():
    """Append-only committed stream (27 Jul): a word types once two
    consecutive decodes agree on it (LocalAgreement-2), and is then FINAL."""
    lt = _livetyper(["hello there",
                     "hello there my",
                     "hello there my friend indeed"])
    lt._tick()
    assert lt.text() == ""                    # nothing agreed yet
    lt._tick()
    assert lt.text() == "hello there"
    lt._tick()
    assert lt.text() == "hello there my"


def test_livetyper_stable_hypothesis_commits_every_word():
    """A pause: the hypothesis stops changing, so all of it commits."""
    lt = _livetyper(["hello there", "hello there"])
    lt._tick()
    assert lt.text() == ""
    lt._tick()
    assert lt.text() == "hello there"


def test_livetyper_never_revises_typed_words():
    """No correction (Jeremy 27 Jul): a decode that changes its mind about
    typed words leaves them alone - genuinely new words still flow."""
    screen = [""]
    lt = _livetyper(["i rode a bike",
                     "i rode a bike",
                     "i wrote a book fast",
                     "i wrote a book fast"], screen=screen)
    lt._tick()
    lt._tick()
    assert screen[0] == "i rode a bike"
    lt._tick()                                # rethink: ignored
    assert screen[0] == "i rode a bike"
    lt._tick()                                # the NEW word still arrives
    assert screen[0] == "i rode a bike fast"
    assert screen[0] == lt.text()             # accounting matches the app


def test_livetyper_strips_silence_hallucinations_at_the_live_edge():
    """A pause must never type Whisper's silence artifacts (live 27 Jul:
    "You." typed mid-dictation). Same filter as the final pass."""
    lt = _livetyper(["okay done. Thank you.", "okay done. Thank you."])
    lt._tick()
    lt._tick()
    assert lt.text() == "okay done."          # the stock tail never types
    lt.on_segment_text(1, "okay done.")       # final drain: the provisional
    assert lt.text() == "okay done."          # "." is simply confirmed

    lt = _livetyper(["You.", "You."])         # a silent window decodes to
    lt._tick()                                # ONLY the artifact
    lt._tick()
    assert lt.text() == ""


def test_livetyper_pause_period_is_edited_away_when_the_sentence_continues():
    """Whisper punctuates a mid-sentence pause like a sentence end (live
    29 Jul: "in the future. and I'm okay"). The mark types immediately but
    stays provisional: a lowercase continuation backspaces exactly that
    one character - the sole sanctioned edit (Jeremy 30 Jul: punctuation
    only, never words)."""
    screen = [""]
    lt = _livetyper(["I like it.",
                     "I like it.",
                     "I like it and more",
                     "I like it and more"], screen=screen)
    lt._tick()
    lt._tick()
    assert screen[0] == "I like it."          # typed at once, provisional
    lt._tick()
    lt._tick()                                # lowercase "and": one backspace
    assert screen[0] == "I like it and more"
    assert screen[0] == lt.text()             # accounting matches the app


def test_livetyper_pause_period_stays_for_a_real_sentence_end():
    screen = [""]
    lt = _livetyper(["I like it.",
                     "I like it.",
                     "I like it. Then we left",
                     "I like it. Then we left"], screen=screen)
    lt._tick()
    lt._tick()
    assert screen[0] == "I like it."
    lt._tick()
    lt._tick()                                # capitalised "Then": a real
    assert screen[0] == "I like it. Then we left"   # boundary, no edit
    assert screen[0] == lt.text()


def test_livetyper_late_mark_is_added_at_the_boundary():
    """A mark the fuller decode discovers AFTER the word was typed bare
    still lands (live 30 Jul: "think about What" should have gained the
    "?"). Add is a pure append - no backspace at all."""
    screen = [""]
    lt = _livetyper(["so think about",
                     "so think about",
                     "so think about? What else",
                     "so think about? What else"], screen=screen)
    lt._tick()
    lt._tick()
    assert screen[0] == "so think about"
    lt._tick()
    lt._tick()
    assert screen[0] == "so think about? What else"
    assert screen[0] == lt.text()


def test_livetyper_pause_mark_is_replaced_when_whisper_rethinks_it():
    """"." becomes "," when the decode across the pause says so."""
    screen = [""]
    lt = _livetyper(["we have mods.",
                     "we have mods.",
                     "we have mods, I just want",
                     "we have mods, I just want"], screen=screen)
    lt._tick()
    lt._tick()
    assert screen[0] == "we have mods."
    lt._tick()
    lt._tick()
    assert screen[0] == "we have mods, I just want"
    assert screen[0] == lt.text()


def test_livetyper_final_tail_resolves_the_provisional_mark_too():
    """The authoritative final drain applies the same rule: a lowercase
    tail removes the pause mark it continues past."""
    screen = [""]
    lt = _livetyper(["a google sheet.", "a google sheet."], screen=screen)
    lt._tick()
    lt._tick()
    assert screen[0] == "a google sheet."
    lt.close(1)
    lt.on_segment_text(1, "a google sheet for me")
    assert screen[0] == "a google sheet for me"
    assert lt.typed_for(1) == "a google sheet for me"


def test_livetyper_one_empty_decode_never_erases_typed_words():
    lt = _livetyper(["hello there", "hello there", ""])
    lt._tick()
    lt._tick()
    assert lt.text() == "hello there"
    lt._tick()                                # a flaky decode returns nothing
    assert lt.text() == "hello there"         # typed text is final


def test_livetyper_skips_the_beat_when_the_engine_is_busy():
    """A segment or final decode holding the engine lock must never make a
    preview queue behind it - the tick is dropped."""
    lt = _livetyper(["hello there", "hello there my"])
    lt.engine_lock.acquire()
    try:
        lt._tick()
        lt._tick()
    finally:
        lt.engine_lock.release()
    assert lt.engine.calls == 0 and lt.text() == ""
    lt._tick()
    lt._tick()
    assert lt.text() == "hello there"         # and it resumes cleanly after


def test_livetyper_short_buffer_and_wrong_dictation_are_ignored():
    lt = _livetyper(["hello there"], window_s=0.5)
    lt._tick()
    assert lt.engine.calls == 0               # under min_window_s: no decode

    lt = _livetyper(["hello there", "hello there my"])
    lt._tick()
    lt._tick()
    lt.on_cut(99)                             # a stale dictation's cut
    lt.on_segment_text(99, "garbage")
    assert lt.text() == "hello there"


def test_livetyper_cut_pauses_until_the_segment_tail_lands():
    """A Tier 1 cut: typing pauses until the cut window's authoritative text
    appends its missing tail, so the tail can never land after the next
    window's words (append-only means order is everything)."""
    screen = [""]
    lt = _livetyper(["hello there my",
                     "hello there my",
                     "second part",
                     "second part"], screen=screen)
    lt._tick()
    lt._tick()
    assert screen[0] == "hello there my"
    lt.on_cut(1)                              # segmenter drained the buffer
    lt._tick()                                # paused: no decode, no typing
    assert lt.engine.calls == 2 and screen[0] == "hello there my"
    lt.on_segment_text(1, "Hello there, my friend.")
    assert screen[0] == "hello there my friend."   # only the missing tail
    lt._tick()                                # the lowercase next window
    lt._tick()                                # backspaces the provisional
    assert screen[0] == "hello there my friend second part"   # cut-edge "."
    assert screen[0] == lt.text()


def test_livetyper_flushes_once_when_the_dictation_ends():
    lt = _livetyper(["hello there", "hello there my"])
    lt._tick()
    lt._tick()
    lt.is_dictating = lambda: False
    lt._tick()
    assert lt._flushed and lt.text() == "hello there"   # state kept for M13
    lt._tick()                                          # no second flush
    lt.is_dictating = lambda: True
    lt.current_id = lambda: 2                 # a new dictation resets it all
    lt._tick()
    # Fresh screen: the new dictation's first decode commits nothing yet.
    assert lt.text() == "" and not lt._flushed
    assert lt.typed_for(1) == ""              # the old dictation is gone


# ---- M13: live injection + the final tail + the live_typing switch -------


def test_livetyper_close_blocks_previews_but_the_final_tail_still_lands():
    """A preview decode started while recording finishes seconds later: it
    must not type. The authoritative final drain still appends the words the
    preview never got to - the ONLY typing after release (27 Jul: no
    correction, no duplicates)."""
    screen = [""]
    lt = _livetyper(["hello there", "hello there", "hello there my"],
                    screen=screen)
    lt._tick()
    lt._tick()
    assert screen[0] == "hello there"
    lt.close(1)
    lt._tick()                                # late decode: dropped
    assert screen[0] == "hello there"
    lt.on_segment_text(1, "hello there my friend")   # the final drain
    assert screen[0] == "hello there my friend"      # tail only, no rewrite
    assert lt.typed_for(1) == "hello there my friend"
    assert lt.typed_for(99) == ""             # never touch another dictation


def test_livetyper_discard_blocks_segment_tails_too():
    """Esc: nothing may type after the cancel, authoritative tails included."""
    screen = [""]
    lt = _livetyper(["hello there", "hello there"], screen=screen)
    lt._tick()
    lt._tick()
    lt.close(1, discard=True)
    lt.on_segment_text(1, "hello there my")
    assert screen[0] == "hello there"


def _fake_user32(monkeypatch, alive_hwnd, wnd_class="RichEditD2DPT"):
    """Replace inject's user32 with a recorder: posted messages are captured,
    IsWindow answers for exactly one HWND, GetClassNameW reports
    `wnd_class` (Notepad's RichEdit by default)."""
    from localflow.desktop import inject

    posted: list[tuple] = []

    class FakeUser32:
        @staticmethod
        def IsWindow(h):
            return h == alive_hwnd

        @staticmethod
        def PostMessageW(h, msg, w, lp):
            posted.append((h, msg, w))

        @staticmethod
        def GetClassNameW(h, buf, n):
            buf.value = wnd_class
            return len(wnd_class)

    monkeypatch.setattr(inject, "_user32", FakeUser32())
    return posted


def test_live_patch_posts_to_the_target_window(monkeypatch):
    """With a target the revision is POSTED to that window (it lands in the
    original field even when focus moved), never sent to the focus. A plain
    edit control deletes on WM_CHAR 0x08 — posting a key-down instead made
    the app's own queue put the delete BEHIND our text (garbled rewrites,
    live 27 Jul), so everything must flow through one FIFO char stream."""
    from localflow.desktop import inject

    posted = _fake_user32(monkeypatch, alive_hwnd=42)
    inject.live_patch(2, "hi", target=42)
    assert posted == [(42, 0x0102, 0x08), (42, 0x0102, 0x08),
                      (42, 0x0102, ord("h")), (42, 0x0102, ord("i"))]


def test_live_patch_uses_key_backspaces_on_chromium(monkeypatch):
    """Chromium surfaces (browser, VS Code) delete on the key-down itself
    and ignore a 0x08 char, so they get the key pair — still FIFO with the
    text that follows."""
    from localflow.desktop import inject

    posted = _fake_user32(monkeypatch, alive_hwnd=42,
                          wnd_class="Chrome_RenderWidgetHostHWND")
    inject.live_patch(1, "x", target=42)
    assert [(p[1], p[2]) for p in posted] == [
        (0x0100, 0x08), (0x0101, 0x08), (0x0102, ord("x"))]


def test_live_patch_key_backspaces_on_electrons_outer_window(monkeypatch):
    """Electron (VS Code, Claude desktop) can hand focus to the OUTER
    Chrome_WidgetWin_* window, not the renderer child. It is still
    Chromium: 0x08 chars are silently ignored (live 30 Jul: full stops
    survived their removal), so the class sniff matches by prefix."""
    from localflow.desktop import inject

    posted = _fake_user32(monkeypatch, alive_hwnd=42,
                          wnd_class="Chrome_WidgetWin_1")
    inject.live_patch(1, "x", target=42)
    assert [(p[1], p[2]) for p in posted] == [
        (0x0100, 0x08), (0x0101, 0x08), (0x0102, ord("x"))]


def test_patches_to_a_dead_target_are_dropped(monkeypatch):
    """The target window closed mid-dictation: dropping the patch beats
    spilling backspaces + text into whatever the user is doing now."""
    from localflow.desktop import inject

    posted = _fake_user32(monkeypatch, alive_hwnd=42)
    inject.live_patch(1, "x", target=999)
    inject.patch_text(1, "x", target=999)
    assert posted == []


def test_patch_text_posts_to_the_target_window(monkeypatch):
    from localflow.desktop import inject

    posted = _fake_user32(monkeypatch, alive_hwnd=42)
    inject.patch_text(1, "a", target=42)
    assert [(p[1], p[2]) for p in posted] == [
        (0x0102, 0x08), (0x0102, ord("a"))]


def _patching_app(monkeypatch, typed_text, recorder=None):
    """A bare app whose live typing already typed `typed_text`, with the
    injectors captured instead of sent to Windows."""
    from localflow.desktop import app as app_mod

    sent: dict = {"patch": None, "paste": None}
    monkeypatch.setattr(
        app_mod, "patch_text",
        lambda n, retype, target=None: sent.__setitem__("patch", (n, retype)))
    monkeypatch.setattr(app_mod, "inject_text",
                        lambda text, **kw: sent.__setitem__("paste", text) or True)

    class StubLiveTyper:
        def typed_for(self, dictation_id):
            return typed_text if dictation_id == 5 else ""

    app = _bare_app(recorder=recorder)
    app.livetyper = StubLiveTyper()
    return app, sent


def test_finish_never_corrects_streamed_text(monkeypatch):
    """No correction pass (27 Jul): even when the final decode disagrees
    with what was streamed, the screen is left alone - no patch, no paste.
    The final drain's tail was already appended via on_segment_text."""
    app, sent = _patching_app(monkeypatch, "i rode a bike")
    app._finish_dictation(["I wrote a book"], 10, "GPU", _time.perf_counter(),
                          had_audio=True, dictation_id=5)
    assert sent == {"patch": None, "paste": None}
    assert app.last_text == "I wrote a book"


def test_finish_sends_nothing_when_live_typing_already_matched(monkeypatch):
    app, sent = _patching_app(monkeypatch, "hello there friend")
    app._finish_dictation(["hello there friend"], 10, "GPU",
                          _time.perf_counter(), had_audio=True, dictation_id=5)
    assert sent == {"patch": None, "paste": None}
    assert app.last_text == "hello there friend"      # still recorded


def test_finish_takes_back_typed_text_the_final_pass_threw_away(monkeypatch):
    """The preview typed a hallucinated tail the final pass filtered out.
    Leaving it would put words in the document no dictation produced."""
    app, sent = _patching_app(monkeypatch, "you")
    app._finish_dictation([], 0, "GPU", _time.perf_counter(), had_audio=True,
                          dictation_id=5)
    assert sent["patch"] == (3, "")


def test_finish_pastes_when_live_typing_never_ran(monkeypatch):
    """live_typing off (or a dictation the preview never touched): the
    LiveTyper reports nothing typed, so this is exactly the v1.1 paste."""
    app, sent = _patching_app(monkeypatch, "i rode a bike")
    app._finish_dictation(["I wrote a book"], 10, "GPU", _time.perf_counter(),
                          had_audio=True, dictation_id=4)   # not the typed id
    assert sent["patch"] is None and sent["paste"] == "I wrote a book"


def test_live_typing_off_is_v1_1_hold_to_talk(recorder):
    """The switch has to reach the interaction shell too: no tap toggle, no
    keyboard suppression."""
    import threading

    from localflow.desktop.app import LocalFlowApp

    suppress = threading.Event()
    app = LocalFlowApp(engine=None, vad=None, recorder=recorder,
                       suppress_keys=suppress)
    app.set_live_typing(False)
    app.on_hotkey_down()
    assert recorder.recording
    assert not suppress.is_set()                  # keyboard stays alive
    app.on_hotkey_up()                            # a quick tap = a release
    assert not recorder.recording and app._work.qsize() == 1

    app.set_live_typing(True)
    app.on_hotkey_down()
    assert suppress.is_set()
    app.on_hotkey_up()                            # < 0.3s = tap, latches on
    assert recorder.recording


def test_stop_closes_the_livetyper_before_the_final_is_queued(recorder):
    """Ordering: nothing may commit (and type) once the audio is drained."""
    app = _bare_app(recorder=recorder)
    closed: list[int] = []
    app.livetyper = type("Stub", (), {
        "close": lambda _self, did, discard=False: closed.append(did),
        "typed_for": lambda _self, did: "",
        "on_segment_text": lambda _self, did, text: None,
    })()
    app.on_hotkey_down()
    app.on_hotkey_up()
    assert closed == [app._dictation_id]
    app.on_hotkey_down()
    app.cancel_dictation()                        # Esc closes it too
    assert closed == [1, 2]


# ---- M11: tap/hold gate, keyboard suppression, Esc cancel ----------------


def _gate(tap_max_s=0.3):
    from localflow.desktop.app import TapHoldGate

    return TapHoldGate(tap_max_s=tap_max_s)


def test_gate_hold_starts_and_stops_on_release():
    from localflow.desktop.app import TapHoldGate as G

    g = _gate()
    assert g.key_down(0.0) == G.START
    assert g.key_down(0.05) == G.NONE          # OS auto-repeat while held
    assert g.active
    assert g.key_up(1.2) == G.STOP             # long press = v1.1 hold
    assert not g.active


def test_gate_tap_latches_until_the_next_tap():
    from localflow.desktop.app import TapHoldGate as G

    g = _gate()
    assert g.key_down(0.0) == G.START
    assert g.key_up(0.12) == G.NONE            # tap: keep recording
    assert g.active
    assert g.key_down(5.0) == G.STOP           # second tap ends it...
    assert g.key_up(5.4) == G.NONE             # ...and its release is a no-op
    assert not g.active
    assert g.key_down(6.0) == G.START          # and the next press is fresh


def test_gate_cancel_and_click_paths():
    from localflow.desktop.app import TapHoldGate as G

    g = _gate()
    assert g.cancel() == G.NONE                # nothing running
    g.key_down(0.0)
    assert g.cancel() == G.CANCEL              # Esc mid-hold
    assert g.key_up(0.9) == G.NONE             # the still-held key is swallowed
    assert not g.active

    assert g.click() == G.START                # pill click toggles on...
    assert g.click() == G.STOP                 # ...and off
    g.click()
    assert g.cancel() == G.CANCEL              # Esc also ends a clicked one


def test_hotkey_tap_toggles_and_hold_still_pastes(recorder):
    """M11 through the app: a quick tap latches recording on (nothing
    enqueued yet), the next tap finishes it; a long press behaves exactly
    like v1.1."""
    states: list[str] = []
    app = _bare_app(recorder=recorder, on_state=states.append, tap_max_s=0.3)
    app.on_hotkey_down()
    app.on_hotkey_up()                            # < 0.3s = tap
    assert recorder.recording                     # latched on
    assert app._work.qsize() == 0
    app.on_hotkey_down()                          # second tap
    assert not recorder.recording
    app.on_hotkey_up()                            # swallowed
    assert app._work.qsize() == 1
    assert states == ["rec", "busy"]


def test_esc_cancel_discards_the_dictation(recorder):
    """Esc mid-dictation: recording stops, a cancelled final flushes the
    accumulation, and nothing is pasted or remembered."""
    from localflow.desktop.app import _Segment

    app = _bare_app(recorder=recorder)
    app.on_hotkey_down()
    assert recorder.recording
    app.cancel_dictation()
    assert not recorder.recording
    item = app._work.get_nowait()
    assert item.final and item.cancelled and len(item.audio) == 0

    app._pending_id = item.dictation_id
    app._pending_texts = ["half a sentence"]
    app._handle_segment(item)
    assert app._pending_texts == []               # dropped, not pasted
    assert app.last_text is None
    # the swallowed key-up must not start or stop anything
    app.on_hotkey_up()
    assert app._work.qsize() == 0
    assert isinstance(item, _Segment)


def test_keyboard_suppression_spans_exactly_one_dictation(recorder, monkeypatch):
    """The hook flag is set for the length of a dictation and cleared on
    stop AND on cancel; the synthetic modifier clear fires once per start."""
    from localflow.desktop import app as app_mod

    cleared: list[int] = []
    monkeypatch.setattr(app_mod, "clear_modifiers",
                        lambda: cleared.append(1))
    import threading

    app = _bare_app(recorder=recorder)
    app.suppress_keys = threading.Event()
    app.on_hotkey_down()
    assert app.suppress_keys.is_set() and cleared == [1]
    app.on_hotkey_up()
    assert not app.suppress_keys.is_set()

    app.on_hotkey_down()
    app.cancel_dictation()
    assert not app.suppress_keys.is_set()
    assert cleared == [1, 1]

    # Esc frees the keyboard even if the gate thinks nothing is running.
    app.suppress_keys.set()
    app.cancel_dictation()
    assert not app.suppress_keys.is_set()


def test_key_filter_suppresses_everything_but_the_hotkey_and_esc():
    """The low-level filter (M11): transparent when idle; while a dictation
    runs it swallows every physical key, routing only the hotkey and Esc to
    us. Never starts the real OS hook."""
    import threading
    import types

    from pynput._util.win32 import SystemHook

    from localflow.desktop.app import PushToTalkListener

    events: list[str] = []
    suppress = threading.Event()
    ptt = PushToTalkListener("alt+`",
                             lambda: events.append("start"),
                             lambda: events.append("stop"),
                             suppress=suppress,
                             on_cancel=lambda: events.append("cancel"))
    assert 0xA4 in ptt._vk_keys and 0xC0 in ptt._vk_keys  # lalt, backquote

    def key(vk, flags=0):
        return types.SimpleNamespace(vkCode=vk, flags=flags)

    assert ptt._win32_filter(0x100, key(0x41)) is True       # idle: 'a' passes
    assert ptt._win32_filter(0x100, key(0x41, flags=0x10)) is False  # injected

    suppress.set()
    with pytest.raises(SystemHook.SuppressException):        # 'a' is swallowed
        ptt._win32_filter(0x100, key(0x41))
    assert events == []
    assert ptt._win32_filter(0x100, key(0x41, flags=0x10)) is False  # our typing

    with pytest.raises(SystemHook.SuppressException):        # alt down
        ptt._win32_filter(0x104, key(0xA4))
    with pytest.raises(SystemHook.SuppressException):        # ` down
        ptt._win32_filter(0x104, key(0xC0))
    assert events == ["start"]                               # combo completed
    with pytest.raises(SystemHook.SuppressException):        # ` up
        ptt._win32_filter(0x105, key(0xC0))
    assert events == ["start", "stop"]
    with pytest.raises(SystemHook.SuppressException):        # Esc
        ptt._win32_filter(0x100, key(0x1B))
    assert events == ["start", "stop", "cancel"]


def test_key_filter_locks_only_the_dictation_target(monkeypatch):
    """With a target (27 Jul): keys into the dictated field are swallowed,
    the rest of the keyboard stays alive - including Esc, which only cancels
    from inside the target. The hotkey still works from anywhere."""
    import threading
    import types

    from pynput._util.win32 import SystemHook

    from localflow.desktop import app as app_mod
    from localflow.desktop.app import PushToTalkListener

    events: list[str] = []
    suppress = threading.Event()
    ptt = PushToTalkListener("alt+`",
                             lambda: events.append("start"),
                             lambda: events.append("stop"),
                             suppress=suppress,
                             on_cancel=lambda: events.append("cancel"),
                             target=lambda: 42)

    def key(vk, flags=0):
        return types.SimpleNamespace(vkCode=vk, flags=flags)

    suppress.set()
    monkeypatch.setattr(app_mod, "focused_control", lambda: 42)
    with pytest.raises(SystemHook.SuppressException):   # in the target: dead
        ptt._win32_filter(0x100, key(0x41))
    with pytest.raises(SystemHook.SuppressException):   # Esc cancels here
        ptt._win32_filter(0x100, key(0x1B))
    assert events == ["cancel"]

    monkeypatch.setattr(app_mod, "focused_control", lambda: 7)
    assert ptt._win32_filter(0x100, key(0x41)) is True  # elsewhere: alive
    assert ptt._win32_filter(0x100, key(0x1B)) is True  # Esc means Esc there
    with pytest.raises(SystemHook.SuppressException):   # hotkey: ours anywhere
        ptt._win32_filter(0x104, key(0xA4))
    with pytest.raises(SystemHook.SuppressException):
        ptt._win32_filter(0x104, key(0xC0))
    assert events == ["cancel", "start"]

    # No target captured (injection follows focus): the old M11 full lock.
    ptt._target = lambda: None
    with pytest.raises(SystemHook.SuppressException):
        ptt._win32_filter(0x100, key(0x41))
