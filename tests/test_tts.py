"""TTS seam: clean_for_speech (pure) + Narrator toggle/stop state machine."""

from localflow.core.tts import clean_for_speech


def test_clean_strips_markdown_paragraph_break_becomes_pause():
    out = clean_for_speech("# Title\n\nSome **bold** and _italic_ text.")
    # blank line between heading and body -> a sentence pause, not a run-on
    assert out == "Title. Some bold and italic text."


def test_clean_dehyphenates_line_break():
    assert clean_for_speech("inter-\nnational law") == "international law."


def test_clean_line_break_becomes_pause():
    # every line break is honoured as a pause (period), per the writer's spacing
    assert clean_for_speech("the quick brown\nfox jumps") == "the quick brown. fox jumps."


def test_clean_strips_bullet_markers_and_pauses_between_points():
    out = clean_for_speech("- first item\n- second item\n- third")
    assert out == "first item. second item. third."
    assert not out.lstrip().startswith("-")


def test_clean_pauses_before_bracket():
    assert ", (" in clean_for_speech("See the note (very important) today")


def test_clean_drops_code_blocks_keeps_inline():
    out = clean_for_speech("Run ```py\nprint(1)\n``` then `x = 1` works")
    assert "print(1)" not in out
    assert "code block skipped" in out
    assert "x = 1 works" in out


def test_clean_link_keeps_label_not_url():
    out = clean_for_speech("See [the docs](https://example.com/a?b=c) now")
    assert out == "See the docs now."
    assert "example.com" not in out


def test_clean_caps_length():
    assert len(clean_for_speech("a " * 2000, max_chars=100)) == 100


def test_clean_empty():
    assert clean_for_speech("   \n\t  ") == ""


# -- Narrator (no real audio: monkeypatch winsound + the engine) -----------

def _make_narrator(monkeypatch, spoken):
    from localflow.desktop import narrate

    class FakeEngine:
        def __init__(self, *a, **k):
            self.length_scale = 0.8

        def synthesize_wav(self, text, path):
            spoken.append(text)

    monkeypatch.setattr(narrate, "TtsEngine", FakeEngine)
    # A no-op winsound so tests never play sound or need a real wav file.
    monkeypatch.setattr(narrate.winsound, "PlaySound", lambda *a, **k: None)
    monkeypatch.setattr(narrate, "_wav_seconds", lambda p: 0.0)
    return narrate.Narrator("nonexistent.onnx")


def test_narrator_empty_clipboard_does_not_speak(monkeypatch):
    from localflow.desktop import narrate

    spoken: list = []
    n = _make_narrator(monkeypatch, spoken)
    monkeypatch.setattr(narrate.pyperclip, "paste", lambda: "")
    n.toggle_clipboard()
    assert not n.speaking
    assert spoken == []


def test_narrator_toggle_stops_when_speaking(monkeypatch):
    n = _make_narrator(monkeypatch, [])
    n._speaking = True
    n.toggle_clipboard()          # speaking -> stop, no synth
    assert not n.speaking


def test_narrator_set_speed_updates_loaded_engine(monkeypatch):
    n = _make_narrator(monkeypatch, [])
    n._engine = n._ensure_engine()   # force-load the fake engine
    n.set_length_scale(1.2)
    assert n._engine.length_scale == 1.2


def test_copy_selection_empty_when_nothing_selected(monkeypatch):
    from localflow.desktop import narrate

    clip = {"v": "old clipboard"}
    monkeypatch.setattr(narrate.pyperclip, "paste", lambda: clip["v"])
    monkeypatch.setattr(narrate.pyperclip, "copy",
                        lambda s: clip.__setitem__("v", s))
    monkeypatch.setattr(narrate.time, "sleep", lambda s: None)

    class Dummy:            # keybd_event no-op: nothing was selected to copy
        def keybd_event(self, *a):
            pass
    monkeypatch.setattr(narrate, "_user32", Dummy())

    n = _make_narrator(monkeypatch, [])
    assert n._copy_selection() == ""            # no selection -> nothing
    assert clip["v"] == "old clipboard"         # original clipboard restored


def test_copy_selection_returns_selection_and_restores(monkeypatch):
    from localflow.desktop import narrate

    clip = {"v": "old clipboard"}
    monkeypatch.setattr(narrate.pyperclip, "paste", lambda: clip["v"])
    monkeypatch.setattr(narrate.pyperclip, "copy",
                        lambda s: clip.__setitem__("v", s))
    monkeypatch.setattr(narrate.time, "sleep", lambda s: None)

    class Dummy:            # Ctrl+C "copies" the selection into the clipboard
        def keybd_event(self, vk, *a):
            if vk == narrate._VK_C:
                clip["v"] = "hello selection"
    monkeypatch.setattr(narrate, "_user32", Dummy())

    n = _make_narrator(monkeypatch, [])
    assert n._copy_selection() == "hello selection"
    assert clip["v"] == "old clipboard"         # original clipboard restored
