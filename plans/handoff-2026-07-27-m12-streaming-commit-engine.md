# Session Handoff — v1.2 M12 streaming commit engine (log-only preview loop)

## Where it started
Fresh session after the M11 handoff. Jeremy said: read
`c:\Users\jemer\code\localflow\plans\handoff-2026-07-27-m11-interaction-shell.md`,
then build M12 of `plans/v1.2-live-typing-plan.md`. M12 is the streaming
commit engine with NO injection: prove cadence, engine contention and commit
stability on real audio, visible only in the log. Ponytail mode (full) active
for the whole session. Nothing else was discussed.

## Important discoveries and decisions
- Word comparison in `commit_prefix` MUST be normalised (lowercase,
  punctuation stripped). Whisper re-punctuates and re-capitalises a word as
  the buffer around it grows ("hello" -> "Hello,"), so strict equality would
  almost never agree twice and nothing would ever commit. This is the one
  non-obvious correctness point in the pure layer.
- `patch_plan` deletes back to the END of the last kept word, so the
  separating space belongs to the retype: `patch_plan("I rode a bike",
  "I wrote a book")` = `(12, " wrote a book")`, not `(11, "wrote a book")`.
  A first-draft test asserted the wrong shape and was corrected, not the code.
- The recorder's buffer IS the uncommitted window: `drain_segment` already
  keeps only the tail after a Tier 1 cut, so the preview needs no window
  bookkeeping of its own beyond knowing a cut happened.
- Ordering trap: a cut segment's authoritative text arrives from the worker
  SECONDS after the cut, by which time the new window may already have
  committed words. Solved by keeping the stream as `_regions` (closed windows,
  oldest first) + `_window` (live), concatenated — the late text fills its own
  region in place and nothing reorders.
- Every mid segment must reconcile, silent ones included: reconciliation is
  FIFO by cut order, so skipping one (empty raw text) would shift every later
  segment onto the wrong window.
- `recorder.recording` alone is NOT "a dictation is running" — Test mic and
  Calibrate also record. `is_dictating` = `recorder.recording and not
  app.mic_test_active`. (The Segmenter has the same latent flaw but never
  fires on a 3s test because segment_seconds is 30.)
- Preview decodes deliberately skip preprocess AND the VAD/hallucination
  filter. Denoise on a growing window every second would eat the cadence this
  milestone exists to prove; the final pass still owns quality.
- Accepted cost, no config gate yet: the final decode on release can wait up
  to one preview decode (~0.6s on a 30s window) for `engine_lock`.

## Decisions locked + what shipped
- `c:\Users\jemer\code\localflow\localflow\core\live.py` (new, pure) —
  `commit_prefix(prev_words, new_words, committed_len) -> int`
  (LocalAgreement-2, normalised comparison, never retreats below
  `committed_len`) and `patch_plan(typed, final) -> (n_backspaces, retype)`
  (word-aligned, for M13's final patch). Both exported from
  `localflow/core/__init__.py`.
- `c:\Users\jemer\code\localflow\localflow\desktop\livetyper.py` (new) —
  `LiveTyper`, Segmenter-shaped: own thread, own 1s poll, reads the recorder
  only, never queues. Tick order: `engine is None` -> return; not
  `is_dictating()` -> `_flush()`; new `current_id()` -> `_reset()`; snapshot;
  under `MIN_WINDOW_S` (1.5s) -> return; resample OUTSIDE the lock;
  `engine_lock.acquire(blocking=False)` -> skip the beat if busy;
  `engine.transcribe(timestamps=False)`; `_commit()`; log.
- Commit state: `_regions` + `_reconciled` pointer + `_window` + `_prev`, all
  under `LiveTyper._lock`. `on_cut(id)` closes the window; `on_segment_text
  (id, text)` appends only the words the preview had not committed
  (count-aligned, carries a `ponytail:` comment naming the ceiling). Every
  event checks the dictation id and drops stale ones.
- End of dictation: `_flush()` logs the stream ONCE (`_flushed` flag) but
  keeps the state until the next dictation's `_reset`, so a segment text still
  in flight lands instead of racing a teardown. `livetyper.text()` is what M13
  will diff the final text against.
- `c:\Users\jemer\code\localflow\localflow\desktop\app.py` — three touch
  points: `self.livetyper = None` in `__init__`; `emit_segment` calls
  `on_cut(dictation_id)` INSIDE the produce lock (a preview tick must never
  scan the drained buffer while holding the pre-cut hypothesis);
  `_handle_segment` calls `on_segment_text(item.dictation_id, seg_text)` for
  every non-final segment (new local `seg_text`, defaults `""`); `_run` builds
  the LiveTyper beside the Segmenter, `startup_worker` sets `.engine` and
  `.start()` after warmup, the teardown `_try(app.livetyper.stop)`.
- Tests — `c:\Users\jemer\code\localflow\tests\test_live.py` (new, 10: first
  decode commits nothing, agreement, divergence, never-retreat,
  punctuation/case insensitivity, and the four `patch_plan` shapes) and 5 new
  LiveTyper tests in `c:\Users\jemer\code\localflow\tests\test_desktop.py`
  under a "M12: streaming commit engine" section (commit cadence via a stub
  engine, busy-lock skip + clean resume, short buffer / stale id, segment-cut
  ordering, flush-once + reset on a new dictation).
- Build log appended to the plan file under "M12 build log (27 Jul 2026) -
  CODE COMPLETE, live DoD pending Jeremy".

## Key files for next session
- Plan file: `c:\Users\jemer\code\localflow\plans\v1.2-live-typing-plan.md` —
  read FIRST. M13 is next and carries the v1.2 live sign-off DoD. The M11 and
  M12 build logs sit inline above the M13 section.
- `c:\Users\jemer\code\localflow\localflow\desktop\livetyper.py` — M13 turns
  the two `log.info("live commit...")` calls into injection. The natural seam
  is an `on_commit(text)` callback deliberately NOT added in M12.
- `c:\Users\jemer\code\localflow\localflow\core\live.py` — `patch_plan` is
  already built and tested for M13's final patch; nothing there should need
  changing.
- `c:\Users\jemer\code\localflow\localflow\desktop\app.py` —
  `_finish_dictation` is where M13's patch runs (it already computes the final
  text and calls `inject_text`); `inject.py`'s `type` mode is the injector to
  reuse, per the plan (do NOT write a second one).
- `c:\Users\jemer\code\localflow\plans\handoff-2026-07-27-m11-interaction-shell.md`
  — the previous session's handoff (suppression, TapHoldGate, Esc cancel).
- Memory files touched: none this session.

## Running state
- Background processes: none
- Dev servers / ports: none
- Open worktrees / branches: main, no worktrees. Nothing committed this
  session — M12 is uncommitted alongside the uncommitted M11 and v1.1 work
  already in the tree.

## Verification — how to confirm things still work
- `c:\Users\jemer\code\localflow\.venv\Scripts\python.exe -m pytest tests -q`
  — 271 pass (baseline before this session was 256).
- `c:\Users\jemer\code\localflow\.venv\Scripts\python.exe -m pytest tests/test_live.py tests/test_desktop.py -q`
  — 94 pass, the fast loop while working on M12/M13.
- `c:\Users\jemer\code\localflow\.venv\Scripts\python.exe -c "import localflow.desktop.app, localflow.desktop.livetyper; print('ok')"`
  — the `_run` wiring imports the LiveTyper lazily, so this is the cheap check
  that nothing in the module graph broke.

## Deferred / pending tasks + open questions
- Pending: M12's LIVE definition of done, which only Jeremy can run. A real
  60s+ dictation whose log (`%LOCALAPPDATA%\LocalFlow\localflow.log`) shows
  `live commit` lines landing 1-3s behind speech, no commit retracted, and
  segment cuts plus the final paste unaffected. Set logging to DEBUG to also
  see per-tick `preview: Xs window decoded in Yms` lines (the contention
  evidence).
- Pending: M11's live DoD is ALSO still owed (keyboard dead while dictating
  and alive after, tap toggles, hold still pastes, Esc leaves nothing).
- Deferred: no `live_typing` config key yet, so the preview loop runs on every
  dictation and burns GPU. M13 owns the toggle; it is the first thing to add
  if live testing goes badly.
- Deferred: `on_commit` injection hook, the final patch, and history recording
  the FINAL text — all M13, all specified in the plan.
- Deferred (unchanged from the plan): Parakeet TDT OpenVINO A/B on the NPU,
  beam-size/decoding knobs, AlignAtt early stopping.
- Open: none. Jeremy asked for a handoff immediately after M12 finished, with
  no questions raised.

## Plan
- `c:\Users\jemer\code\localflow\plans\v1.2-live-typing-plan.md` — M11
  interaction shell (code-complete, live DoD pending), M12 streaming commit
  engine (code-complete, live DoD pending), M13 live injection + final patch +
  `live_typing` config. The v1.2 live sign-off DoD lives in M13.

## Next steps
Build M13, the last v1.2 milestone. Three pieces, in the plan's order: (1)
type newly committed text into the focused app by reusing `inject.py`'s `type`
path (an `on_commit` callback on LiveTyper is the seam; backspaces via
VK_BACK); (2) on stop, run the normal final pass then
`patch_plan(livetyper.text(), final)` -> backspace the divergent suffix and
retype, skipping injection entirely when nothing diverges, and no patch at all
after Esc; (3) `live_typing = true` config (tomlkit, live-appliable, Settings
checkbox) where false bypasses the M11-M13 paths entirely and restores exact
v1.1 behaviour. Follow the tdd skill. Then Jeremy's live sign-off across
Notepad, a browser textbox and VS Code.

## Next step (for the next session after /clear)
Next step: read
`c:\Users\jemer\code\localflow\plans\handoff-2026-07-27-m12-streaming-commit-engine.md`,
then build M13 of `plans/v1.2-live-typing-plan.md`.
