# Session handoff — M1 complete (2 July 2026)

## Where the project stands

**M1 is done and PASSED.** OpenVINO Whisper (`whisper-large-v3-turbo` INT8) runs on
the Intel Arc 140V iGPU at ~240ms steady-state for a 7.4s utterance — ~5x inside the
PRD's STT budget. Risk R1 (the load-bearing assumption) is retired. Full evidence
trail: `docs/m1-results.md`. Nothing is committed to git yet (last commit is the
scaffold, fd47854).

## What exists on disk (uncommitted / gitignored)

- `scripts/m1_prove_igpu.py` — throwaway M1 proof script (device listing, GPU→CPU
  fallback, warmup vs steady timing). Committable as-is if wanted.
- `docs/m1-results.md` — M1 findings + device matrix + implications. Commit this.
- `.venv/` (gitignored) — Python 3.12, OpenVINO/openvino-genai 2026.2.1,
  optimum-intel 2.0.0, torch 2.12.1, transformers 5.0.0, nncf 3.2.0, soundfile.
- `models/whisper-turbo-ov/` (gitignored, ~780MB) — the exported INT8 model. The
  one-time `optimum-cli export` is already done; do NOT re-export.
- `.tmp/test_utterance.wav` (gitignored) — 7.4s TTS test sentence, ground truth:
  "The quick brown fox jumps over the lazy dog. Local flow is a fully local
  dictation tool for Windows."
- `.tmp/ov_cache/` (gitignored) — OpenVINO compile cache. Contains the NPU blob
  (188s cold compile already paid; warm load is ~3s). Keep it.

Quick environment sanity check for a fresh session:
`.venv\Scripts\python.exe scripts\m1_prove_igpu.py` → should print DEVICE USED: GPU,
steady run ~250ms.

## Findings that constrain the next milestones (from docs/m1-results.md)

1. **GPU deterministically drops an unusually quiet trailing word** (TTS trail-off
   artifact; CPU and NPU transcribe it fine; not fixable by f32/normalize/padding;
   beam search Not Implemented on GPU). Watch item, not blocker. → M2 must validate
   last-word fidelity on REAL mic speech early.
2. **VAD trim must keep ~0.3–0.5s trailing padding** after the last speech frame.
3. **CPU fallback works but is ~6s/utterance** — over budget. FR-6 fallback should
   log loudly; consider auto-suggesting the `small` model when device=CPU.
4. **NPU is a fully viable plan-B primary** (correct, ~0.6–0.8s, ~3s load once
   `CACHE_DIR` is set). Set `CACHE_DIR` for every device in the real engine.
5. GPU pipeline load is 3–14s → load once at startup (FR-14), never per utterance.

## Next: M2 — capture + VAD (PRD §9)

Goal: mic capture → resample to 16kHz mono float32 → Silero VAD trim → feed into the
M1 pipeline. This is also when the real `localflow/core/` package gets born (PRD §4
contract) — M1 was throwaway; M2 starts the durable code.

Suggested M2 shape:
1. `localflow/core/` package: `SttEngine` (wraps WhisperPipeline, device fallback,
   CACHE_DIR, timing meta), `VadGate` (silero-vad via onnxruntime, trailing-padding
   rule), `transcribe_audio()` + `TranscriptResult` exactly per PRD §4. Pure: no
   clipboard/keyboard/desktop/network imports (AT-7).
2. `localflow/desktop/` gets only `record_audio()` (sounddevice) for now — no hotkey
   yet (that's M3).
3. New deps to install: `sounddevice`, `silero-vad`, `onnxruntime`, `librosa` (only
   if resampling from non-16k mic rates is needed — check the mic's native rate).
4. Acceptance for M2: a script records N seconds from the mic, VAD-trims, transcribes
   on GPU, prints TranscriptResult fields incl. device + stt_ms. FR-4 silence gate:
   recording pure silence produces NO text (Whisper hallucinates on non-speech —
   Calm-Whisper 99.97%). Test both.
5. Carry the GPU trailing-word check into M2: dictate a sentence with a trailing-off
   last word into the real mic; compare GPU vs NPU output.

Then M3 (hotkey + clipboard injection, AT-1/2/3), M4 (fallback/latency/tray/config,
AT-4/5/6). M5 (LLM cleanup) is v1.1 — not part of v1 ship.

## Open decisions (non-blocking, decide in M2)

- Whether `CACHE_DIR` should live under `models/` or `.tmp/` (currently `.tmp/`,
  which is documented as disposable — moving it to `models/ov_cache` avoids
  accidentally re-paying the 188s NPU compile when `.tmp/` is cleared).
- Mic capture sample rate strategy: capture at device default + resample vs request
  16k directly from sounddevice (some mics/drivers won't honor 16k natively).
