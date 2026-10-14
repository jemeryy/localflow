# LocalFlow

**Local voice dictation for Windows, built in Python.** Hold a hotkey, speak,
and insert text into the app you are using. Speech recognition runs on your
computer with Parakeet on CPU or Whisper through OpenVINO.

I built LocalFlow for my own daily dictation, with control over the models,
microphone behaviour and text insertion. It is a working personal project,
shared here as source code. It needs manual setup and is still experimental.

## What it does

- Hold-to-talk or tap-to-toggle dictation, with an optional live typing mode.
- A tray menu, waveform overlay and settings window.
- Microphone fallback, noise calibration and silence detection.
- A personal replacement dictionary and optional local text cleanup.
- Optional read-aloud for selected or copied text using Piper.

Model downloads need an internet connection during setup. Audio and text are
processed locally during dictation; no cloud speech API or API key is needed.
When enabled, the local history keeps the most recent 200 dictations. See
[Privacy and local files](#privacy-and-local-files).

## Quick start

Use **Windows 11 and Python 3.12**. Development has primarily been on an Intel
Core Ultra laptop with Arc 140V graphics. Other Windows hardware has not had
the same testing. Start with the CPU-based Parakeet option below; Intel GPU
acceleration is only needed for the optional Whisper path.

Run these commands in PowerShell:

```powershell
git clone https://github.com/jemeryy/localflow.git
cd localflow
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[parakeet]"
```

Download and load the English Parakeet model once. This downloads several
hundred megabytes into the ignored `models/` directory:

```powershell
.\.venv\Scripts\python.exe -c "import onnx_asr; onnx_asr.load_model('nemo-parakeet-tdt-0.6b-v2', path='models/parakeet-tdt-0.6b-v2-onnx', quantization='int8')"
```

The checked-in `localflow.toml` selects `stt_model = "parakeet"`. Launch from
the repository directory so that this settings file is used:

```powershell
.\.venv\Scripts\localflow.exe
```

Wait for the model to warm up, focus a text field, then hold **Alt + backtick**
to dictate. Release to finish. With live typing enabled, a short tap starts
dictation and another tap stops it. The tray menu provides Settings, Restart
and Quit. Once setup works, `localflow-tray.exe` starts without a console.

Parakeet loading and model download behaviour are documented by
[onnx-asr](https://github.com/istupakov/onnx-asr/blob/main/docs/usage.md).

### Optional: Whisper on Intel graphics

The second engine uses OpenVINO's Whisper runtime. Download the
[OpenVINO INT8 Whisper model](https://huggingface.co/OpenVINO/whisper-large-v3-turbo-int8-ov)
into the configured folder:

```powershell
.\.venv\Scripts\python.exe -m pip install huggingface-hub
.\.venv\Scripts\python.exe -c "from huggingface_hub import snapshot_download; snapshot_download('OpenVINO/whisper-large-v3-turbo-int8-ov', local_dir='models/whisper-turbo-ov')"
```

Set `stt_model = "whisper"` in `localflow.toml` and restart. `device = "GPU"`
uses the Intel GPU; `device = "CPU"` uses the CPU. First-time compilation can
take a while, and CPU Whisper can be substantially slower. The earlier
[hardware experiment](docs/m1-results.md) records measurements on the
development machine, not a guarantee for other hardware.

### Optional: cleanup and read-aloud

Text cleanup is disabled by default. It requires a separately downloaded
OpenVINO language model at `cleanup_model_dir`; leave it off for initial setup.

Read-aloud needs a Piper voice's `.onnx` and matching `.onnx.json` files at
`tts_voice`. Without a voice file, read-aloud remains unavailable and dictation
still works. The default voice path is
`models/piper/en_US-hfc_male-medium.onnx`. **Ctrl + Alt + R** reads the selected
text once a voice is installed. Third-party models retain their own licences.

## How it works

```text
Microphone -> audio cleanup -> speech detection -> local speech model
                                                    |
                                        stable text / final transcript
                                                    |
                                     Windows text insertion + local history
```

The reusable [`localflow/core`](localflow/core) package handles audio and
text processing. [`localflow/desktop`](localflow/desktop) handles microphone
capture, hotkeys, focus, text insertion and the interface.

The main engineering work has been making dictation behave predictably:

- **Keeping the start of speech:** a persistent microphone stream retains a
  short buffer before the hotkey is pressed.
- **Typing while speaking:** repeated decodes establish a stable word prefix,
  with silence-based segments to limit how much audio is processed again.
- **Inserting into the right field:** live typing tracks the original Windows
  control rather than sending text blindly to whichever app has focus.
- **Recovering from device problems:** the app can fall back when a microphone
  fails, and exposes calibration and sensitivity controls.

## Limits

- Windows only. There is no packaged installer or supported macOS/Linux build.
- Recognition accuracy and latency depend on your microphone, model and hardware.
- Text insertion varies between applications. Browser tabs can share a Windows
  control, so changing tabs during dictation can redirect the text. Finish a
  dictation before switching tabs.
- Live typing can still repeat or miss words. Turn off `live_typing` in settings
  to use paste-on-release mode.
- The historical `docs/` and `plans/` contain experiments and earlier designs;
  this README describes the current setup. Dependencies are not fully pinned,
  so a fresh environment may need compatibility adjustments.

## Privacy and local files

- `models/` contains downloaded weights and compile caches. It is not tracked.
- `%LOCALAPPDATA%\LocalFlow\history.jsonl` stores dictation history when
  `history_enabled = true`. Set it to `false` to stop saving new entries; use
  the History panel to clear existing entries.
- Logs and crash reports also live under `%LOCALAPPDATA%\LocalFlow` and may
  contain dictated text. Review them before sharing a bug report.
- `localflow.toml` contains settings and dictionary replacements. Keep personal
  terms out of commits you share publicly.

## Development

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev,parakeet]"
.\.venv\Scripts\python.exe -m pytest tests -q
```

Tests cover audio/text processing, streaming behaviour, configuration and
desktop logic. Some model and audio checks need local assets and are skipped
when those assets are missing. Manual hardware-check scripts live in
[`scripts/`](scripts); those may interact with your microphone or desktop.

Start with [`core/live.py`](localflow/core/live.py) for streaming text rules,
[`desktop/inject.py`](localflow/desktop/inject.py) for Windows text insertion,
and [`desktop/config.py`](localflow/desktop/config.py) for settings.
