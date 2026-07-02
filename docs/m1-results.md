# M1 results — OpenVINO Whisper on the Arc 140V iGPU

**Date:** 2 July 2026
**Verdict: PASS.** Risk R1 is retired. `whisper-large-v3-turbo` (INT8, exported via
`optimum-cli`) compiles and runs on the Intel Arc 140V iGPU through
`openvino_genai.WhisperPipeline`, and it is fast enough for the PRD latency budget
with a wide margin.

Proof script: `scripts/m1_prove_igpu.py` (throwaway). Test audio: 7.4s sentence
generated locally with Windows SAPI TTS at 16kHz mono — ground truth known, no
network involved.

## Environment

- OpenVINO / openvino-genai 2026.2.1, optimum-intel 2.0.0, Python 3.12.10 in `.venv`
- Devices visible to `ov.Core()`: CPU (Core Ultra 7 256V), GPU (Arc 140V 8GB iGPU), NPU (AI Boost)
- Model export (one-time, ~780MB on disk):
  `optimum-cli export openvino --model openai/whisper-large-v3-turbo --weight-format int8 models/whisper-turbo-ov`

## Device matrix (7.4s utterance, 3 runs each, greedy decode)

| Device | Steady STT | Load/compile | Transcript quality |
|---|---|---|---|
| **GPU** (target) | **~240ms** | ~3–14s | Drops one quiet trailing word (see below), otherwise perfect |
| CPU | ~5.8–6.1s | ~2s | Perfect, incl. the quiet trailing word |
| NPU | ~580–820ms | **188s cold / ~3s cached** | Perfect, incl. the quiet trailing word |

GPU at ~240ms for 7.4s audio is well inside the PRD's 0.4–1.2s STT budget (G4).
CPU at ~6s blows the budget — it is a *works-at-all* fallback, not a fast one; if CPU
fallback triggers in practice, switch config to `small`/`base` per PRD §7.

## Finding: GPU deterministically drops a quiet trailing word

The TTS test sentence ends with a trailing-off "…for Windows." whose final word is
about half the RMS of the rest of the speech. On GPU the transcript ends "…dictation
tool for" — the quiet last word is dropped, identically across every run. CPU and NPU
both transcribe it correctly from the same model files.

Ruled out by experiment:

- **Not missing audio** — energy analysis + duration diff (7.41s vs 7.00s without the
  word) prove the word is in the WAV; GPU transcribes the word fine in isolation.
- **Not trailing-silence truncation** — padding 0.5s of silence changes nothing.
- **Not FP16 accumulation** — `INFERENCE_PRECISION_HINT='f32'` on GPU still drops it
  (and is 10x slower, ~2.7s; do not use).
- **Not amplitude clipping headroom** — normalizing to 0.95 peak still drops it.
- **Not decoder randomness** — greedy decode, bit-identical output across runs.
- **Beam search is unavailable** on the GPU Whisper path (`num_beams=5` →
  `RuntimeError: Not Implemented`), so it can't be used as a mitigation.

Conclusion: a systematic numerical difference in the GPU plugin's kernels nudges a
borderline-quiet word under the decoder's emission threshold. Known-class issue, not
a blocker: the repro needs an unusually quiet trail-off (synthetic TTS artifact).

**Watch item for M2/M3:** validate against real mic dictation, where trail-offs also
happen. If real speech shows last-word drops on GPU: options are (a) NPU as primary
STT device, (b) light gain normalization after VAD trim, (c) accept and document.
Do NOT let VAD trim right up to the last speech frame — keep ~0.3–0.5s of trailing
context after the trim (helps the decoder regardless of device).

## NPU notes (plan-B device, R1)

- Accurate and fast enough (~0.6–0.8s steady for 7.4s audio) — a viable primary if
  the GPU quirk turns out to matter on real speech.
- Cold compile is brutal: **188s** at pipeline construction (one-time).
- With `CACHE_DIR='.tmp/ov_cache'` set, warm-cache load is **~3s** — same as GPU.
  The 3-minute cost is paid once per model/driver version, then NPU is fully viable.

## Implications carried forward

1. **Primary device = GPU** as planned. Latency margin is huge (240ms vs 1.2s budget).
2. **Startup model load must be async/once** (FR-14 already says load once at startup;
   GPU compile is 3–14s, fine).
3. **CPU fallback works but is slow** — pair the FR-6 fallback with a log warning and
   consider auto-suggesting the `small` model when device=CPU.
4. **Set `CACHE_DIR`** in the real engine for all devices — free load-time win.
5. **VAD trim must leave trailing padding** (M2 design constraint from the GPU finding).
