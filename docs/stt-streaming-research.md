# Why Google voice typing feels better, and how to close the gap

Research pass 27 Jul 2026 (3 web-research agents). Question: why is Chrome/Google
voice typing so much better than LocalFlow, and can we match it locally?

## Finding 1: Google's edge is mostly architecture + UX, not raw accuracy

- Google Docs voice typing and Chrome's Web Speech API default to **Google's
  cloud** (Conformer/USM-class models, ~12M hours training audio). Chrome 139+
  can do on-device (SODA) but the default you compared against is the cloud.
- It streams because it uses an **RNN-T transducer**: emits words 100-300ms
  behind your voice as audio arrives. Whisper is encoder-decoder over a padded
  30s window; it structurally cannot emit words mid-utterance.
- The "corrects itself while I speak" effect is **two-pass decoding**: a fast
  streaming first pass paints interim grey text, a second-pass rescorer
  rewrites earlier words when more audio (or an endpoint) arrives. Interim
  instability is a designed feature, not a smarter model.
- Plus: contextual biasing (your contacts etc.), on-device personalization,
  separate punctuation/caps models.
- **Raw accuracy on real conversational speech is a wash**: Whisper large-v3
  ~8-16% WER, Google Chirp 2 ~11.6% on comparable sets. The perceived gulf is
  live feedback + rescoring polish, not word accuracy.
  (Sources: research.google two-pass/RNN-T/USM posts, MDN Web Speech docs,
  Gladia STT comparison, arXiv 2010.11148 FastEmit.)

## Finding 2: no local model both streams natively AND beats Whisper turbo on Intel

Mid-2026 landscape on Arc 140V / NPU / CPU, no CUDA:

| Option | Accuracy | Streams | Intel path |
|---|---|---|---|
| Whisper large-v3-turbo (current) | good (~0.4pt behind large-v3 EN) | no | best-supported (OpenVINO GenAI) |
| **Parakeet TDT 0.6B v2/v3** | beats large-v3 avg on Open ASR leaderboard (~6.1 vs ~7.4) | 10s chunks w/ state, not live partials | **ready-made OpenVINO builds (FluidInference), NPU 22-38x realtime** |
| **sherpa-onnx Zipformer transducer** | Whisper-class on clean speech, less proven noisy | **yes, true token-level partials** | CPU or iGPU via DirectML, Windows fine |
| Kyutai STT / Voxtral Realtime | good, true streaming | yes | no Intel port, CUDA-sized |
| Canary / Granite / ARK-ASR (leaderboard top) | best | no | no Intel port |
| Vosk/Kaldi | behind | yes | trivially, not worth it |

## Finding 3: live self-correcting partials are buildable on the CURRENT stack

- Standard trick: re-decode a growing buffer every ~1s, commit the longest
  prefix that two consecutive decodes agree on (**LocalAgreement-2**,
  ufal/whisper_streaming), show the uncommitted tail as provisional grey.
  whisper.cpp `stream` does the sliding-window variant of exactly this.
- Cost math on our hardware: turbo at ~50x realtime means a 30s buffer
  re-decodes in ~0.6s. Viable if the buffer stays capped, and our Tier-1
  segmenter ALREADY caps it (cut at silence ~30s, drain, reset). VAD + denoise
  overhead stacks on top, so poll ~1s, skip when a decode is still running.
- State of the art is **SimulStreaming / AlignAtt** (UFAL, ~5x cheaper than
  re-decoding, won IWSLT 2025): uses Whisper's cross-attention to stop decoding
  near the live edge. Needs step-wise decoder control that
  `openvino_genai.WhisperPipeline` does not expose. Upgrade path, not step 1.
- `WhisperPipeline` streaming support is short-form only (<30s, no timestamps)
  via StreamerBase; no OpenVINO port of either streaming policy exists. So the
  re-decode+prefix-agreement pattern is OUR code either way, and it reuses the
  existing recorder/VAD/segmenter almost as-is.

## Finding 4: accuracy levers on the current model, cheap to try

- Decoding: beam_size=5 + temperature fallback is the standard max-accuracy
  combo (check what openvino_genai's GenerationConfig exposes).
- condition_on_previous_text helps coherence but causes repetition loops; we
  effectively avoid it already per-segment.
- initial_prompt biasing is confirmed double-edged (can inject hallucinated
  sentences), matches our live finding that multi-word hot_words hurt.
- Denoise-before-Whisper can DEGRADE accuracy (2026 paper, worse on larger
  models); our light spectral gate default is right, don't crank it.
- large-v3 (full) vs turbo on accented speech: no published head-to-head; only
  a personal A/B would settle whether full large-v3 is worth 4x decode time.

## Recommended path (in order)

1. **Tier 2: live preview** on the current stack. During a hold, re-decode the
   uncommitted buffer every ~1s on the iGPU, LocalAgreement-2 prefix commit,
   paint confirmed text solid + tail grey on/above the pill. No new models, no
   new deps; reuses recorder.snapshot, VadGate, segmenter caps. This alone is
   the Google feel.
2. **Parakeet TDT OpenVINO A/B** (FluidInference build, NPU): same audio through
   both engines on a personal test set; if it wins on Jeremy's voice, swap the
   final-pass engine (keep Whisper as fallback). Best accuracy candidate with a
   ready Intel path.
3. Decoding knobs on turbo (beam size, fallback ladder) + optional one-off
   large-v3-full A/B for the final pass only.
4. Later, only if 1 feels laggy: port AlignAtt-style early stopping, or bolt on
   sherpa-onnx Zipformer purely as the preview model.
