# M2 results — capture + VAD + the real `localflow` package

**Date:** 2 July 2026
**Verdict: PASS on everything automatable.** The durable `localflow/core/` package
now exists per the PRD §4 contract, mic capture and the Silero VAD gate work on
real hardware, and both GPU and NPU transcribe through the new code. Two checks
that need a human voice at the mic remain open (below).

## What was built

```
localflow/
  core/                 # PURE: audio in, text out (the FARA seam, AT-7)
    audio.py            #   ensure_16k_mono() — dtype/downmix/soxr resample
    vad.py              #   VadGate — silero-vad (ONNX, CPU), silence gate + padding
    stt.py              #   SttEngine — WhisperPipeline, CACHE_DIR, device fallback
    pipeline.py         #   transcribe_audio() + TranscriptResult (PRD §4)
    cleanup.py          #   CleanupEngine stub (raises until M5/v1.1)
  desktop/
    capture.py          #   record_audio() — sounddevice, native rate, 60s cap (FR-3)
scripts/m2_capture_vad.py   # acceptance script (mic or --wav, --expect-silence)
tests/test_core.py          # 7 tests incl. GPU integration
pyproject.toml              # editable install; new deps: sounddevice, silero-vad,
                            #   onnxruntime, soxr (chosen over librosa — lighter)
```

## Verification matrix (all autonomous, target machine)

| Check | Result |
|---|---|
| pytest suite (7 tests) | **7/7 pass** in ~13s |
| AT-7 core purity (import `localflow.core` in clean interpreter) | PASS — no sounddevice/pynput/pyperclip/desktop modules leak |
| Test WAV → VAD → GPU | PASS — 7.41s → 7.22s trim, transcript = M1 output, load 1.9s (cache hit) |
| Test WAV → VAD → NPU | PASS — **load 3.6s** (warm cache after move), full transcript incl. trailing word |
| Real mic ambient room (3s, Samsung USB headset @ 44.1kHz) | PASS — captured, soxr 44.1k→16k, VAD gated to no text, `--expect-silence` exit 0 (FR-4 / AT-3 shape) |
| Silence → `transcribe_audio()` | PASS — `raw_text == ""`, stt_ms 0, no hallucination |

Notes:
- First `generate()` on GPU is ~1.0s (warmup); M1 measured ~240ms steady-state.
  The engine loads once and stays resident, so only utterance #1 pays this.
- GPU still drops the unusually quiet TTS trailing word ("…tool for"); NPU gets
  it ("…tool for Windows."). Exactly matches M1 — the known watch item, not a
  regression. VAD trailing pad (0.5s) is in place as designed.

## Decisions made (were open in the handoff)

1. **`CACHE_DIR` moved to `models/ov_cache`** (from `.tmp/ov_cache`, ~2GB moved,
   not recompiled). `.tmp/` is documented-disposable; the NPU blob costs 188s to
   rebuild. Both GPU (1.4–1.9s) and NPU (3.6s) warm loads confirmed from the new
   location. `SttEngine(cache_dir=...)` takes it explicitly; scripts default to
   `models/ov_cache`.
2. **Mic strategy: capture at device native rate, resample in core.** FR-3 says
   exactly this, and it is deterministic across drivers vs. asking WASAPI for
   16kHz. Resampler is **soxr** (small, fast, what librosa wraps anyway) — librosa
   not installed.
3. VAD trailing pad default **0.5s**, leading pad 0.2s (top of the M1-recommended
   range — insurance for the GPU quiet-word behavior at ~8ms STT cost).

## Human-at-the-mic checks — DONE (2 July 2026), M2 fully signed off

1. **Real dictation happy path: PASS.** Multiple 6s captures on the Samsung USB
   headset; GPU transcribed spoken sentences correctly at 650–1000ms per
   utterance (incl. warmup — inside the 1.2s budget).
2. **GPU-vs-NPU trailing-word check: PASS — GPU keeps the trailing word on real
   speech.** A take spoken with a deliberately clear "The" and a quietly
   trailed-off "dog" transcribed *identically* on GPU (650ms) and NPU (1.6s)
   from the same saved WAV — first and last words both present on both devices.
   (An earlier ambiguous run was traced to the spoken audio itself: three
   devices *and* a 25x gain-normalized replay all heard the same words, so the
   engine reports the recording faithfully.)

**Decision:** the M1 trailing-word drop is closed as a synthetic-TTS artifact.
**GPU stays the primary STT device.** No gain normalization needed — Whisper
was accurate even at the mic's very low capture level (peak ~0.03–0.05
full-scale; raising the Windows input level is a nice-to-have, not required).
NPU remains the documented plan-B (warm cache in `models/ov_cache` keeps its
load at ~3s).

## Environment deltas

- New in `.venv`: sounddevice 0.5.5, silero-vad 6.2.1, onnxruntime 1.27.0,
  soxr 1.1.0, pytest 9.1.1, torchaudio 2.11.0 (dragged in by silero-vad; unused
  by our code paths).
- `localflow` installed editable (`pip install -e .[dev]`); scripts and tests
  import it without path hacks.
- Silero VAD weights ship inside the wheel — no runtime downloads (hard
  constraint respected).
