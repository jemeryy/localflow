# Session Handoff — long-dictation cap + settings reset controls + DeepFilterNet spike; Tier 1 (VAD-segmented streaming) is NEXT

## Where it started
Jeremy (against the prior live-fix handoff) asked four things: why does recording
stop after a while, add a way to reset pill size, add a "reset all" button beside
Close, and try DeepFilterNet. Then he asked how Wispr Flow / other tools handle
indefinite talking and which A/B option we had deferred. He decided the next work
is: **Tier 1 (VAD-segmented pipelined transcription), then a real-mic A/B on the
existing spectral denoise** — but wanted this handoff written first.

## Important discoveries and decisions
- **The 60s stop was the FR-3 cap** (`MAX_UTTERANCE_S`), not a bug. Empirically
  OpenVINO genai Whisper transcribes ARBITRARILY long audio in one `generate()`
  call (chunks + stitches internally) at ~50x realtime on the Arc GPU (60s audio
  -> 1.1s stt; verified via scratchpad `ov_longform_test.py`). So Jeremy's
  "record ahead and stitch the text" idea is unnecessary for correctness — OV
  already stitches. Shipped the simpler fix (configurable cap) instead.
- **Long-buffer full-pipeline latency** grows with length: a 90s buffer through
  preprocess+VAD+STT took ~14s total, of which STT was only 5.6s — the VAD +
  spectral denoise over the whole buffer dominate. Short dictations unaffected.
  This is why Tier 1 (below) is worth building: it bounds the end-of-dictation
  wait to one segment.
- **DeepFilterNet spike = NOT adopted.** It builds+runs fully locally but the
  quality edge is thin and CPU latency isn't free. Full write-up +
  numbers in `c:\Users\jemer\code\localflow\docs\deepfilternet-spike.md`.
  - Latency ~270ms per 5s utterance on CPU (RTF 0.05), over the plan's <=150ms
    gate but within the ~1200ms paste budget for short clips.
  - Quality on synthetic 0/5/10 dB noise: DF fixed a leading-article error
    ("The" for "A") at every SNR that the spectral gate (0.6 AND 0.9) never
    fixed (WER 0.00 vs 0.11) — but Whisper is already so noise-robust the
    headroom is tiny. Not "clearly better".
  - Build path (no MSVC on this machine): installed Rust GNU toolchain
    (`~/.cargo`, `stable-x86_64-pc-windows-gnu` 1.96.1) + linked against the
    existing WinLibs mingw64 gcc. DF lives in an ISOLATED venv `.tmp/dfspike`
    (torch 2.2.2+cpu / torchaudio 2.2.2 / deepfilternet 0.5.6).
  - **Trap learned:** `pip install deepfilternet` into the MAIN `.venv`
    downgraded numpy to 1.26 and broke openvino/scipy. Reverted — main venv is
    now numpy 2.4.6, `pip check` clean, no DF packages. NEVER install DF into
    `.venv`.
- **Research on indefinite talking** (sources in the chat): three techniques —
  VAD-segmented cut-at-silence (WhisperX), chunked/striding long-form (what OV
  already does internally), and LocalAgreement streaming (Whisper-Streaming/Ufal
  for live word-by-word). Wispr Flow is cloud streaming + an LLM formatting pass
  (SOC2/HIPAA-ready = audio leaves the device) — the architecture localflow
  rejects. We copy the technique (local VAD segmentation), not their pipeline.

## Decisions locked + what shipped (this session, NOT committed yet)
- **Configurable long dictation.** New config `max_utterance_s` (default 300.0 =
  5 min, `0` = unlimited), live-appliable. `c:\Users\jemer\code\localflow\localflow\desktop\config.py`
  (field + validator 0-3600 + DEFAULT_CONFIG_TOML). `capture.py`: `_max_frames`
  is `None` when `max_seconds<=0` (no cap in `_on_chunk`), plus a
  `set_max_seconds()` live setter; module `MAX_UTTERANCE_S` default now 300.0.
  `app.py`: recorder built with `max_seconds=cfg.max_utterance_s`, new
  `hook_max_utterance` in `live_hooks`, dispatched in `apply_settings` for key
  `max_utterance_s`. `settings_ui.py`: "Max recording length (s)" spinbox in the
  Behaviour section, read in `_read_values`, coercer added.
  `c:\Users\jemer\code\localflow\localflow.toml` got the `max_utterance_s = 300`
  line for discoverability.
- **Reset pill size** — a "Reset" button next to the pill-size slider snaps it to
  1.0 (deferred to Apply). `settings_ui._on_reset_pill_size`.
- **Reset all** — button beside Close. Confirm dialog, restores the tuning +
  behaviour keys to Config defaults and resets pill position; LEAVES dictionary /
  history / calibration alone. `settings_ui.py`: `RESET_ALL_KEYS`,
  `default_tuning()`, `_on_reset_all` / `_reset_widgets_to` / `_reset_all_done`.
- **Tests 156 -> 163.** New: capture unlimited + `set_max_seconds`
  (test_desktop.py), config `max_utterance_s` default/validation (test_config.py),
  coerce max_utterance + `default_tuning` x3 (test_settings_ui.py), apply-dispatch
  extended for `max_utterance`.
- **Docs:** `plans/v1.1-plan.md` milestone log (new top entry),
  `c:\Users\jemer\code\localflow\CLAUDE.md` build status (163 tests, long
  dictation, resets, DF verdict), `docs/deepfilternet-spike.md` (new).

## Key files for next session
- Plan file: `c:\Users\jemer\code\localflow\plans\v1.1-plan.md` — read the
  milestone log top entry first.
- `c:\Users\jemer\code\localflow\docs\deepfilternet-spike.md` — DF verdict +
  repro (only if revisiting DF).
- `c:\Users\jemer\code\localflow\localflow\desktop\capture.py` — `StreamRecorder`;
  Tier 1 adds segment-draining here.
- `c:\Users\jemer\code\localflow\localflow\desktop\app.py` — `LocalFlowApp`
  (`on_hotkey_down/up`, `_drain`, `_process`, `engine_lock`); Tier 1 changes the
  worker to accumulate ordered segment texts and join once on release.
- `c:\Users\jemer\code\localflow\localflow\core\vad.py` — `VadGate` (Silero);
  Tier 1's silence-boundary finder builds on this.
- `c:\Users\jemer\code\localflow\localflow\core\pipeline.py` — `transcribe_audio`.
- Memory files touched: none.

## Running state
- Background processes: none. (This session ran 4 background shells — rust
  download `bz4bshthm`, rust install `bambighme`, DF build `b2yv7256i`, spike
  venv `bj7c8vt9u` — ALL completed, nothing left running.)
- Dev servers / ports: none.
- The LocalFlow tray app: a prior session launched it (~19:55) on the OLD
  901d6e6 build; it does NOT have this session's changes. To live-test the new
  features it must be restarted (command below).
- Open worktrees / branches: none (on `main`, working tree DIRTY — see below).

## Verification — how to confirm things still work
- `c:\Users\jemer\code\localflow\.venv\Scripts\python.exe -m pytest tests -q` —
  expect 163 passed.
- `.venv\Scripts\python.exe -m pip check` — expect "No broken requirements"
  (numpy 2.4.6; confirms the DF-install regression is fully reverted).
- Long-buffer pipeline sanity (spawns the engine, ~15s): scratchpad
  `longbuf_pipeline.py` prints "LONGBUF PASS".
- Settings-window smoke (builds the window + exercises both resets headlessly):
  scratchpad `settings_smoke.py` prints "SMOKE PASS".
- Scratchpad dir:
  `C:\Users\jemer\AppData\Local\Temp\claude\c--Users-jemer-code-localflow\e641bf0f-20b6-4638-9fed-cbce9cb228ae\scratchpad`
  (ov_longform_test.py, settings_smoke.py, longbuf_pipeline.py, df_*.py). Also
  copied to `.tmp/dfspike/` for the DF ones. Safe to delete.

## Deferred / pending tasks + open questions
- **Uncommitted.** 10 modified + 1 new file (`docs/deepfilternet-spike.md`) on
  `main`, per Jeremy's live-test-first workflow. Suggested commit when he's happy:
  `git add -A && git commit -m "feat: configurable long dictation cap + settings reset controls; DeepFilterNet spike (not adopted)"`
- **LIVE DoD pending Jeremy** (needs an app restart onto this build): hold the
  hotkey past 60s -> full text pastes; set Max recording length to 0 -> no cap;
  Settings pill-size Reset -> Apply -> normal size; Settings Reset all -> confirm
  -> everything to defaults, dictionary/history kept.
- Deferred: real-mic A/B on the spectral denoise (M9 item 4) — record a fixed
  sentence quiet + with fan/keyboard noise across denoise_strength 0/0.3/0.6/0.9
  and the sensitivity slider, transcribe, compare WER, lock the best default.
  Jeremy wants this AFTER Tier 1.
- Optional cleanup: pre-existing `torch 2.12 / torchaudio 2.11` sit unused in the
  MAIN `.venv` from an earlier abandoned attempt; `pip uninstall torch torchaudio`
  from `.venv` slims it (~2GB). Left alone unasked.
- The `.tmp/dfspike` venv (~1GB, gitignored) can be deleted, or kept to run the
  DF real-room A/B later.

## Plan
- Overarching plan: `c:\Users\jemer\code\localflow\plans\v1.1-plan.md`. Tier 1 is
  NEW scope beyond M6-M10 (the long-dictation UX upgrade); fold it into the
  milestone log when built. No separate plan file yet — create one if Tier 1
  grows.

## Next steps
1. **Build Tier 1: VAD-segmented, pipelined transcription** (Jeremy approved;
   complexity = medium, risk is that it touches the dictation core). Design agreed
   in-session:
   - While the key is held, a background segmenter watches the buffer; once it
     passes ~20-30s it uses the Silero VAD (`core/vad.py`) to find a recent
     SILENCE, cuts the segment there, and enqueues it to the worker to transcribe
     DURING the hold. Cutting at silence = NO overlap-stitching needed.
   - `StreamRecorder` gains a `drain_segment(up_to_frame)`-style method: return
     audio up to the cut, keep the remainder as the next segment's start.
   - Worker accumulates ordered segment texts for the current dictation; on
     key-up it flushes the final (short) segment, joins all texts, then runs
     cleanup + dictionary + inject ONCE on the joined text, and writes ONE history
     entry. End-latency is bounded to ~one segment, not the whole buffer.
   - Keep a small PURE "find cut point" function for unit tests. Dictations
     shorter than the segment threshold must behave EXACTLY as today (single
     segment on release). Serialize the engine via the existing `app.engine_lock`.
     Edge case: no silence found in a long monologue -> hard-cut at the segment
     cap (accept a rare mid-word split) rather than growing unbounded.
   - Add tests (cut-point finder, recorder drain, multi-segment join) and keep
     the full suite green.
2. **Then the real-mic spectral-denoise A/B** (deferred item above) to set the
   default `denoise_strength`.
3. Do NOT push/tag. Commit to `main` is the project convention but Jeremy tests
   live first — confirm before committing.
