"""FR-13 config tests (M4). All filesystem work happens in tmp_path; the
default-search test monkeypatches cwd/APPDATA so a real localflow.toml on the
machine can't leak into the suite.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from localflow.desktop import config
from localflow.desktop.app import apply_cli_overrides


def test_defaults_when_no_file():
    cfg = config.load_config(None)
    assert cfg.hotkey == "alt+`"
    assert cfg.device == "GPU"
    assert cfg.fallback_device == "CPU"
    assert cfg.inject_mode == "paste"
    assert cfg.cleanup_enabled is False
    assert cfg.input_device is None


def test_load_config_reads_values_and_resolves_paths(tmp_path):
    p = tmp_path / "localflow.toml"
    p.write_text(
        'hotkey = "f9"\n'
        'device = "NPU"\n'
        'stt_model_dir = "models/whisper-turbo-ov"\n'
        'cleanup_model_dir = ""\n'
        'input_device = "Samsung"\n',
        encoding="utf-8",
    )
    cfg = config.load_config(p)
    assert cfg.hotkey == "f9"
    assert cfg.device == "NPU"
    assert cfg.input_device == "Samsung"
    # relative paths resolve against the config file's directory, so the
    # tool works regardless of the launch cwd
    assert Path(cfg.stt_model_dir) == tmp_path / "models" / "whisper-turbo-ov"
    # empty path fields stay empty rather than becoming the config dir
    assert cfg.cleanup_model_dir == ""


def test_load_config_keeps_absolute_paths(tmp_path):
    absolute = tmp_path / "elsewhere" / "model"
    p = tmp_path / "localflow.toml"
    p.write_text(f'stt_model_dir = {str(absolute)!r}\n', encoding="utf-8")
    cfg = config.load_config(p)
    assert Path(cfg.stt_model_dir) == absolute


def test_unknown_key_is_an_error_not_silence(tmp_path):
    p = tmp_path / "localflow.toml"
    p.write_text('hotkeyy = "f9"\n', encoding="utf-8")  # typo must not pass
    with pytest.raises(ValueError, match="hotkeyy"):
        config.load_config(p)


def test_bad_inject_mode_is_an_error(tmp_path):
    p = tmp_path / "localflow.toml"
    p.write_text('inject_mode = "teleport"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="inject_mode"):
        config.load_config(p)


def test_bad_toml_is_a_readable_error(tmp_path):
    p = tmp_path / "localflow.toml"
    p.write_text("hotkey = not quoted\n", encoding="utf-8")
    with pytest.raises(ValueError, match="bad config file"):
        config.load_config(p)


def test_find_config_explicit_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        config.find_config(str(tmp_path / "nope.toml"))


def test_find_config_search_order(tmp_path, monkeypatch):
    cwd = tmp_path / "cwd"
    appdata = tmp_path / "appdata"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("APPDATA", str(appdata))

    assert config.find_config() is None  # nothing anywhere -> defaults

    appdata_cfg = appdata / "LocalFlow" / config.CONFIG_FILENAME
    appdata_cfg.parent.mkdir(parents=True)
    appdata_cfg.write_text("", encoding="utf-8")
    assert config.find_config() == appdata_cfg

    cwd_cfg = cwd / config.CONFIG_FILENAME
    cwd_cfg.write_text("", encoding="utf-8")
    assert config.find_config() == cwd_cfg  # cwd beats APPDATA


def test_write_default_config_roundtrips_and_never_overwrites(tmp_path):
    p = tmp_path / "sub" / config.CONFIG_FILENAME
    config.write_default_config(p)
    cfg = config.load_config(p)  # template must parse to the defaults
    defaults = config.Config()
    assert cfg.model_dump(exclude=set(config._PATH_FIELDS)) == \
        defaults.model_dump(exclude=set(config._PATH_FIELDS))
    # path fields carry the default values, resolved against the config dir
    assert Path(cfg.stt_model_dir) == p.parent / defaults.stt_model_dir
    assert Path(cfg.cache_dir) == p.parent / defaults.cache_dir
    p.write_text('hotkey = "f9"\n', encoding="utf-8")
    config.write_default_config(p)  # must not clobber user edits
    assert config.load_config(p).hotkey == "f9"


def _args(**kwargs) -> argparse.Namespace:
    base = dict(model=None, device=None, fallback=None, cache=None,
                hotkey=None, inject_mode=None, input_device=None,
                no_overlay=False)
    base.update(kwargs)
    return argparse.Namespace(**base)


def test_cli_overrides_beat_config_file():
    cfg = config.Config(hotkey="f9", device="NPU")
    out = apply_cli_overrides(cfg, _args(device="CPU", inject_mode="type"))
    assert out.device == "CPU"          # flag given -> wins
    assert out.inject_mode == "type"
    assert out.hotkey == "f9"           # flag not given -> config value stays
    assert out.stt_model_dir == config.Config().stt_model_dir


def test_hot_words_parse_and_default_empty(tmp_path):
    assert config.Config().hot_words == []
    p = tmp_path / "localflow.toml"
    p.write_text('hot_words = ["Codex", "OpenVINO"]\n', encoding="utf-8")
    assert config.load_config(p).hot_words == ["Codex", "OpenVINO"]


def test_no_overlay_flag_disables_overlay():
    assert config.Config().overlay_enabled is True  # on by default
    out = apply_cli_overrides(config.Config(), _args(no_overlay=True))
    assert out.overlay_enabled is False


# -- M7: save_config (comment-preserving round trip) + new pill keys ---------


def test_save_config_preserves_comments_and_updates_value(tmp_path):
    p = tmp_path / "localflow.toml"
    p.write_text(
        "# top-of-file comment that MUST survive\n"
        'hotkey = "alt+`"          # inline comment survives too\n'
        "restore_delay_ms = 500\n",
        encoding="utf-8",
    )
    config.save_config(p, {"hotkey": "f9"})
    text = p.read_text(encoding="utf-8")
    assert "# top-of-file comment that MUST survive" in text
    assert "# inline comment survives too" in text
    assert config.load_config(p).hotkey == "f9"
    assert config.load_config(p).restore_delay_ms == 500


def test_save_config_none_removes_key(tmp_path):
    p = tmp_path / "localflow.toml"
    p.write_text("overlay_position = [100, 200]\n", encoding="utf-8")
    config.save_config(p, {"overlay_position": None})
    assert "overlay_position" not in p.read_text(encoding="utf-8")
    assert config.load_config(p).overlay_position is None


def test_save_config_rejects_unknown_keys(tmp_path):
    p = tmp_path / "localflow.toml"
    p.write_text('hotkey = "f9"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="unknown config keys"):
        config.save_config(p, {"hotkye": "f10"})     # typo must not land
    assert config.load_config(p).hotkey == "f9"      # file untouched


def test_save_config_creates_default_file_first(tmp_path):
    p = tmp_path / "localflow.toml"
    config.save_config(p, {"overlay_visibility": "dictating"})
    cfg = config.load_config(p)
    assert cfg.overlay_visibility == "dictating"
    # the commented default skeleton was written, not a bare one-key file
    assert "hold-to-talk combo" in p.read_text(encoding="utf-8")


def test_overlay_visibility_and_position_validation(tmp_path):
    p = tmp_path / "localflow.toml"
    p.write_text(
        'overlay_visibility = "dictating"\noverlay_position = [960, 1000]\n',
        encoding="utf-8",
    )
    cfg = config.load_config(p)
    assert cfg.overlay_visibility == "dictating"
    assert cfg.overlay_position == [960, 1000]

    p.write_text('overlay_visibility = "sometimes"\n', encoding="utf-8")
    with pytest.raises(ValueError):
        config.load_config(p)

    p.write_text("overlay_position = [1, 2, 3]\n", encoding="utf-8")
    with pytest.raises(ValueError):
        config.load_config(p)


def test_max_utterance_s_default_and_validation(tmp_path):
    assert config.Config().max_utterance_s == 300.0

    p = tmp_path / "localflow.toml"
    p.write_text("max_utterance_s = 0\n", encoding="utf-8")     # 0 = unlimited
    assert config.load_config(p).max_utterance_s == 0.0

    p.write_text("max_utterance_s = 120\n", encoding="utf-8")
    assert config.load_config(p).max_utterance_s == 120.0

    p.write_text("max_utterance_s = -5\n", encoding="utf-8")    # negative
    with pytest.raises(ValueError):
        config.load_config(p)

    p.write_text("max_utterance_s = 99999\n", encoding="utf-8")  # over 1h cap
    with pytest.raises(ValueError):
        config.load_config(p)


def test_stt_language_default_and_load(tmp_path):
    assert config.Config().stt_language == "en"
    p = tmp_path / "localflow.toml"
    p.write_text('stt_language = "auto"\n', encoding="utf-8")
    assert config.load_config(p).stt_language == "auto"
    p.write_text('stt_language = "fr"\n', encoding="utf-8")
    assert config.load_config(p).stt_language == "fr"


def test_filter_hallucinations_default_and_load(tmp_path):
    assert config.Config().filter_hallucinations is True
    p = tmp_path / "localflow.toml"
    p.write_text("filter_hallucinations = false\n", encoding="utf-8")
    assert config.load_config(p).filter_hallucinations is False


def test_auto_gain_default_and_load(tmp_path):
    assert config.Config().auto_gain is True
    p = tmp_path / "localflow.toml"
    p.write_text("auto_gain = false\n", encoding="utf-8")
    assert config.load_config(p).auto_gain is False


def test_stt_language_is_restart_only():
    from localflow.desktop.app import RESTART_KEYS
    assert "stt_language" in RESTART_KEYS


def test_segment_seconds_default_and_validation(tmp_path):
    assert config.Config().segment_seconds == 30.0

    p = tmp_path / "localflow.toml"
    p.write_text("segment_seconds = 0\n", encoding="utf-8")       # off
    assert config.load_config(p).segment_seconds == 0.0

    p.write_text("segment_seconds = 45\n", encoding="utf-8")
    assert config.load_config(p).segment_seconds == 45.0

    p.write_text("segment_seconds = 5\n", encoding="utf-8")       # too choppy
    with pytest.raises(ValueError):
        config.load_config(p)

    p.write_text("segment_seconds = -1\n", encoding="utf-8")      # negative
    with pytest.raises(ValueError):
        config.load_config(p)

    p.write_text("segment_seconds = 900\n", encoding="utf-8")     # over the cap
    with pytest.raises(ValueError):
        config.load_config(p)
