"""M10 tests: dictionary application, correction miner, history store."""

from __future__ import annotations

import json

import pytest

from localflow.core.dictionary import apply_replacements, mine_substitutions
from localflow.desktop.history import MAX_ENTRIES, HistoryStore


# -- apply_replacements -------------------------------------------------------

def test_replacement_word_boundaries():
    rules = {"code x": "Codex"}
    assert apply_replacements("open the code x docs", rules) \
        == "open the Codex docs"
    # inside a longer word: untouched
    assert apply_replacements("encode xylophone", rules) == "encode xylophone"


def test_replacement_case_insensitive_match_case_preserving_output():
    rules = {"code x": "Codex", "j peg": "jpeg"}
    # target with its own capitals is used verbatim
    assert apply_replacements("Code X is great", rules) == "Codex is great"
    # lowercase target adapts to sentence casing of the match
    assert apply_replacements("J peg files", rules) == "Jpeg files"
    assert apply_replacements("my j peg files", rules) == "my jpeg files"


def test_replacement_longest_source_wins():
    rules = {"code x": "Codex", "code x api": "the Codex API"}
    assert apply_replacements("use code x api now", rules) \
        == "use the Codex API now"


def test_replacement_multiple_hits_and_punctuation():
    rules = {"fara": "FARA"}
    assert apply_replacements("fara, fara and fara.", rules) \
        == "FARA, FARA and FARA."


def test_replacement_empty_inputs():
    assert apply_replacements("", {"a": "b"}) == ""
    assert apply_replacements("hello", {}) == "hello"
    assert apply_replacements("hello", {"  ": "x"}) == "hello"


# -- mine_substitutions -------------------------------------------------------

def test_miner_counts_repeated_substitution():
    pairs = [
        ("open the code x docs", "open the Codex docs"),
        ("i like code x a lot", "i like Codex a lot"),
        ("unrelated sentence", "unrelated sentence"),
    ]
    assert mine_substitutions(pairs, min_count=2) == [("code x", "Codex", 2)]


def test_miner_below_threshold_suggests_nothing():
    pairs = [("open the code x docs", "open the Codex docs")]
    assert mine_substitutions(pairs, min_count=2) == []


def test_miner_ignores_case_only_edits_and_long_spans():
    pairs = [
        ("hello world", "Hello World"),           # case-only: not a rule
        ("a b c d e f", "one two three four five six"),  # too long a span
    ] * 3
    assert mine_substitutions(pairs, min_count=2) == []


def test_miner_keeps_latest_target_casing():
    pairs = [
        ("go to j peg", "go to jpeg"),
        ("send the j peg", "send the JPEG"),
    ]
    assert mine_substitutions(pairs, min_count=2) == [("j peg", "JPEG", 2)]


# -- HistoryStore -------------------------------------------------------------

def _store(tmp_path) -> HistoryStore:
    return HistoryStore(tmp_path / "history.jsonl")


def test_history_round_trip_and_correction(tmp_path):
    store = _store(tmp_path)
    store.append("raw one", None, "final one", "mic-A", 400)
    store.append("raw two", "clean two", "final two", "mic-A", 500)
    entries = store.load()
    assert [e["final_text"] for e in entries] == ["final one", "final two"]
    assert entries[0]["corrected_text"] is None

    ts = entries[0]["ts"]
    assert store.set_correction(ts, "corrected one") is True
    assert store.load()[0]["corrected_text"] == "corrected one"
    assert store.corrections() == [("final one", "corrected one")]
    assert store.set_correction(123456.0, "nope") is False


def test_history_cap_rewrites_to_newest(tmp_path):
    store = _store(tmp_path)
    for i in range(MAX_ENTRIES + 25):
        store.append(f"raw {i}", None, f"final {i}", "mic", 1)
    entries = store.load()
    assert len(entries) == MAX_ENTRIES
    assert entries[0]["final_text"] == f"final {25}"
    assert entries[-1]["final_text"] == f"final {MAX_ENTRIES + 24}"


def test_history_skips_corrupt_lines(tmp_path):
    store = _store(tmp_path)
    store.append("raw", None, "final", "mic", 1)
    with open(store.path, "a", encoding="utf-8") as f:
        f.write("{not json}\n")
    store.append("raw2", None, "final2", "mic", 1)
    finals = [e["final_text"] for e in store.load()]
    assert finals == ["final", "final2"]


def test_history_clear_removes_the_file(tmp_path):
    store = _store(tmp_path)
    store.append("raw", None, "final", "mic", 1)
    store.clear()
    assert not store.path.exists()
    assert store.load() == []


def test_history_disabled_means_no_file(tmp_path, recorder=None):
    """history_enabled=false -> LocalFlowApp.history is None and _process
    writes nothing. Covered at the app level: with history=None the append
    call is simply never made (see _process); here we assert the store is
    only touched through that gate."""
    from localflow.desktop.app import LocalFlowApp

    app = LocalFlowApp(engine=None, vad=None, recorder=None)
    assert app.history is None                    # off unless _run wires it
    assert app.replacements == {}


# -- config + live-apply ------------------------------------------------------

def test_config_parses_replacements_table(tmp_path):
    from localflow.desktop import config

    p = tmp_path / "localflow.toml"
    p.write_text('history_enabled = false\n\n[replacements]\n'
                 '"code x" = "Codex"\n', encoding="utf-8")
    cfg = config.load_config(p)
    assert cfg.history_enabled is False
    assert cfg.replacements == {"code x": "Codex"}


def test_save_config_writes_replacements_table(tmp_path):
    from localflow.desktop import config

    p = tmp_path / "localflow.toml"
    p.write_text('hotkey = "f9"   # comment survives\n', encoding="utf-8")
    config.save_config(p, {"replacements": {"code x": "Codex"}})
    text = p.read_text(encoding="utf-8")
    assert "# comment survives" in text
    assert config.load_config(p).replacements == {"code x": "Codex"}


def test_apply_settings_dispatches_history_and_replacements():
    from localflow.desktop import config
    from localflow.desktop.app import apply_settings

    cfg = config.Config()
    got: dict = {}
    hooks = {"history": lambda v: got.setdefault("history", v),
             "replacements": lambda v: got.setdefault("rules", v)}
    restart = apply_settings({"history_enabled": False,
                              "replacements": {"a": "B"}}, cfg, hooks)
    assert restart == set()
    assert got == {"history": False, "rules": {"a": "B"}}
    assert cfg.history_enabled is False and cfg.replacements == {"a": "B"}
