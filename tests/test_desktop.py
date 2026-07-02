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
