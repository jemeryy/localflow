# Session Handoff — LocalFlow live-feedback pass: pill self-heal, "you" hallucination filter, foreign languages, copy-last-text, backgrounded startup + loading bar, instant quit

## Where it started
Jeremy had been using LocalFlow and reported, across two messages: (1) the pill sometimes
disappears; (2) it appends a stray word at the END of the sentence (not always "you",
glued to the real text, not its own sentence); (3) can it detect foreign languages; (4) a
right-click-pill "copy last dictation" for when the paste target wasn't focused. Then:
"make LocalFlow start up faster" -> which became: pill up immediately with a loading bar,
only two visible states (loading vs usable), and quit must close everything at once so a
fast relaunch doesn't clash with the single-instance guard.

## Important discoveries and decisions
- **openvino_genai 2026.2.1 exposes NO `no_speech_threshold`** (introspected the
  WhisperGenerationConfig). So the trailing-hallucination fix cannot use a decode
  threshold. BUT `return_timestamps=True` returns `result.chunks` with `.text/.start_ts/
  .end_ts`, and the fake tail has a clear signature: a few characters spanning many
  seconds and/or starting past the real audio end. Measured on the test WAV: `" Thank
  you."` at 0.3 chars/sec over ~30s, start_ts 8.04 past a 7.22s clip.
- **The timestamp path also RECOVERS the real final word.** The no-timestamp path was
  silently dropping the last word ("...tool for" instead of "...tool for windows.").
  Verified end-to-end on the GPU: filter on -> "...tool for windows." with the "Thank you"
  hallucination removed; filter off -> "...tool for" (drops final word, faster).
- **Cost: timestamps roughly double STT** (test clip: 306ms -> 764ms warm). Tied to the
  `filter_hallucinations` flag (default true); `false` = fast path, no timestamps, no
  filtering.
- **Text-only blocklist can't catch the glued/varying trailing word** (Jeremy's real
  case). The first-pass text filter (`strip_hallucinations`) is kept only as a
  whole-utterance/detached-"You." backstop; the timestamp chunk filter is the real fix.
- **Startup profile (fresh-process import timing):** `silero_vad` import = 2.36s (pulls
  onnxruntime), `scipy.signal` = 1.36s (already lazy, inside preprocess funcs), openvino
  0.38s, numpy 0.22s, sounddevice 0.30s. Engine build ~1.7s (GPU, warm cache).
  Deferring `silero_vad`: `import localflow.core` 2.4s -> 0.18s.
- **`StreamRecorder.start()` self-opens** the stream if needed, so the hotkey being live
  before the background mic-open is safe (first press opens the mic).
- **pystray MenuItem text accepts a callable** and re-evaluates it (verified) -> the tray
  status line can show "model on device" once the background load finishes.
- **Could NOT launch-test the full restructured startup:** Jeremy's tray app (pythonw
  PID seen ~40924) is running on the OLD build; two instances can't coexist (both grab
  the hotkey + double-paste). The full `_run`/tray/quit path needs his restart to verify.

## Decisions locked + what shipped (NOT committed — working tree dirty on `main`)
All changes are on top of the still-uncommitted 6 Jul + Tier 1 work.

- **Trailing hallucination filter (timestamp-based)** —
  `c:\Users\jemer\code\localflow\localflow\core\hallucination.py` (NEW):
  `drop_trailing_hallucinated_chunks(chunks, audio_duration_s)` (peels trailing chunks
  with cps<2.0 over >=2.5s, or start_ts past audio end) + `strip_hallucinations(text)`
  (whole-utterance/detached-"You." backstop). Thresholds: `_HALLUC_MIN_DURATION_S=2.5`,
  `_HALLUC_MAX_CPS=2.0`, `_HALLUC_START_MARGIN_S=0.2`. Exported from `localflow.core`.
  Wired in `...\core\pipeline.py` (`transcribe_audio(..., filter_hallucinations=True)`;
  requests timestamps then chunk-drop + text backstop) and `...\core\stt.py`
  (`transcribe(audio, timestamps=False)` returns `meta["chunks"]`; sets
  `self._config.return_timestamps` per call). App reads `app.filter_hallucinations` per
  segment (live via Settings -> Behaviour "Filter trailing hallucinated words").
- **Foreign languages** — `...\core\stt.py` `to_language_token(code)` ("en"->"<|en|>",
  "auto"/""/None->None, "fr"->"<|fr|>"; tolerates "<|fr|>"). Config `stt_language="en"`
  (`...\desktop\config.py`), RESTART_KEYS in `...\desktop\app.py`, Settings -> Advanced
  "Language" dropdown (`...\desktop\settings_ui.py` `language_rows()` + `_LANGUAGES`).
  Verified live: en forces English, auto detects, fr decodes as French.
- **Copy last dictation** — `app.last_text` set in `_finish_dictation` BEFORE inject
  (survives a failed paste); pill right-click menu gains "Copy last dictation" (top row)
  in `...\desktop\overlay.py`; `overlay.on_copy_last` wired to `copy_last_text()` in
  `_run` (pyperclip.copy + pill flash confirmation via `show_flash`).
- **Pill self-heal + crash-proof tick** — `...\desktop\overlay.py`: `heal_visibility()`
  on a ~2s heartbeat (`_HEAL_TICKS=60`) re-shows an unmapped pill and reclaims the
  topmost band; `tick()` always reschedules even on a draw error.
- **Backgrounded startup + loading bar (M11)** — `...\desktop\app.py` `_run`: mic open +
  engine build + VAD + warmup all moved into `startup_worker` (a background thread);
  app built with `engine=None`/`vad=None`/`preprocess=None` (worker gates on `app.warm`).
  Tray status + settings header are callables reading `status_holder` (filled when the
  engine loads). `overlay.set_progress(frac,label)` + a chip-sized "loading" render
  (`draw_loading`, SAME size as the idle chip — no size jump); only loading -> idle shown.
  Progress: 0.12 mic -> 0.30 model -> 0.60 voice -> 0.82 warming -> 1.0 ready.
  Default-mic watcher moved into `startup_worker` (`watcher_ref`).
- **Instant quit** — `_run` finally: releases hotkey/mic/segmenter/watcher, `logging.
  shutdown()`, then `os._exit(0)` (no overlay-join / GPU-teardown / GC wait). Releases the
  named mutex at once so relaunch never hits "already running".
- **Startup import defer** — `...\core\vad.py`: `import silero_vad` moved from module top
  into `VadGate.__init__` (kept as `self._get_speech_timestamps`).
- **Config/docs** — `...\desktop\config.py` + repo-root `...\localflow.toml`: new
  `stt_language`, `filter_hallucinations` keys with comments. `...\plans\v1.1-plan.md`
  milestone-log TOP entry updated (this whole pass). CLAUDE.md build-status NOT updated
  (held for live sign-off; it still says 182 tests).

## Key files for next session
- Plan file: `c:\Users\jemer\code\localflow\plans\v1.1-plan.md` — read the milestone-log
  TOP entry ("Live-feedback pass ... 10 July 2026") first; it lists every change +
  the live-DoD checklist.
- `c:\Users\jemer\code\localflow\localflow\desktop\app.py` — `_run` restructure:
  `startup_worker`, `status_holder`, `watcher_ref`, `_progress`, the `os._exit(0)`
  finally, `app.last_text`/`copy_last_text`, `hook_filter_hallucinations`, RESTART_KEYS.
- `c:\Users\jemer\code\localflow\localflow\desktop\overlay.py` — `set_progress`,
  `draw_loading` (chip-sized), `heal_visibility`, crash-proof `tick`, "Copy last
  dictation" menu + `show_flash`.
- `c:\Users\jemer\code\localflow\localflow\core\hallucination.py` — the filter.
- `c:\Users\jemer\code\localflow\localflow\core\stt.py` /
  `c:\Users\jemer\code\localflow\localflow\core\pipeline.py` — timestamps + language.
- Prior handoff (context for the uncommitted Tier 1 base):
  `c:\Users\jemer\code\localflow\plans\handoff-2026-07-10-tier1-vad-segmented-streaming.md`
- Memory files touched: none.

## Running state
- Background processes (this agent's `run_in_background`): none.
- Dev servers / ports: none.
- Jeremy's LocalFlow tray app: RUNNING on the OLD build (pythonw, seen ~PID 40924, log at
  `%LOCALAPPDATA%\LocalFlow\localflow.log` showing activity at 12:21). It has the FIRST
  batch of changes (copy-last-text etc.) but NOT the startup restructure or the
  timestamp hallucination fix. Must be QUIT from the tray and relaunched to test this
  build (two instances can't coexist).
- Open worktrees / branches: none (on `main`, dirty).
- Scratchpad smoke scripts (throwaway, safe to delete) under
  `C:\Users\jemer\AppData\Local\Temp\claude\c--Users-jemer-code-localflow\...\scratchpad`:
  overlay_smoke.py, prog_smoke.py, settings_smoke.py, measure.py, imports.py, ts_probe.py,
  halluc_probe.py, e2e.py.

## Verification — how to confirm things still work
- `c:\Users\jemer\code\localflow\.venv\Scripts\python.exe -m pytest tests -q` — expect
  **213 passed** (was 182 at session start; +31 this session across test_hallucination.py
  [NEW], test_core.py, test_config.py, test_desktop.py, test_settings_ui.py).
- Language plumbing (needs model+GPU): build `SttEngine(..., language="fr")` and
  transcribe — decodes as French; "auto" detects; "en" forces English.
- Hallucination filter end-to-end already asserted in
  `tests\test_core.py::test_transcribe_audio_end_to_end_on_gpu` ("windows" kept, "thank
  you" dropped) and `test_filter_off_is_the_fast_path`.
- **Full startup / quit: NOT verifiable without a live restart** (single-instance).
  Quit the running instance from the tray, then:
  `c:\Users\jemer\code\localflow\.venv\Scripts\python.exe -m localflow.desktop.app`

## Deferred / pending tasks + open questions
- **LIVE DoD pending Jeremy (needs restart onto this build):** (1) pill + tray up almost
  immediately with a loading bar that fills to Ready then becomes the idle chip; (2) the
  trailing hallucinated word is gone AND the real last word survives; (3) quit-from-tray
  then relaunch at once never says "already running"; (4) pill returns on its own after a
  full-screen app / lock/unlock; (5) Settings -> Advanced -> Language + Behaviour filter
  toggle work; (6) right-click pill -> Copy last dictation. If STT feels slow,
  `filter_hallucinations = false` is the fast path.
- **Open (Jeremy to decide after testing):** language default English vs auto; whether to
  keep `filter_hallucinations` on by default given the ~2x STT cost; whether the
  timestamp filter still misses a word GLUED inside one chunk (report if any slip
  through mid-sentence — would need word_timestamps, more overhead).
- **Deferred:** CLAUDE.md build-status line still says 182 tests — update to 213 + this
  pass ONLY after live sign-off. Engine build (~1.7s) is now the largest remaining
  pre-usable cost; could shave further but not requested.
- **Not committed.** Suggested commit split when Jeremy is happy (confirm with him, do
  NOT push): (a) 6 Jul long-dictation/reset/DF batch, (b) Tier 1, (c) this live-feedback
  pass. He tests live before any commit.
- Unchanged prior deferrals: real-mic denoise A/B for default `denoise_strength`;
  optional `pip uninstall torch torchaudio` from `.venv`; delete `.tmp\dfspike`.

## Plan
- Overarching plan: `c:\Users\jemer\code\localflow\plans\v1.1-plan.md`. This session is
  the new TOP entry in its Milestone log ("Live-feedback pass ... 10 July 2026"). No
  separate plan file for this pass.

## Next steps
1. Jeremy QUITS the running (old-build) tray instance, relaunches onto this build, and
   walks the 6-point live DoD above. Watch `%LOCALAPPDATA%\LocalFlow\localflow.log` for
   the `startup breakdown: ... UI live at Xs ... usable at Ys` line.
2. Fold back any tuning: hallucination thresholds (`_HALLUC_*` in
   `core/hallucination.py`), progress labels/stages, or `filter_hallucinations` default.
   Re-run `pytest tests -q`.
3. On sign-off: update CLAUDE.md build status (213 tests + this pass), then commit
   (confirm the split above with Jeremy; do NOT push/tag).
