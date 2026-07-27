# Session Handoff — STT deep research + v1.2 live-typing plan (no code changes)

## Where it started
Jeremy asked why Google browser speech-to-text (Chrome/Docs voice typing) feels
so much better than LocalFlow — it streams in real time and corrects words as he
speaks — and whether LocalFlow can match it. He asked for deep research with
subagents (explicitly on lesser models, Sonnet, not Fable). Research led to
grilling, which led to a locked v1.2 plan. No code was written or changed.

## Important discoveries and decisions
- Google's edge is architecture + UX, NOT raw accuracy: cloud RNN-T/Conformer
  streams words 100-300ms behind voice, two-pass rescoring rewrites earlier
  words (the "self-correcting" effect). On real conversational speech Whisper
  large-v3 and Google's cloud model sit in the same ~8-16% WER band.
- No local model both truly streams AND beats Whisper turbo on Intel today.
  Parakeet TDT 0.6B has ready OpenVINO builds (FluidInference) that beat
  large-v3 on leaderboard average and run 22-38x realtime on the NPU, but its
  streaming is 10s chunks — final-pass accuracy candidate only. sherpa-onnx
  Zipformer is the only true-streaming Intel-viable option (DirectML/CPU),
  clean-speech accuracy roughly Whisper-class.
- Practical streaming route on our exact stack: re-decode the growing buffer
  every ~1s on the iGPU (turbo ~50x realtime = ~0.6s for 30s buffer), commit
  the prefix two consecutive decodes agree on (LocalAgreement-2). SimulStreaming
  /AlignAtt is the smarter upgrade but needs decoder internals openvino_genai
  does not expose. openvino_genai WhisperPipeline has no usable streaming mode.
- Denoise-before-Whisper can DEGRADE accuracy (2026 paper) — keep the spectral
  gate light. initial_prompt biasing confirmed double-edged (matches the live
  hot_words finding). All research with sources:
  `c:\Users\jemer\code\localflow\docs\stt-streaming-research.md`.

## Decisions locked + what shipped (docs only)
- Grilled and locked v1.2 (Tier 2) with Jeremy, all decisions in the plan:
  live-TYPE committed words into the focused app (~1-2s behind voice), NOT an
  overlay preview; both tap-toggle and hold stream; keyboard fully suppressed
  while dictating (except alt+` stop, Esc cancel) + synthetic modifier clear at
  start (this is what makes mid-hold injection clean); committed-words-only
  typing (no provisional churn); on stop run the normal final pass and patch
  the divergent suffix (backspace + retype); Esc = cancel, no patch, typed
  stays; `live_typing = true` default, false = exactly v1.1 behavior; focus
  changes mid-dictation follow focus (known behavior).
- Shipped: `c:\Users\jemer\code\localflow\docs\stt-streaming-research.md`
  (research synthesis) and
  `c:\Users\jemer\code\localflow\plans\v1.2-live-typing-plan.md` (M11-M13).
- Session feedback saved to memory: research/deep-research subagents run on
  Sonnet, not Fable (Jeremy stopped 3 Fable agents mid-run over this).

## Key files for next session
- Plan file: `c:\Users\jemer\code\localflow\plans\v1.2-live-typing-plan.md` —
  read FIRST; M11-M13 with locked decisions, constraints, DoD per milestone.
- `c:\Users\jemer\code\localflow\docs\stt-streaming-research.md` — research
  behind the plan; deferred follow-ups (Parakeet A/B, decoding knobs) live here.
- `c:\Users\jemer\code\localflow\localflow\desktop\app.py` — hotkey handlers,
  `_produce_lock`/`engine_lock`/worker accumulation M11-M12 must respect.
- `c:\Users\jemer\code\localflow\localflow\desktop\segmenter.py` — the pattern
  the M12 preview loop thread copies (private VAD, snapshot-only reads).
- Memory files touched:
  `C:\Users\jemer\.claude\projects\c--Users-jemer-code-localflow\memory\research-agents-use-sonnet.md`

## Running state
- Background processes: none (3 research subagents all completed)
- Dev servers / ports: none
- Open worktrees / branches: main, no worktrees

## Verification — how to confirm things still work
- `c:\Users\jemer\code\localflow\.venv\Scripts\python.exe -m pytest tests -q`
  — 182 pass (unchanged; this session touched no code, docs/plans only)

## Deferred / pending tasks + open questions
- Deferred: Parakeet TDT OpenVINO A/B on NPU as final-pass engine — needs a
  personal test set first (research doc, finding 2).
- Deferred: beam-size/decoding knobs on turbo; one-off large-v3-full A/B.
- Deferred: AlignAtt-style early stopping — only if ~1s cadence feels laggy.
- Open: none. Plan is locked, Jeremy said "ok write it up" and it is written.

## Plan
- `c:\Users\jemer\code\localflow\plans\v1.2-live-typing-plan.md`: M11
  interaction shell (toggle + keyboard suppression, still paste-on-release),
  M12 streaming commit engine log-only (pure core/live.py + preview loop),
  M13 live injection + final patch + `live_typing` config. Live sign-off DoD
  in M13.

## Next steps
Build M11 per the plan (tap/hold state machine, suppression hook, Esc cancel,
modifier clearing; TDD the pure timing logic). Then M12, then M13. Self-drive
works: the plan has per-milestone DoD.

## Next step (for the next session after /clear)
Next step: read c:\Users\jemer\code\localflow\plans\handoff-2026-07-27-stt-research-v12-plan.md, then build M11 of plans/v1.2-live-typing-plan.md.
