# M5 results — LLM cleanup (S1 / FR-8 / FR-9) + follow-ups

**Date:** 3 July 2026
**Verdict: AT-8 PASS (machine-verified on GPU).** Cleanup is live on
Jeremy's machine, toggleable from the tray. Live dictation feel-check by
Jeremy still to come.

## Same-day follow-ups shipped before M5 (Jeremy's requests)

1. **Hot words (jargon fix):** `hot_words` list in localflow.toml →
   Whisper `initial_prompt` vocabulary biasing (verified supported in this
   openvino_genai; `hotwords` param also exists if we ever want per-word
   boosting). Restart to apply. Caveat: biasing, not a guarantee.
   **REVERTED same day:** with a 5-word list live, Jeremy saw split and
   dropped words — a known initial_prompt side effect. His config now has
   `hot_words = []`; the mechanism stays in code for targeted 1-3 word use.
2. **Default-mic auto-follow:** PortAudio snapshots devices at init, so
   `localflow/desktop/micwatch.py` polls the WASAPI default capture
   endpoint ID via COM (new dep: pycaw) every 2s and calls
   `StreamRecorder.refresh_default_device()` (close → `sd._terminate()` /
   `sd._initialize()` → re-resolve → reopen, guarded by a reopen lock) when
   it changes. Swaps are deferred mid-recording and retried. Only active
   when `input_device` is unset; a pinned mic never switches.

## M5 cleanup design (per PRD S1/FR-8/FR-9)

- **Model:** Qwen2.5-3B-Instruct INT4 (`models/qwen2.5-3b-ov`), exported
  with `optimum-cli export openvino --weight-format int4` from a throwaway
  `.venv-export` (runtime venv stays lean). Same OpenVINO GenAI runtime as
  STT — no Ollama.
- **Mode:** synchronous "slower, cleaner" clean mode (the PRD-blessed v1.1
  path) — when ON, the paste waits ~1-3s for the LLM. Default OFF in the
  template; ON in Jeremy's live config (he asked for it).
- **FR-9 prompt** is fixed: remove filler, fix punctuation/caps/stumbles,
  never paraphrase/add/answer. `validate_cleanup()` guardrails: strip
  wrapping quotes, reject empty output, reject output >2.5x input length
  (model answered instead of edited) — any rejection falls back to the raw
  transcript inside `transcribe_audio` (cleanup can never block a paste).
- **Runtime toggle (FR-14):** tray "Cleanup (slower, cleaner)" checkbox
  flips `app.cleanup_active` live; engine loads once at startup when
  `cleanup_enabled = true` (changing that needs a restart, FR-13).
- Chat mode (`start_chat(system)/generate/finish_chat`) applies the model's
  own chat template — engine code stays model-agnostic.
- Cleanup runs on the worker thread only; warmup does one dummy clean at
  startup. `cleanup_ms` shows in the per-utterance `[ok]` line and log.

## Verification

| Check | Result |
|---|---|
| pytest suite (39: 8 core + 21 desktop + 10 config... by file: core 8, desktop 19, config 12) | **39/39 pass** |
| `validate_cleanup` guardrails (quotes/empty/expansion) | PASS (unit) |
| Mic watcher switch flow (baseline/defer-while-recording/swap-once) | PASS (unit) |
| Hot words live in startup log | PASS |
| Default-mic follow live in startup log | PASS |
| **AT-8: "um so like the meeting is uh on tuesday" → "The meeting is on Tuesday."** | **PASS** — exact expected output, no added content |
| — "...i want to uh i want to refactor the the config module" (stutter) | PASS — "Okay, I want to refactor the config module." |
| — "codex api docs" (jargon + casing) | PASS — "the Codex API docs" |
| Cleanup latency on GPU | 572–1161ms per utterance; model load 23.8s cold / 4.3s with warm ov_cache; app ready in ~9s total |
| Export | Qwen2.5-3B-Instruct INT4 → `models/qwen2.5-3b-ov`, 1.65GB, 12 files; `.venv-export` removed after |

## Paste-race bug found live (FR-10) — fixed

Dictating into a busy app pasted Jeremy's OLD clipboard (a stale shell
command) instead of his words: the app processed Ctrl+V *after* the 120ms
clipboard restore. There is no OS signal for "paste consumed", so:
- settle delay raised 120ms → **500ms**, configurable as
  `restore_delay_ms` (FR-13) — raise it if a slow app ever repeats this;
- restore now SKIPS if the clipboard changed during the settle (a copy the
  user made mid-window is never clobbered);
- regression tests for both behaviours (suite now 41/41).

## Notes / limits

- Cleanup edits what Whisper HEARD. Slurred words Whisper misheard as
  other words stay wrong — hot_words attacks that side; speak-a-touch-
  slower helps more than any model.
- AT-4's latency budget is defined with cleanup OFF (PRD §7) — toggle
  cleanup off in the tray for the AT-4 measurement run.
- Whisper + Qwen both resident on the 8GB iGPU (int8 ~0.8GB + int4 ~2GB) —
  fits with room; watch for GPU memory pressure if models grow.
