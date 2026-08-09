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

import threading

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

log = logging.getLogger("localflow.desktop.config")

CONFIG_FILENAME = "localflow.toml"

# Written by write_default_config() (tray "Open settings" on first use, or by
# hand). Keep in sync with the Config defaults below.
DEFAULT_CONFIG_TOML = """\
# LocalFlow settings (FR-13). Device/model changes take effect on restart.
# Relative paths resolve against this file's directory.

hotkey = "alt+`"          # hold-to-talk combo, e.g. "alt+`", "ctrl+space", "f9"
stt_model = "whisper"     # whisper (GPU, the v1.2 baseline) | parakeet
                          # (transducer on CPU: no decode loops, no silence
                          # hallucinations, frees the GPU; English only).
                          # Restart to apply. See docs/parakeet-spike.md.
stt_model_dir = "models/whisper-turbo-ov"
parakeet_model_dir = "models/parakeet-tdt-0.6b-v2-onnx"
stt_language = "en"       # spoken language: "en", "auto" (detect each time),
                          # or a Whisper code like "fr", "es", "de", "zh",
                          # "ja". "en" is most reliable if you dictate English;
                          # "auto" handles other languages. Restart to apply.
device = "GPU"            # GPU | NPU | CPU
fallback_device = "CPU"
cache_dir = "models/ov_cache"
inject_mode = "paste"     # paste (clipboard+Ctrl+V) | type (keystrokes, slower)
restore_delay_ms = 500    # clipboard restore delay; raise it if a slow app
                          # ever pastes your OLD clipboard instead of speech
max_utterance_s = 300     # longest single hold-to-talk capture (seconds).
                          # Whisper stitches long audio internally, so you can
                          # talk for minutes; 0 = no limit (a stuck key then
                          # grows memory ~0.7MB/s until release). 300 = 5 min.
segment_seconds = 30      # Tier 1: past this many seconds of a single hold,
                          # cut at a silence and transcribe that piece WHILE you
                          # keep talking, so the wait on release stays short.
                          # Pieces join back cleanly. 0 = off (one shot on
                          # release). Applied live from Settings.
live_typing = true        # type words into the focused app ~1-2s behind your
                          # voice, then patch the ending to the final text.
                          # Also enables tap-to-toggle (tap the hotkey to start,
                          # tap again to stop, Esc cancels) and blanks the
                          # physical keyboard while dictating. false = the v1.1
                          # way: hold to talk, one paste on release, no
                          # suppression. Applied live from Settings.
overlay_enabled = true    # Wispr-style waveform pill while recording (FR-2)
overlay_visibility = "always"  # always | dictating (show only while dictating)
pill_size = 1.0           # pill scale: 0.5 small ... 2.0 huge
# overlay_position = [960, 1000]  # pill anchor (center-x, bottom-y in logical
                                  # px); set by dragging, removed by reset
cleanup_enabled = false   # LLM cleanup: removes filler, fixes punctuation.
                          # false = not loaded at startup (saves ~2-3GB memory);
                          # the tray toggle loads it on demand. true = always on.
cleanup_model_dir = "models/gemma-3-4b-ov"

# Audio cleanup BEFORE transcription (helps the quiet/noisy built-in mic most).
denoise_enabled = true    # high-pass + gentle spectral noise reduction
normalize_audio = true    # boost quiet mics toward a consistent loudness
denoise_strength = 0.6    # 0 = off, ~0.6 gentle, ~0.9 aggressive (can eat
                          # quiet speech — dial DOWN if accuracy regresses)
auto_gain = true          # nudge the Windows mic input level up when real
                          # dictations arrive too quiet (down on clipping),
                          # like meeting apps do; default-mic mode only
vad_threshold = 0.5       # speech sensitivity: lower = quiet speech accepted,
                          # higher = background noise gated (slider 0.3-0.7)
auto_calibrate_minutes = 0  # re-measure the room-noise floor this often while
                          # idle (the mic-in-use light blinks for ~2s; a sample
                          # with speech in it is discarded). Refreshes the
                          # denoise profile only - it never moves your sliders.
                          # 0 = off (calibrate manually from Settings).
filter_hallucinations = true  # drop the stray word/phrase Whisper appends to
                          # the end on trailing silence (a "you", "thanks for
                          # watching", ...). Uses timestamps to spot the fake
                          # tail, which also stops the real last word being
                          # dropped, but roughly doubles STT time. false = off
                          # (faster, but the artifacts come back).

# Mic: sounddevice index or name substring; omit to follow the system
# default (LocalFlow auto-switches when the Windows default mic changes).
# input_device = "Samsung"

# Jargon Whisper mishears — biases transcription toward these spellings
# (capitalization is kept). Takes effect on restart.
hot_words = []
history_enabled = true    # keep the last 200 dictations for the History
                          # panel in Settings (fully local); false = never
                          # write anything to disk

# Read-aloud (text-to-speech). Press the hotkey to speak the SELECTED text in
# any app; "Narrate clipboard" in the tray speaks whatever is copied. Fully
# local (Piper neural voice, nothing leaves the machine). Press again to stop.
tts_enabled = true
read_aloud_hotkey = "ctrl+alt+r"  # press (not hold) to read selection; again = stop
tts_voice = "models/piper/en_US-hfc_male-medium.onnx"  # path to the .onnx voice
tts_length_scale = 0.8    # speech speed: lower = faster (0.8 ~ 25% faster),
                          # 1.0 = normal. Live from the Settings slider.
tts_volume = 1.0          # narration loudness multiplier: 1.0 = normal, lower
                          # = quieter, up to 2.0 louder. Live from Settings.

numbers_as_digits = false # rewrite spoken numbers as digits after STT
                          # ("forty seven" -> "47"). Restart to apply.

# Personal dictionary: deterministic fixes applied AFTER transcription
# (safe, unlike hot_words). Edit in Settings, or add rows here like:
#   "code x" = "Codex"
[replacements]
"""

_PATH_FIELDS = ("stt_model_dir", "parakeet_model_dir", "cache_dir",
                "cleanup_model_dir", "tts_voice")


class Config(BaseModel):
    """FR-13 fields. extra="forbid" turns config-file typos into errors
    instead of silently ignored settings."""

    model_config = ConfigDict(extra="forbid")

    hotkey: str = "alt+`"
    # Which speech model transcribes: "whisper" (OpenVINO on GPU, the v1.2
    # baseline) or "parakeet" (TDT transducer on CPU - structurally immune to
    # decode loops and silence hallucinations; docs/parakeet-spike.md).
    # Restart to apply, like device/model-dir.
    stt_model: Literal["whisper", "parakeet"] = "whisper"
    stt_model_dir: str = "models/whisper-turbo-ov"
    parakeet_model_dir: str = "models/parakeet-tdt-0.6b-v2-onnx"
    # Spoken language. "en" forces English (most reliable when you dictate
    # English), "auto" detects the language per utterance, or a Whisper code
    # ("fr", "es", "de", "zh", "ja", ...) forces that language. Restart to
    # apply (the STT config is built once at startup, like device/model).
    stt_language: str = "en"
    device: str = "GPU"
    fallback_device: str = "CPU"
    cache_dir: str = "models/ov_cache"
    inject_mode: Literal["paste", "type"] = "paste"
    # How long (ms) the transcript stays on the clipboard before the old
    # clipboard is restored. Too short races slow apps: they end up pasting
    # your OLD clipboard instead of your words. Raise if that ever recurs.
    restore_delay_ms: int = 500
    # Longest single push-to-talk capture, in seconds. Whisper transcribes
    # arbitrarily long audio (it chunks + stitches internally at ~50x realtime
    # on GPU), so this is only a memory safety valve for a stuck key, not a
    # model limit. 0 = unlimited. Applied live from the settings window.
    max_utterance_s: float = 300.0

    @field_validator("max_utterance_s")
    @classmethod
    def _max_utterance_sane(cls, v: float) -> float:
        if v < 0 or v > 3600:
            raise ValueError("max_utterance_s must be between 0 (unlimited) "
                             "and 3600")
        return v
    # Tier 1 (VAD-segmented streaming): once a hold passes this many seconds,
    # a background segmenter cuts the buffer at a recent silence and transcribes
    # that piece DURING the hold, so the wait on release stays short no matter
    # how long you talk. The per-segment texts join back cleanly. 0 = off
    # (transcribe the whole hold in one shot on release). Applied live.
    segment_seconds: float = 30.0

    @field_validator("segment_seconds")
    @classmethod
    def _segment_seconds_sane(cls, v: float) -> float:
        if v < 0 or v > 600:
            raise ValueError("segment_seconds must be 0 (off) or up to 600")
        if 0 < v < 10:
            raise ValueError("segment_seconds must be 0 (off) or at least 10 "
                             "(shorter segments would be too choppy)")
        return v
    # M13: type committed words into the focused app DURING the dictation
    # (~1-2s behind speech), then patch the difference against the final
    # pass. Also turns on tap-to-toggle and keyboard suppression (M11) and
    # the preview decode loop (M12). false bypasses all three: v1.1
    # hold-to-talk with a single paste on release. Applied live.
    live_typing: bool = True
    overlay_enabled: bool = True
    # Pill visibility (M7): "always" keeps the idle chip on screen,
    # "dictating" shows the pill only around a dictation, "never" hides it
    # (tray menu is the way back). overlay_enabled=false maps to "never" at
    # startup but keeps the UI thread alive for the settings window.
    overlay_visibility: Literal["always", "dictating", "never"] = "always"
    # Pill anchor as [center-x, bottom-y] in logical (96-dpi) px, saved by
    # hold-to-drag; None = default bottom-center. Clamped to the virtual
    # screen at draw time so a monitor change can't strand it off-screen.
    overlay_position: list[int] | None = None
    # Pill scale factor (M7): 1.0 = the default (already enlarged) pill;
    # exposed as a slider in the M8 settings window.
    pill_size: float = 1.0

    @field_validator("pill_size")
    @classmethod
    def _pill_size_sane(cls, v: float) -> float:
        if not 0.5 <= v <= 2.0:
            raise ValueError("pill_size must be between 0.5 and 2.0")
        return v

    @field_validator("overlay_position")
    @classmethod
    def _position_is_xy(cls, v):
        if v is not None and len(v) != 2:
            raise ValueError("overlay_position must be [x, y]")
        return v
    cleanup_enabled: bool = False
    # 9 Aug Gemma spike: gemma-3-4b int4 beat qwen2.5-3b on every cleanup
    # case (edits instead of answering; context-gated cloud->Claude fix)
    # at similar size. qwen2.5-3b-ov stays on disk as the fallback.
    cleanup_model_dir: str = "models/gemma-3-4b-ov"
    # Jargon fed to the cleanup prompt (NOT a replacement dictionary): the
    # model fixes a mishearing to one of these only when context agrees.
    cleanup_terms: list[str] = []
    # Audio cleanup before STT. denoise = high-pass + gentle spectral gating;
    # normalize = boost quiet mics. denoise_strength is the safety valve: dial
    # it down (or set denoise_enabled=false) if denoising ever costs accuracy.
    denoise_enabled: bool = True
    normalize_audio: bool = True
    denoise_strength: float = 0.6
    # Auto mic gain (11 Jul): after a dictation that decoded speech but
    # peaked too quiet (or clipped), nudge the WINDOWS mic input level one
    # step - what meeting apps' auto-sensitivity does. Digital normalize
    # can't fix SNR; the capture level can. Default-mic mode only.
    auto_gain: bool = True
    # M9: Silero VAD speech-probability threshold. Lower accepts quiet
    # speech; higher gates background noise. The settings slider exposes
    # 0.3-0.7; the config tolerates a little more headroom.
    vad_threshold: float = 0.5

    @field_validator("vad_threshold")
    @classmethod
    def _vad_threshold_sane(cls, v: float) -> float:
        if not 0.1 <= v <= 0.9:
            raise ValueError("vad_threshold must be between 0.1 and 0.9")
        return v
    # Re-measure the room-noise floor this often while idle (0 = off). Only
    # refreshes the denoise profile; the user's sliders are never moved. A
    # sample the VAD hears speech in is discarded and retried next cycle.
    auto_calibrate_minutes: float = 0.0

    @field_validator("auto_calibrate_minutes")
    @classmethod
    def _auto_calibrate_sane(cls, v: float) -> float:
        if not 0 <= v <= 1440:
            raise ValueError("auto_calibrate_minutes must be between 0 (off) "
                             "and 1440 (daily)")
        return v
    # Drop Whisper's trailing-silence hallucinations (a stray word/phrase
    # appended to the end). When on, STT runs with timestamps so the fake tail
    # is spotted by its timing (and the real final word is no longer dropped),
    # at ~2x decode cost. Applied live. false = fast path, no filtering.
    filter_hallucinations: bool = True
    input_device: int | str | None = None
    # Jargon Whisper mishears — biases transcription toward these spellings.
    # Takes effect on restart.
    hot_words: list[str] = []
    # M10: keep the last 200 dictations for the settings History panel
    # (fully local); false = nothing is ever written to disk.
    history_enabled: bool = True
    # M10: personal dictionary, applied AFTER STT and cleanup by
    # core/dictionary.py — zero STT-accuracy risk, unlike hot_words.
    replacements: dict[str, str] = {}
    # Rewrite spoken numbers as digits ("forty seven" -> "47", "twenty six
    # point two seven" -> "26.27") after STT, on the same pass as the
    # dictionary. Bare "one" is left alone ("no one" must survive).
    # Default off = exact v1.2 surface. Takes effect on restart.
    numbers_as_digits: bool = False
    # Read-aloud (TTS). Fully local Piper voice. read_aloud_hotkey is a PRESS
    # (not hold): press to speak the selection, press again to stop.
    tts_enabled: bool = True
    read_aloud_hotkey: str = "ctrl+alt+r"
    tts_voice: str = "models/piper/en_US-hfc_male-medium.onnx"
    # Speech speed as Piper's length_scale: lower is faster (0.8 ~ 25% faster
    # than the 1.0 baseline). Applied live from the Settings slider.
    tts_length_scale: float = 0.8

    @field_validator("tts_length_scale")
    @classmethod
    def _tts_length_scale_sane(cls, v: float) -> float:
        if not 0.4 <= v <= 2.0:
            raise ValueError("tts_length_scale must be between 0.4 (fast) "
                             "and 2.0 (slow)")
        return v
    # Narration loudness multiplier (Piper volume): 1.0 = as synthesized,
    # lower = quieter, above 1.0 louder (can clip). Applied live from Settings.
    tts_volume: float = 1.0

    @field_validator("tts_volume")
    @classmethod
    def _tts_volume_sane(cls, v: float) -> float:
        if not 0.1 <= v <= 2.0:
            raise ValueError("tts_volume must be between 0.1 and 2.0")
        return v


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
    with _SAVE_LOCK:
        if path.exists():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
    log.info("wrote default config to %s", path)


# RLock: save_config holds it while calling write_default_config, and
# write_default_config also takes it when called directly (tray/overlay
# "Open settings" racing a drag/visibility save must not clobber the file).
_SAVE_LOCK = threading.RLock()


def save_config(path: Path, changes: dict) -> None:
    """Comment-preserving update of localflow.toml (M7/M8, via tomlkit).

    Only known Config fields may be written (same typo protection as load).
    A value of None REMOVES the key (back to the built-in default). Creates
    the default commented file first if none exists. Serialized by a lock:
    the tray thread and the overlay UI thread both save settings."""
    import tomlkit

    unknown = set(changes) - set(Config.model_fields)
    if unknown:
        raise ValueError(f"unknown config keys: {sorted(unknown)}")
    with _SAVE_LOCK:
        write_default_config(path)     # no-op when the file already exists
        doc = tomlkit.parse(path.read_text(encoding="utf-8"))
        for key, value in changes.items():
            if value is None:
                doc.pop(key, None)
            else:
                doc[key] = value
        path.write_text(tomlkit.dumps(doc), encoding="utf-8")
    log.info("saved settings %s to %s", sorted(changes), path)


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
