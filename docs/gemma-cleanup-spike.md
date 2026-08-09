# Gemma cleanup spike — 9 Aug 2026

**Question:** can a cleanup LLM fix context-obvious mishears (Parakeet writes
"cloud" for "Claude") without a replacement dictionary, and is a small Gemma
better than the shipped Qwen2.5-3B?

**Verdict: ADOPTED — Gemma 3 4B int4 (`models/gemma-3-4b-ov`) is the new
cleanup model, with a rewritten few-shot prompt.** Config default and
`localflow.toml` updated; Qwen stays on disk as the fallback
(`cleanup_model_dir = "models/qwen2.5-3b-ov"` to revert).

## A/B (7 cases from live history: cloud->Claude x3, genuine cloud,
## filler, question-that-must-not-be-answered, digits passthrough)

| Model | Size | Latency (GPU) | Score | Failure modes |
|---|---|---|---|---|
| Qwen2.5-3B int4 (old prompt) | 1.65GB | 0.6-8.1s | 2/7 | answered the question, over-generated twice (guardrail caught), missed every cloud fix |
| Gemma 3 1B int4, old-style prompt | 691MB | 0.15-1.7s | unusable | regurgitated the jargon list, answered, paraphrased |
| Gemma 3 1B int4, few-shot prompt | 691MB | 0.2-1.8s | 6/7 | conservative: left "cloud extension" unfixed |
| **Gemma 3 4B int4, few-shot prompt** | **2.3GB** | **0.5-2s** | **7/7** | none observed |

## What the spike taught

- **Few-shot examples are the whole game for small models.** A bare system
  instruction produced garbage from both Qwen and Gemma. Five worked
  examples (filler fix, cloud->Claude, cloud.md->CLAUDE.md, a question left
  as a question, a genuine "cloud" left alone) taught edit-don't-answer and
  the context gate. The prompt lives in `core/cleanup.py` (`build_prompt`);
  jargon comes from the `cleanup_terms` config list.
- **The context gate works.** "Ask cloud to review the diff" -> Claude;
  "in the cloud on AWS Lambda" -> untouched. This is the no-dictionary
  cloud fix Jeremy asked for (blanket replacement rejected 9 Aug).
- **gemma-3-4b-it is multimodal**; optimum only exports it as
  image-text-to-text. Fix: safetensors key surgery (drop `vision_tower.*` /
  `multi_modal_projector.*`, strip the `language_model.` prefix, rebuild
  config.json from `text_config`) then a normal text-generation export.
  Script pattern preserved in this doc's git history; ~10 min end to end.
- **huggingface_hub transfers were broken on this network** (0-byte stalls,
  xet and plain HTTP both). Direct `curl.exe` on the resolve URLs ran at
  ~80MB/s. Use curl for future model pulls.
- 1B is the fallback if 4B ever feels heavy: 691MB, ~3x faster, only
  conservative misses (`models/gemma-3-1b-ov`, kept on disk).

## Known limit (unchanged design)

Cleanup output still does NOT reach the screen on live-typed dictations —
no correction pass exists by design (Jeremy 27 Jul). Cleaned text shapes
history and "Copy last dictation". It lands on screen only in paste mode
(`live_typing = false`).
