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
