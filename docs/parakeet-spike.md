# Parakeet TDT A/B spike (4 Aug 2026)

**Question:** can Parakeet TDT replace whisper-turbo to kill the decode-loop /
missing-word / invented-word failures at the source?

**Verdict: STRONG CANDIDATE — recommend integrating behind a config flag for a
live A/B.** Whisper stays the default until Jeremy signs the live run off; the
v1.2 tag baseline is untouched (spike ran in an isolated venv, main app never
modified).

## Why this architecture matters

Whisper is an autoregressive text generator loosely conditioned on audio: when
the audio evidence is weak (quiet mic, silence padding) its language-model side
free-runs — that is the looping, the invented "Thank you", and the dropped
trailing words. A transducer (TDT) emits each token locked to a slice of audio
frames: it structurally cannot loop over silence or pad-hallucinate, and its
anti-repeat behaviour needs no filters. We also proved (30 Jul-3 Aug logs,
knob test) that openvino_genai's WhisperPipeline ignores repetition_penalty and
no_repeat_ngram_size, so there is no decode-knob fix on the Whisper side.

## Setup

- Model: `nemo-parakeet-tdt-0.6b-v2` (English, punctuation + capitalisation),
  int8 ONNX (~660 MB) via `onnx-asr[cpu,hub]`, onnxruntime CPU.
- Isolated venv `.tmp/parakeet/venv` (repo convention, like the DFN spike; do
  NOT install into `.venv`).
- Test set `.tmp/parakeet/testset/` (built by `make_testset.py`): clean, quiet
  (0.08 peak), quiet+noise, real mic takes (0.037 peak), sentence said 3x
  (loud + quiet), 20 s trailing silence, near-silence. Scripts:
  `bench_whisper.py` (main venv, GPU, raw decodes) and `bench_parakeet.py`
  (spike venv). Results JSONs beside them.

## Results (raw model output, no VAD / no filters, warm timings)

| case | whisper-turbo (GPU) | parakeet-tdt int8 (CPU) |
|---|---|---|
| clean 7.4s | 1135 ms, DROPPED trailing "Windows." | 1325 ms, perfect |
| quiet 0.08 peak | 806 ms, full text | 920 ms, perfect |
| quiet + noise | 758 ms, dropped "Windows." | 985 ms, perfect |
| real mic take 0.037 peak | 459 ms, "A quick…" (article wrong) | 814 ms, perfect |
| said 3x (loud / quiet) | correct count both | correct count both, 2.1 s |
| +20 s trailing silence | dropped "Windows." | 2620 ms, perfect, no pad junk |
| near-silence 5 s | "." | "" (clean empty) |
| trailing-off take | same text both | same text both |

Whisper dropped the final word in 3 of 4 clean/quiet variants (its known
trailing-word weakness) and none of the set provoked a loop this run (loops
are intermittent; the live logs have them daily). Parakeet made zero errors on
the whole set, including silence and repeat counts.

## Latency and practicalities

- CPU int8 ≈ 120-180 ms per second of audio (7.4 s → ~0.9 s, 23 s → ~2.1 s):
  1.5-2x slower than whisper-on-GPU but well inside the feel budget, and it
  FREES the iGPU — the preview-decode memory pressure ("bad allocation",
  27 Jul) disappears if STT moves to CPU. onnxruntime-openvino (GPU/NPU EP)
  is the follow-up if faster is wanted.
- `model.recognize(numpy_f32, sample_rate=16000)` works → drop-in at the
  `SttEngine.transcribe` seam. First decode after load is cold (~6 s);
  warmup pass needed, same as today.
- English-only (v2); fine for the current `<|en|>` config. v3 = multilingual.
- No timestamps used yet; the chunk-timing hallucination filter is
  Whisper-specific and simply would not be needed.

## Integration (DONE 4 Aug, Jeremy's go — live A/B running)

`stt_model = "whisper" | "parakeet"` in localflow.toml (restart to apply;
code default stays "whisper" = exact v1.2). `localflow/core/parakeet.py`
implements the `transcribe(audio16k) -> (text, meta)` contract (int8 model in
`models/parakeet-tdt-0.6b-v2-onnx`, onnx-asr in the MAIN venv now — pip check
clean, numpy untouched). A parakeet load failure logs and falls back to
whisper, so dictation is never dead. Jeremy's localflow.toml is flipped to
parakeet for the live A/B; sign-off decides the default. Flip back +
tray Restart = the saved baseline.
