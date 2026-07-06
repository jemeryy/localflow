"""Settings file (FR-13): localflow.toml, with CLI flags as overrides.

Search order: --config PATH (must exist) > ./localflow.toml >
%APPDATA%/LocalFlow/localflow.toml > built-in defaults. Path fields written
in a config file resolve relative to that file's directory, so `localflow`
works no matter where it is launched from; CLI paths stay cwd-relative.
Device/model changes take effect on restart (no live reload in v1).
"""

from __future__ import annotations

import logging
import os
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

log = logging.getLogger("localflow.desktop.config")

CONFIG_FILENAME = "localflow.toml"

# Written by write_default_config() (tray "Open settings" on first use, or by
# hand). Keep in sync with the Config defaults below.
DEFAULT_CONFIG_TOML = """\
# LocalFlow settings (FR-13). Device/model changes take effect on restart.
# Relative paths resolve against this file's directory.

hotkey = "alt+`"          # hold-to-talk combo, e.g. "alt+`", "ctrl+space", "f9"
stt_model_dir = "models/whisper-turbo-ov"
device = "GPU"            # GPU | NPU | CPU
fallback_device = "CPU"
cache_dir = "models/ov_cache"
inject_mode = "paste"     # paste (clipboard+Ctrl+V) | type (keystrokes, slower)
restore_delay_ms = 500    # clipboard restore delay; raise it if a slow app
                          # ever pastes your OLD clipboard instead of speech
overlay_enabled = true    # Wispr-style waveform pill while recording (FR-2)
cleanup_enabled = false   # LLM cleanup: removes filler, fixes punctuation.
                          # false = not loaded at startup (saves ~2-3GB memory);
                          # the tray toggle loads it on demand. true = always on.
cleanup_model_dir = "models/qwen2.5-3b-ov"

# Audio cleanup BEFORE transcription (helps the quiet/noisy built-in mic most).
denoise_enabled = true    # high-pass + gentle spectral noise reduction
normalize_audio = true    # boost quiet mics toward a consistent loudness
denoise_strength = 0.6    # 0 = off, ~0.6 gentle, ~0.9 aggressive (can eat
                          # quiet speech — dial DOWN if accuracy regresses)

# Mic: sounddevice index or name substring; omit to follow the system
# default (LocalFlow auto-switches when the Windows default mic changes).
# input_device = "Samsung"

# Jargon Whisper mishears — biases transcription toward these spellings
# (capitalization is kept). Takes effect on restart.
hot_words = []
"""

_PATH_FIELDS = ("stt_model_dir", "cache_dir", "cleanup_model_dir")


class Config(BaseModel):
    """FR-13 fields. extra="forbid" turns config-file typos into errors
    instead of silently ignored settings."""

    model_config = ConfigDict(extra="forbid")

    hotkey: str = "alt+`"
    stt_model_dir: str = "models/whisper-turbo-ov"
    device: str = "GPU"
    fallback_device: str = "CPU"
    cache_dir: str = "models/ov_cache"
    inject_mode: Literal["paste", "type"] = "paste"
    # How long (ms) the transcript stays on the clipboard before the old
    # clipboard is restored. Too short races slow apps: they end up pasting
    # your OLD clipboard instead of your words. Raise if that ever recurs.
    restore_delay_ms: int = 500
    overlay_enabled: bool = True
    cleanup_enabled: bool = False
    cleanup_model_dir: str = "models/qwen2.5-3b-ov"
    # Audio cleanup before STT. denoise = high-pass + gentle spectral gating;
    # normalize = boost quiet mics. denoise_strength is the safety valve: dial
    # it down (or set denoise_enabled=false) if denoising ever costs accuracy.
    denoise_enabled: bool = True
    normalize_audio: bool = True
    denoise_strength: float = 0.6
    input_device: int | str | None = None
    # Jargon Whisper mishears — biases transcription toward these spellings.
    # Takes effect on restart.
    hot_words: list[str] = []


def find_config(explicit: str | None = None) -> Path | None:
    """Resolve which config file to use, or None for built-in defaults.
    An explicitly requested file that doesn't exist is an error, not a
    silent fall-through — the user asked for that file."""
    if explicit is not None:
        p = Path(explicit)
        if not p.is_file():
            raise FileNotFoundError(f"--config {explicit!r} does not exist")
        return p
    for candidate in (Path.cwd() / CONFIG_FILENAME, appdata_config_path()):
        if candidate.is_file():
            return candidate
    return None


def appdata_config_path() -> Path:
    appdata = os.environ.get("APPDATA") or str(Path.home())
    return Path(appdata) / "LocalFlow" / CONFIG_FILENAME


def load_config(path: Path | None) -> Config:
    """Parse `path` (None ⇒ all defaults). Raises ValueError with a readable
    message on bad TOML, unknown keys, or invalid values."""
    if path is None:
        log.info("no config file found — using built-in defaults")
        return Config()
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
        cfg = Config(**data)
    except (tomllib.TOMLDecodeError, ValidationError) as e:
        raise ValueError(f"bad config file {path}:\n{e}") from e
    base = path.resolve().parent
    for field in _PATH_FIELDS:
        value = getattr(cfg, field)
        if value and not Path(value).is_absolute():
            setattr(cfg, field, str(base / value))
    log.info("loaded config from %s", path)
    return cfg


def write_default_config(path: Path) -> None:
    """Create a commented default config (never overwrites an existing one)."""
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
    log.info("wrote default config to %s", path)


def open_settings_editor(path: Path) -> None:
    """Open the settings file in an editor (tray menu + overlay menu).

    os.startfile needs a .toml file association and a stock Windows box has
    none (found live 3 Jul: the Settings button raised and did nothing) —
    fall back to Notepad, which always exists and edits TOML fine."""
    write_default_config(path)
    log.info("opening settings file %s", path)
    try:
        os.startfile(path)  # noqa: S606 - the user's chosen .toml editor
    except OSError:
        import subprocess
        subprocess.Popen(["notepad.exe", str(path)])
