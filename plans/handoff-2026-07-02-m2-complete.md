# Session handoff — M2 complete (2 July 2026)

## Where the project stands

**M1 and M2 are DONE and fully signed off.** Evidence: `docs/m1-results.md`,
`docs/m2-results.md`. Both milestones are committed (M1 + M2 commits on top of
the fd47854 scaffold). The durable `localflow` package exists and is installed
editable. **Next milestone: M3 — push-to-talk hotkey + clipboard injection
(AT-1/2/3).**

Key decisions locked during M2 (do not relitigate):
- **GPU is the primary STT device.** The M1 trailing-word worry is CLOSED as a
  synthetic-TTS artifact: real mic speech with a deliberately trailed-off last
  word transcribes identically on GPU and NPU. NPU stays plan-B.
- **OpenVINO compile cache lives at `models/ov_cache`** (moved from `.tmp/`;
  the M1 handoff doc predates this). Warm loads: GPU ~1–3s, NPU ~3–5s. Never
  delete it — the NPU blob costs 188s to recompile.
- **Resampler is soxr, not librosa.** Capture at mic native rate, resample in
  core via `ensure_16k_mono()` (FR-3).
- VAD pads: 0.5s trailing / 0.2s leading around the outermost speech frames.

## What exists (all committed)

- `localflow/core/` — PURE package per PRD §4: `SttEngine` (device fallback,
  CACHE_DIR, timing meta), `VadGate` (silero-vad ONNX on CPU, silence gate
  FR-4), `transcribe_audio()` + `TranscriptResult`, `ensure_16k_mono()`,
  `CleanupEngine` stub (raises until M5). AT-7 purity is tested.
- `localflow/desktop/capture.py` — `record_audio(seconds)` fixed-duration
  capture. **M3 will need a start/stop streaming recorder instead** (see below).
- `scripts/m2_capture_vad.py` — acceptance script (`--wav`, `--save`,
  `--expect-silence`, `--list-devices`). `scripts/m1_prove_igpu.py` — throwaway,
  kept for reference.
- `tests/test_core.py` — 7 tests, all pass (~13s incl. GPU engine load).
  Run: `.venv\Scripts\python.exe -m pytest tests -v`
- `pyproject.toml` — editable install: `pip install -e .[dev]`

## Environment (gitignored, on disk)

- `.venv/` — Python 3.12; OpenVINO/genai 2026.2.1 + new in M2: sounddevice
  0.5.5, silero-vad 6.2.1, onnxruntime 1.27.0, soxr 1.1.0, pytest 9.1.1.
- `models/whisper-turbo-ov/` (~780MB INT8 export — do NOT re-export) and
  `models/ov_cache/` (~2GB compile cache — keep).
- `.tmp/*.wav` — disposable test captures.
- Mic: 'Headset Microphone (Samsung USB' @ 44.1kHz native. Its level is LOW
  (peak ~0.03–0.05 full-scale) but Whisper is accurate anyway; raising the
  Windows input level is a nice-to-have.

Fresh-session sanity check: `.venv\Scripts\python.exe -m pytest tests -v`
→ 7/7 pass, or `scripts\m2_capture_vad.py --wav .tmp\test_utterance.wav`
→ GPU, ~7.2s trimmed, transcript printed.

## Next: M3 — push-to-talk hotkey + clipboard injection (PRD §9, AT-1/2/3)

New deps: `pynput`, `pyperclip` (add to pyproject, reinstall `-e .[dev]`).

Suggested shape:
1. `localflow/desktop/capture.py`: add a `StreamRecorder` (sd.InputStream with
   a callback accumulating chunks) — push-to-talk needs start()/stop(), not
   fixed duration. Keep the 60s cap (FR-3). Reuse `ensure_16k_mono` after stop.
2. `localflow/desktop/inject.py`: FR-10 exactly — save clipboard, set
   transcript, send Ctrl+V via pynput, **wait ~120ms**, restore clipboard
   (restoring too early corrupts the paste). `inject_mode: paste | type`
   (FR-12, default paste). Empty `raw_text` ⇒ inject NOTHING (FR-4/AT-3).
3. `localflow/desktop/app.py`: wire hotkey (default hold Ctrl+Space, FR-1) →
   StreamRecorder → `transcribe_audio()` → inject. Build the SttEngine + VadGate
   ONCE at startup (FR-14; GPU load 1–3s warm). Log per-utterance device/ms
   (FR-7). Recording indicator (FR-2) can be a console log line for M3; tray
   arrives in M4.
4. Acceptance: AT-1 (Notepad happy path), AT-2 (clipboard PRESERVE_ME survives),
   AT-3 (3s silence ⇒ nothing pasted). Known limits to respect: UIPI — injection
   into elevated/admin apps silently fails (FR-11, document, don't fight it).

Watch items for M3:
- First utterance after launch pays ~0.3–0.7s extra GPU warmup on top of steady
  ~240ms — consider one dummy `generate()` on a short silent/noise buffer at
  startup if it bothers AT-4 later (measure first).
- pynput global hotkey + sounddevice callback = threads; keep the transcribe +
  inject work OFF the key-listener thread (queue or worker thread).

Then M4 (fallback/latency/tray/config, AT-4/5/6). M5 (LLM cleanup) is v1.1.
