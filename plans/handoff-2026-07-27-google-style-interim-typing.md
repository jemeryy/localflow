# Session Handoff — live typing rebuilt Google-style (interim results, revised in place)

## Where it started
Fresh session after the M13 handoff. The task was the v1.2 live sign-off, but
Jeremy's first live run surfaced three failures: streaming was slow and bursty,
typing continued AFTER he stopped dictating, and Whisper's "You." silence
hallucination got typed mid-dictation. There was also a crash (MemoryError
spiral). He then asked to research how Google does streaming STT and copy the
feel exactly. Ponytail mode (full) active.

## Important discoveries and decisions
- **The crash root cause (from `%LOCALAPPDATA%\LocalFlow\localflow.log` +
  `crash.log`):** a preview decode hit GPU "bad allocation" (shared-memory
  iGPU under RAM pressure, ~1.7GB free), the engine's fallback then built a
  SECOND Whisper pipeline while still holding the failed one, CPU build died
  the same way, and numpy later failed a 1.64MB allocation. Machine-level
  memory exhaustion, not a logic bug.
- **Google's model (researched, confirmed):** streaming STT emits interim
  results immediately (`is_final=false`), the UI shows them at once and
  revises them in place; the final result replaces them once. The OLD
  LocalFlow engine did the opposite (LocalAgreement-2: wait for two decodes to
  agree, append-only, never revise) — that delay was the slowness, and the
  append-only constraint forced the ~1s pause per Tier 1 cut.
- **The whole append-only design was replaced with type-now/revise-later.**
  Since we can backspace, typed text is NOT immutable. This deleted the
  hold-on-cut pause, the segment-text word-count alignment hack, and my (same
  day, previous session-half) async word pacer queue — the pacer was what kept
  typing after the dictation ended.
- **Revisions must ignore punctuation/case drift.** Whisper re-punctuates
  already-typed words on every decode ("hello" -> "Hello,"); honouring that
  rewrites the line once a second. Hence `patch_plan(loose=True)`: prefix
  matched on letters only, typed form kept, BUT the keep-point walks back over
  trailing words whose form still differs, so "friend" -> "friend." is fixed
  cheaply at the cursor. Accepted loss: a stale mid-text comma/capital stays
  until the final patch, and the final patch (also loose now) leaves
  punctuation-only mid-text drift permanently. Trade chosen for calm typing.
- **"You." fix is a reuse:** the preview now runs the SAME
  `core.strip_hallucinations` the final pass uses, on every hypothesis before
  typing. Whole-utterance stock phrases become "" (typed nothing).
- **Live edge rule:** the newest word of a hypothesis is held until two
  consecutive decodes agree on the whole hypothesis; everything older types
  immediately. On a Tier 1 cut the held word types at once (its audio is
  final).
- **Mid-dictation backspaces are safe under suppression:** injected events
  carry LLKHF_INJECTED and pass the M11 suppression hook (`app.py`
  `_INJECTED_FLAGS`), and M11's `clear_modifiers` already told the app Alt is
  up. `live_patch` therefore does NOT wait for modifiers. Watch hold-mode in
  odd apps for Alt+Backspace=undo misfires — tap-toggle mode has zero risk.
- **Memory-pressure hardening:** `SttEngine.transcribe` gained
  `rebuild_on_failure` (previews pass False — a preview must never trigger the
  ~15s CPU rebuild or demote the device); the rebuild path now drops the dead
  pipeline + `gc.collect()` BEFORE building the replacement; the livetyper
  loop caps preview decoding at 50% wall clock and backs off 3x after a failed
  tick.
- Preview cadence: `POLL_INTERVAL_S` 1.0 -> 0.7. `live_patch` types
  word-by-word with an 80ms gap (`LIVE_WORD_GAP_S`), synchronous on the
  livetyper thread, so nothing can ever type after `close()`.
- Late `on_segment_text` after `close()` is now SKIPPED (reversed from M13's
  "segment texts always land"): the final patch fixes everything in one pass,
  and `_screen` must stay exactly what is on screen. One correction at the
  end instead of two.

## Decisions locked + what shipped
- `c:\Users\jemer\code\localflow\localflow\desktop\livetyper.py` — full
  rewrite: `_screen` (exact on-screen text), `_revise` (edge hold + type now),
  `_apply` (loose patch vs desired = regions + window), `on_cut` freezes the
  hypothesis as provisional region text, `on_segment_text` REPLACES it,
  `on_patch(n_back, text)` seam replaces `on_commit`. Guard: one empty decode
  never erases typed words.
- `c:\Users\jemer\code\localflow\localflow\core\live.py` — `patch_plan(...,
  loose=False)` param: loose prefix by `_norm`, walk-back over trailing
  casefold-different words. `commit_prefix` still exported but no longer used
  by the livetyper (left in core, tested).
- `c:\Users\jemer\code\localflow\localflow\core\stt.py` — `rebuild_on_failure`
  param; drop-then-gc before fallback rebuild; `import gc`.
- `c:\Users\jemer\code\localflow\localflow\desktop\inject.py` — `_TypePacer`,
  `type_text`, `drain_typing` DELETED; new `live_patch(n_backspaces, text)`
  (no modifier wait, 80ms word gap); `patch_text` unchanged except the drain
  call removed.
- `c:\Users\jemer\code\localflow\localflow\desktop\app.py` — imports
  `live_patch` (not `type_text`), LiveTyper wired `on_patch=live_patch`,
  `_patch_typed` uses `loose=True`.
- Tests: `c:\Users\jemer\code\localflow\tests\test_desktop.py` livetyper block
  rewritten to the new contract (interim typing, edge hold, in-place revision,
  hallucination strip, empty-decode guard, close semantics incl. late segment
  text skipped); pacer test deleted; `c:\Users\jemer\code\localflow\tests\test_live.py`
  gained 4 loose-mode tests + case/punct tests from the first half of the
  session.
- NOT committed — working tree carries all of this uncommitted (plus the
  handoff doc for the previous session).

## Key files for next session
- Plan file: `c:\Users\jemer\code\localflow\plans\v1.2-live-typing-plan.md` —
  M13's live sign-off is still the only thing left in v1.2. NOTE: the plan's
  M12/M13 build logs describe the OLD append-only committed-stream design;
  this session's rewrite supersedes them (update the plan when v1.2 is signed
  off).
- `c:\Users\jemer\code\localflow\localflow\desktop\livetyper.py` — the new
  engine; the invariant to preserve is now "`_screen` == exactly what is on
  the app's screen", not append-only.
- `c:\Users\jemer\code\localflow\localflow\core\live.py` — loose patch_plan;
  read the docstring before touching revision behaviour.
- `c:\Users\jemer\code\localflow\plans\handoff-2026-07-27-m13-live-injection-and-first-commits.md`
  — previous handoff (M13 design + the three commits), still accurate except
  where this doc reverses it (append-only gone, close now blocks late segment
  texts, type_text gone).
- Memory files touched: none.

## Running state
- Background processes: none started by this session that still run. A stale
  background pytest from earlier (task bjwcpe8wk) was reported stopped — dead,
  ignore it.
- Dev servers / ports: none.
- The LocalFlow tray app IS RUNNING with the new code (PID 21684 at launch
  time, process name `localflow-tray`). Kill:
  `Get-Process localflow-tray | Stop-Process -Force`. Log at
  `%LOCALAPPDATA%\LocalFlow\localflow.log` (look for "live type" lines).
- Open worktrees / branches: `main`, no worktrees. Uncommitted changes: 6 code
  and test files listed above + 2 handoff docs.

## Verification — how to confirm things still work
- `c:\Users\jemer\code\localflow\.venv\Scripts\python.exe -m pytest tests -q`
  — 288 collected; 286 pass with 2 deselected. The two GPU end-to-end tests
  (`tests/test_core.py::test_transcribe_audio_end_to_end_on_gpu` and
  `::test_filter_off_is_the_fast_path`) FAIL ONLY under machine RAM pressure
  (<2GB free, OpenVINO "bad allocation") — rerun with memory free before
  concluding anything.
- `c:\Users\jemer\code\localflow\.venv\Scripts\python.exe -m pytest tests/test_live.py tests/test_desktop.py -q`
  — fast loop, 111 pass.
- Live smoke: dictate into Notepad; words should appear ~1-1.5s behind speech,
  one at a time; occasional in-place backspace+reword is CORRECT (interim
  revision); nothing may type after release/Esc beyond the single final patch.

## Deferred / pending tasks + open questions
- Pending (the whole remaining v1.2 scope): **the live sign-off, Jeremy only**
  — Notepad, browser textbox, VS Code; hold + tap modes; Esc; suppression on
  during, keyboard alive after. Was attempted this session and failed on the
  three issues fixed above; needs a fresh run.
- Pending: commit this session's work once the live run looks right.
- Pending: update `plans/v1.2-live-typing-plan.md` M12/M13 sections to the
  interim-results design after sign-off.
- Watch: machine RAM. 1.0-1.7GB free during this session; the GPU crash and
  the two test failures were both allocation failures. Not a LocalFlow bug;
  close Chrome/VS Code before long dictations.
- Watch: hold-mode mid-dictation backspaces in apps where Alt+Backspace is
  undo (see discoveries) — if seen live, the fix direction is queueing
  revisions until a modifier check passes, only in hold mode.
- Deferred (unchanged): Parakeet TDT A/B on NPU, turbo decoding-knob pass,
  Settings checkbox has no direct test.
- Open: none.

## Plan
- `c:\Users\jemer\code\localflow\plans\v1.2-live-typing-plan.md` — M11-M13 all
  code-complete; only the live sign-off remains, now on the rebuilt
  interim-results engine.

## Next steps
Run the live sign-off: tray app is already running (or relaunch
`c:\Users\jemer\code\localflow\.venv\Scripts\python.exe -m localflow.desktop.app`).
Dictate across Notepad, a browser textbox and VS Code in tap and hold modes;
verify the stream feels immediate, revisions are calm, nothing types after
stop, no "You.". Fix what surfaces. Then commit everything and update the
v1.2 plan's milestone log.

## Next step (for the next session after /clear)
Next step: read
`c:\Users\jemer\code\localflow\plans\handoff-2026-07-27-google-style-interim-typing.md`,
then rerun the v1.2 live sign-off on the new interim-typing engine and fix
what it surfaces; commit once it holds.
