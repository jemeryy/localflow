# Session Handoff — v1.2 M13 live injection + final patch, then the first commits in three weeks

## Where it started
Fresh session after the M12 handoff. Jeremy said: read
`c:\Users\jemer\code\localflow\plans\handoff-2026-07-27-m12-streaming-commit-engine.md`,
then build M13 of `plans/v1.2-live-typing-plan.md`. M13 is the last v1.2
milestone: type committed words into the focused app, patch the ending against
the final pass on stop, and add the `live_typing` config toggle. Ponytail mode
(full) active the whole session. After M13 was green Jeremy asked what was
next, then said "commit first" — so the session ended with three weeks of
uncommitted work landed in three commits.

## Important discoveries and decisions
- **The one load-bearing design change: the committed stream had to become
  strictly APPEND-ONLY.** M12 let a Tier 1 cut segment's authoritative text
  fill its region in place while the NEW window had already committed past it.
  That is fine for a log and impossible for typing — you cannot insert text
  into a document you have already typed past. Fix: `LiveTyper._commit` HOLDS
  while `_reconciled < len(_regions)`. `_prev` still advances, so the tick
  after the segment text lands commits immediately. Accepted cost: a ~1s
  typing pause per cut, i.e. every 30s. The M12 ordering test asserted the old
  (now wrong) behaviour and was rewritten.
- Mid-dictation typing must NOT wait for modifiers. The hotkey's Alt is
  physically down for a whole hold, so `_wait_modifiers_released` would stall
  every commit for its full 1.5s timeout. Safe to skip: pynput types
  characters as KEYEVENTF_UNICODE, which ignores modifier state entirely, and
  M11's `clear_modifiers` already told the app Alt is up. The FINAL patch does
  wait, because backspace is a real VK and Alt+Backspace is "undo" in plenty of
  apps.
- A preview decode that started while recording lands SECONDS later. Without a
  close, it would commit and type words AFTER the final patch — unrecoverable
  text. Hence `LiveTyper.close(id)`.
- `close()` deliberately does NOT block `on_segment_text`. Those texts are
  authoritative, they arrive before `_finish_dictation` on the same worker
  thread, and they must be typed to keep `typed_for()` equal to what is
  actually on screen.
- Ask the LiveTyper what was typed, not the `live_typing` flag. That is what
  keeps a mid-dictation toggle honest — the answer is what actually happened,
  not what the setting says now. Chased a wrong design here first (closing the
  stream on toggle-off) and backed it out; leaving the in-flight dictation
  alone is both less code and more predictable.
- Silence path needed a patch too: if the final pass produced nothing but the
  preview had typed something (a hallucinated tail, a VAD-gated window), it is
  backspaced away. Leaving it would put words in the document that no
  dictation ever produced.
- `on_commit` is called INSIDE `LiveTyper._lock`, so what the app receives is
  exactly what `text()` reports, in that order. The joining space rides on the
  FOLLOWING chunk (`"hello there"`, then `" my"`).
- Found while committing: the SessionStart hook in `.claude/settings.json` and
  the `.gitignore` entry both pointed at `docs/NEXT.md`, but the handoff skill
  writes `plans/NEXT.md`. The hook was silently reading a file that never
  existed. Both fixed.

## Decisions locked + what shipped
- `c:\Users\jemer\code\localflow\localflow\desktop\livetyper.py` — `on_commit`
  seam, `close(dictation_id)`, `typed_for(dictation_id)`, `_emit()`, plus the
  append-only hold in `_commit` and `_closed` in `_reset`.
- `c:\Users\jemer\code\localflow\localflow\desktop\inject.py` — new
  `type_text(text)` (no modifier wait) and `patch_text(n_backspaces, retype)`
  (waits, then VK_BACK x n, then types).
- `c:\Users\jemer\code\localflow\localflow\desktop\app.py` — `live_typing`
  attribute + `set_live_typing(on)` (sets the flag AND `_gate.tap_max_s = 0`,
  which is what "no toggle mode" means); `_grab_keyboard` returns early when
  off; `_close_livetyper()` called from `_stop_dictation` and
  `_cancel_dictation` inside `_produce_lock`; `_finish_dictation` gained
  `dictation_id` and the `_typed_text` / `_patch_typed` helpers; `_run` builds
  the LiveTyper with `on_commit=type_text` and an `is_dictating` hook that
  includes `app.live_typing`; `hook_live_typing` + the `live_typing` branch in
  `apply_settings`.
- `c:\Users\jemer\code\localflow\localflow\desktop\config.py` and
  `c:\Users\jemer\code\localflow\localflow.toml` — `live_typing = true`.
- `c:\Users\jemer\code\localflow\localflow\desktop\settings_ui.py` — "Type
  words live while dictating" checkbox at the top of the Behaviour box, plus
  `_COERCERS`, `RESET_ALL_KEYS`, `_read_values`, `_reset_widgets_to`.
- `c:\Users\jemer\code\localflow\tests\test_desktop.py` — module-level
  `import time as _time`, `_livetyper(..., typed=[])`, the rewritten
  append-only cut test, and 7 new M13 tests.
- `c:\Users\jemer\code\localflow\plans\v1.2-live-typing-plan.md` — M13 build
  log appended.
- Three commits on `main` (repo convention: every prior milestone landed
  directly on main, no branch):
  - `e7207b3` chore(agent): SessionStart handoff pointer + the `plans/NEXT.md`
    path fix.
  - `19424e6` docs: v1.2 plan, STT streaming research, DeepFilterNet verdict,
    10 handoffs.
  - `5164d23` feat: 33 files, +4544/-281 — three weeks of work (hallucination
    filter, auto mic gain, TTS read-aloud, boot.py entry point, Tier 1
    segmentation, M11 + M12 + M13). ONE commit on purpose: `app.py`,
    `config.py` and `settings_ui.py` carry every one of those features
    interleaved, so splitting would have meant hunk surgery and intermediate
    commits that do not run.
- Nothing was pushed. Working tree is clean.

## Key files for next session
- Plan file: `c:\Users\jemer\code\localflow\plans\v1.2-live-typing-plan.md` —
  read FIRST. M13's DoD (the live sign-off) is the only thing left in v1.2.
  M11, M12 and M13 build logs sit inline.
- `c:\Users\jemer\code\localflow\localflow\desktop\livetyper.py` — the whole
  streaming + typing engine. The append-only invariant is the thing to
  preserve in any change here.
- `c:\Users\jemer\code\localflow\localflow\desktop\app.py` —
  `_finish_dictation` (the patch), `set_live_typing` (the switch),
  `_close_livetyper` (the ordering guard).
- `c:\Users\jemer\code\localflow\localflow\core\live.py` — pure
  `commit_prefix` + `patch_plan`, unchanged this session.
- `c:\Users\jemer\code\localflow\plans\handoff-2026-07-27-m12-streaming-commit-engine.md`
  — the previous session's handoff (the M12 commit engine's internals).
- Memory files touched: none this session.

## Running state
- Background processes: none
- Dev servers / ports: none
- Open worktrees / branches: `main`, no worktrees. Tree clean, nothing pushed
  (3 commits ahead of whatever the remote had, if any).

## Verification — how to confirm things still work
- `c:\Users\jemer\code\localflow\.venv\Scripts\python.exe -m pytest tests -q`
  — 279 pass (baseline before this session was 271).
- `c:\Users\jemer\code\localflow\.venv\Scripts\python.exe -m pytest tests/test_live.py tests/test_desktop.py -q`
  — 102 pass, the fast loop for anything in the live-typing path.
- `c:\Users\jemer\code\localflow\.venv\Scripts\python.exe -c "import localflow.desktop.app, localflow.desktop.livetyper, localflow.desktop.settings_ui, localflow.desktop.inject; from localflow.desktop.config import Config; print('ok', Config().live_typing)"`
  — prints `ok True`; the cheap check that the module graph and the new config
  key are intact.
- `git -C c:\Users\jemer\code\localflow status --short` — expect empty.

## Deferred / pending tasks + open questions
- Pending, and the whole remaining scope of v1.2: **M13's live sign-off, which
  only Jeremy can run.** Notepad, a browser textbox and VS Code; dictate a
  paragraph in toggle mode (words appear steadily, final patch leaves exactly
  what a v1.1 paste would have); hold mode streams identically and ends on
  release; Esc mid-dictation leaves the typed text and types nothing more;
  physical keyboard suppressed during and alive after, including after a
  forced kill of the tray process.
- Pending: M11's and M12's live DoDs are still owed too, but the same run
  proves both (suppression + tap toggle for M11; `live commit` lines landing
  1-3s behind speech with nothing retracted for M12 — set logging to DEBUG for
  the per-tick `preview: Xs window decoded in Yms` contention evidence).
- Deferred: the Settings checkbox itself has no test — `_SettingsWindow` is
  mocked in `tests/test_settings_ui.py`, so only the pure pieces around it
  (`_COERCERS`, `RESET_ALL_KEYS`, `default_tuning`) are covered. The live
  harness is `scripts/m8_settings_check.py`.
- Deferred (unchanged from the plan): Parakeet TDT OpenVINO A/B on the NPU as
  the final-pass engine, beam-size / decoding-knob pass on turbo plus a
  one-off large-v3-full A/B, AlignAtt-style early stopping if the 1s cadence
  ever feels laggy.
- Known escape hatch if live typing feels bad in practice: `live_typing =
  false` in Settings restores exactly v1.1 (hold-to-talk, one paste on
  release, no suppression, no preview loop), live, no restart.
- Open: none. Jeremy raised no questions this session.

## Plan
- `c:\Users\jemer\code\localflow\plans\v1.2-live-typing-plan.md` — M11
  interaction shell, M12 streaming commit engine, M13 live injection + final
  patch + config. All three are CODE COMPLETE. The plan's remaining content is
  M13's "Definition of done (live sign-off, Jeremy)" section.

## Next steps
Run the app and do the live sign-off. Launch with
`c:\Users\jemer\code\localflow\.venv\Scripts\python.exe -m localflow.desktop.app`
(or the Start Menu "LocalFlow" tray entry; log at
`%LOCALAPPDATA%\LocalFlow\localflow.log`), then walk the M13 DoD list across
Notepad, a browser textbox and VS Code. Fix whatever the live run surfaces —
the likeliest candidates are the ~1s typing pause at each Tier 1 cut feeling
wrong, per-app quirks with unicode injection or backspace, and the final patch
retyping more than expected when the preview and the final pass word the same
audio differently. After sign-off, append the live results to the plan and mark
v1.2 done.

## Next step (for the next session after /clear)
Next step: read
`c:\Users\jemer\code\localflow\plans\handoff-2026-07-27-m13-live-injection-and-first-commits.md`,
then run the v1.2 live sign-off for M11-M13 and fix whatever it surfaces.
