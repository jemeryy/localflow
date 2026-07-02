# PRD: LocalFlow — a local voice-dictation tool (Wispr Flow-style)

**Working title:** LocalFlow
**Owner:** Jeremy (Hesed Consulting)
**Author of this spec:** Claude (from Storm Research briefing, 2 July 2026)
**Status:** v1 scope locked, ready to build
**Target build agent:** Fable
**Companion research:** `storm-reports/wispr-flow-local-clone-briefing.html`

---

## 0. One-liner

A push-to-talk desktop app for Windows: hold a hotkey, speak, release, and your words are typed into whatever app has focus, transcribed by a local Whisper model running on the Intel Arc GPU. No audio ever leaves the machine. Optional local LLM cleanup (filler removal, punctuation) behind a toggle.

This is the layer worth building. The speech model is a commodity; the value is latency + reliable text insertion + cleanup. See the briefing for why.

---

## 1. Goals and non-goals

### v1 goals (must ship)
- G1. Global push-to-talk: hold a configurable hotkey, record mic; release, transcribe, insert text at the cursor in the active app.
- G2. Fully local. No network calls for transcription. No audio or text leaves the device.
- G3. Runs accelerated on the Intel Arc 140V iGPU via OpenVINO, with a CPU fallback.
- G4. Feels fast: p50 end-to-end (key-release to text pasted) under ~1.2s for a typical 5s utterance.
- G5. Does not clobber the user's clipboard (save and restore around the paste).
- G6. A reusable Python core (`localflow.core`) with a clean interface, so FARA/cloud can call the same code later without touching the desktop layer.

### v1 "should" (build the hook, default OFF)
- S1. Optional LLM cleanup stage (remove filler words, fix punctuation/capitalisation), running locally on OpenVINO, applied asynchronously so it never blocks the first paste.

### Non-goals for v1 (explicitly out of scope, do not build)
- N1. FARA web integration (browser mic to server). Phase 2. Keep the core seam clean for it, build nothing.
- N2. Cloud STT / any hosted inference. Phase 3.
- N3. Streaming / live-as-you-speak transcription. v1 is record-then-transcribe (batch on release). Streaming is a later enhancement.
- N4. Custom vocabulary, dictation commands ("new line", "delete that"), multi-language switching. Later.
- N5. macOS / Linux builds. Windows 11 only for v1.
- N6. Parakeet or any non-Whisper STT model (immature on Intel, revisit later).
- N7. Installer / code-signing / auto-update. v1 runs from source.

---

## 2. Target user and environment

- Single user (Jeremy), Windows 11.
- Hardware: Intel Core Ultra (Lunar Lake), **Intel Arc 140V iGPU (Xe2)** + Intel AI Boost NPU. **No NVIDIA / no CUDA.**
- Language: Python 3.11+.
- English dictation only for v1.

Every latency number in this PRD assumes this machine. The build must log which OpenVINO device it actually ran on (GPU / NPU / CPU) at startup.

---

## 3. Architecture

One shared core, one thin desktop client. The core is the FARA/cloud reuse point.

```
  ┌──────────────────────────────────────────────────────────────┐
  │  localflow.core  (pure Python, no UI, no hotkeys)             │
  │                                                              │
  │   record_audio() ─▶ VAD trim ─▶ SttEngine.transcribe()       │
  │                                     │                        │
  │                                     ▼                        │
  │                        (optional) CleanupEngine.clean()      │
  │                                     │                        │
  │                                     ▼  returns TranscriptResult│
  └──────────────────────────────────────────────────────────────┘
        ▲                                          ▲
        │ (v1) desktop client                      │ (v2, DO NOT BUILD YET)
        │ localflow.desktop                        │ FARA FastAPI route
        │  - global hotkey (push-to-talk)          │  - browser MediaRecorder
        │  - mic capture                           │    POSTs audio to server
        │  - calls core                            │  - server calls the SAME core
        │  - injects text (clipboard + Ctrl+V)     │  - returns text to web UI
        ▼                                          ▼
   any Windows app                            FARA web UI
```

**Rule:** the core must not import anything from the desktop layer, must not touch the clipboard, the keyboard, or the network. It takes audio in and returns text. This is what makes G6 real.

---

## 4. The core interface (build to this contract)

`localflow/core/__init__.py` exposes:

```python
@dataclass
class TranscriptResult:
    raw_text: str              # verbatim STT output
    clean_text: str | None     # cleanup output, or None if cleanup disabled/failed
    duration_s: float          # length of the trimmed audio
    stt_ms: int                # STT compute time
    cleanup_ms: int | None
    device: str                # "GPU" | "NPU" | "CPU" actually used
    model: str                 # e.g. "whisper-large-v3-turbo-int8"

class SttEngine:
    def __init__(self, model_dir: str, device: str = "GPU",
                 fallback_device: str = "CPU"): ...
    def transcribe(self, audio_16k_mono: np.ndarray) -> tuple[str, dict]:
        """audio: float32 mono @16kHz. Returns (text, meta)."""

class CleanupEngine:                # optional, may be None
    def __init__(self, model_dir: str, device: str = "GPU"): ...
    def clean(self, text: str) -> str: ...

def transcribe_audio(
    audio_16k_mono: np.ndarray,
    stt: SttEngine,
    cleanup: CleanupEngine | None = None,
    vad: "VadGate | None" = None,
) -> TranscriptResult: ...
```

The desktop client only ever calls `record_audio()` (its own) then `transcribe_audio(...)`. The future FARA route calls the same `transcribe_audio(...)` with audio decoded from the browser upload.

---

## 5. Tech stack (exact packages)

Confirmed against current Intel Arc / OpenVINO sources (see briefing references and the Intel Arc stack note).

| Concern | Package(s) | Notes |
|---|---|---|
| STT runtime | `openvino-genai`, `openvino`, `optimum-intel[openvino]` | OpenVINO GenAI `WhisperPipeline`. Only runtime that targets both Arc iGPU and NPU with one API. |
| STT model | `openai/whisper-large-v3-turbo`, exported INT8 | Near-large accuracy at ~5x speed. Fallback: `base`/`small` for lower latency. |
| Audio capture | `sounddevice`, `soundfile`, `librosa` | Capture mic, resample to 16kHz mono float32. |
| VAD | `silero-vad`, `onnxruntime` | Runs on CPU, <1ms per 30ms frame. Leave GPU/NPU for STT. |
| Global hotkey | `pynput` | Push-to-talk (key-down starts, key-up stops). No admin needed. |
| Clipboard | `pyperclip` (text) + `pynput` to send Ctrl+V | Save/restore around paste. |
| Cleanup LLM (S1) | `openvino-genai` `LLMPipeline` | Qwen2.5-3B-Instruct or Llama-3.2-3B-Instruct, INT4, on GPU or NPU. Same runtime as STT, no Ollama needed. |
| Config | `pydantic` + a TOML/JSON file | Settings (hotkey, model, device, cleanup on/off). |

**One-time model export (run before first launch, document in README):**
```
pip install openvino-genai openvino "optimum-intel[openvino]" librosa soundfile sounddevice silero-vad onnxruntime pynput pyperclip pydantic
optimum-cli export openvino --model openai/whisper-large-v3-turbo --weight-format int8 models/whisper-turbo-ov
# optional cleanup model (S1):
optimum-cli export openvino --model Qwen/Qwen2.5-3B-Instruct --weight-format int4 models/qwen2.5-3b-ov
```
Then `ov_genai.WhisperPipeline("models/whisper-turbo-ov", "GPU")`.

---

## 6. Functional requirements

### Recording / hotkey
- FR-1. A configurable global hotkey (default: hold `Ctrl+Space`, or a side key). Key-down starts recording, key-up stops. Push-to-talk, not toggle, for v1.
- FR-2. While recording, show a small unobtrusive indicator (system tray icon state change or a tiny always-on-top dot). No modal, no stealing focus.
- FR-3. Capture mic at the device default, resample to 16kHz mono float32 for the model. Cap a single utterance at 60s (safety).

### VAD
- FR-4. Run Silero VAD over the captured buffer and trim leading/trailing non-speech before sending to STT. If VAD finds no speech, produce no output and do not paste anything (prevents Whisper hallucinating on silence, per the Calm-Whisper finding).

### Transcription
- FR-5. Transcribe the trimmed audio with the OpenVINO Whisper pipeline on the configured device.
- FR-6. Device selection order: try configured device (default GPU) → on init/inference failure, fall back to CPU and log a warning. Never crash the app because the GPU pipeline failed.
- FR-7. Log per-utterance: device used, model, audio duration, STT ms. (Needed for the latency acceptance test.)

### Cleanup (S1, optional, default OFF)
- FR-8. If cleanup is enabled, paste the raw transcript first, then run the local LLM cleanup asynchronously; when it returns, replace the pasted text (select-back-and-retype or a diff-replace) OR, simpler for v1.1, only apply cleanup when enabled and accept the added latency with a clear setting label "slower, cleaner". Pick the paste-first-then-replace path only if reliable; otherwise gate cleanup as a synchronous "clean mode" the user opts into. Default OFF so v1 latency is protected.
- FR-9. Cleanup prompt is fixed and minimal: remove filler words (um, uh, like), fix punctuation and capitalisation, do NOT paraphrase, do NOT add content, return only the corrected text.

### Text injection
- FR-10. Insert via clipboard: (a) save current clipboard text, (b) set clipboard to the transcript, (c) send `Ctrl+V`, (d) wait ~120ms, (e) restore the saved clipboard. The restore delay matters: restoring too early races the paste and corrupts it.
- FR-11. Accept that injection into apps running as administrator will silently fail (Windows UIPI: a normal-integrity process may only inject into equal-or-lower integrity apps, and the failure returns no error). Document this limit; do not try to defeat it in v1.
- FR-12. Provide a config option `inject_mode: paste | type`. `type` sends the text as synthetic keystrokes (slower, for apps that block paste). Default `paste`.

### Settings / lifecycle
- FR-13. A settings file controls: hotkey, `stt_model_dir`, `device` (GPU/NPU/CPU), `cleanup_enabled`, `cleanup_model_dir`, `inject_mode`. Changing device/model takes effect on restart.
- FR-14. Runs in the system tray with: status, toggle cleanup, open settings, quit. Loads model once at startup (not per utterance).

---

## 7. Latency budget (target machine, cleanup OFF)

For a ~5s utterance, from key-release to text pasted:
- VAD trim: <30ms
- STT (turbo INT8, iGPU): ~0.4–1.2s
- Injection: <150ms
- **Target: p50 < 1.2s, p95 < 2.0s** for utterances up to 10s.
- If p50 exceeds 1.2s on turbo, the build should be switchable to the `small` or `base` model via config (lower latency, slightly lower accuracy) without code changes.

---

## 8. Acceptance tests (definition of done for v1)

All must pass on the target machine.
- AT-1 (happy path): Focus Notepad, hold hotkey, say "the quick brown fox jumps over the lazy dog", release → that sentence appears in Notepad, correctly, within the latency budget.
- AT-2 (clipboard safety): Copy the text "PRESERVE_ME". Dictate a sentence. After paste, `Ctrl+V` again elsewhere still yields "PRESERVE_ME".
- AT-3 (silence gate): Hold hotkey, stay silent 3s, release → nothing is pasted, no hallucinated text.
- AT-4 (latency): Run 10 dictations of a fixed ~5s sentence; log release-to-paste ms; report p50 and p95; p50 < 1.2s.
- AT-5 (device + fallback): On normal launch, logs show device=GPU. Force a GPU init failure (e.g. bad device string) → app falls back to CPU, still works, logs the fallback.
- AT-6 (three real apps): Same dictation inserts correctly into (a) Notepad, (b) a Chrome text box / address bar, (c) the VS Code editor.
- AT-7 (core isolation): `localflow.core` imports and runs `transcribe_audio()` on a WAV file in a plain script with NO desktop/hotkey/clipboard imports loaded. Proves the FARA seam.
- AT-8 (cleanup, if S1 built): With cleanup ON, "um so like the meeting is uh on tuesday" becomes "The meeting is on Tuesday." with no added content.

---

## 9. Build order (milestones)

1. **M1 core skeleton:** `SttEngine` on OpenVINO, transcribe a WAV file to text on the iGPU. Passes AT-7 shape + prints device. (De-risks the Intel path first, before any UI.)
2. **M2 capture + VAD:** mic capture, resample, Silero VAD trim. Feed into M1.
3. **M3 hotkey + injection:** push-to-talk hotkey, clipboard save/paste/restore. Passes AT-1, AT-2, AT-3.
4. **M4 latency + fallback + tray:** device fallback, latency logging, tray UI, config file. Passes AT-4, AT-5, AT-6.
5. **M5 (optional) cleanup:** OpenVINO LLMPipeline cleanup behind the toggle. Passes AT-8.

Ship M1–M4 as v1. M5 is v1.1.

---

## 10. Risks and open questions

- R1. **OpenVINO Whisper on the Arc iGPU is the load-bearing assumption.** M1 exists to prove it on day one. If the iGPU path underperforms or is unstable, fall back to the NPU (`device="NPU"`) or whisper.cpp Vulkan as a plan B (both confirmed to run on this chip).
- R2. **First-run model export** downloads and converts a multi-GB model. Must be a documented one-time setup step, not a silent runtime download.
- R3. **Cleanup replace-in-place (FR-8)** is the fiddliest UX. If paste-first-then-replace is unreliable, ship cleanup as a synchronous opt-in mode instead. Do not let it hold up v1.
- R4. **The `type` inject fallback** (FR-12) can misfire on Unicode/focus races; keep `paste` the default.

---

## 11. Phase 2 seam (FARA) — reference only, DO NOT BUILD in v1

When FARA integration comes: add a FastAPI route in FARA's backend that accepts an audio upload from the browser (`MediaRecorder`), decodes it to 16kHz mono, and calls the exact same `transcribe_audio(...)` from `localflow.core`. Keep STT self-hosted (local or on FARA's own server), never a third-party cloud STT, because client audio is personal data under the PDPA and a cloud vendor would become a data processor. No new model work; the core is already the shared piece.

---

*Built from a 5-lens Storm Research briefing (practitioner, academic, skeptic, economist, historian), 21/21 citations verified against primary sources. Companion file: `wispr-flow-local-clone-briefing.html`.*
