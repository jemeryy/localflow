# Session Handoff — Fixed the triple-typed phrases after segment cuts + idle keep-warm

## Where it started
Jeremy reported his last dictation "hallucinated" repeated phrases, and a dictation
did not stream and took ~6s after release. Diagnosed both from
`%LOCALAPPDATA%\LocalFlow\localflow.log` (11 Aug entries, 11:56 / 12:04 / 14:03 /
14:04 dictations), then fixed both on request.

## Important discoveries and decisions
- The repeats were NOT STT hallucination. Two distinct duplication mechanisms in
  `LiveTyper`, both triggered when a Tier 1 segment cut lands BEHIND the typed
  frontier (continuous speech pushes the last qualifying silence far back, e.g. cut
  at 5.6s of a 15s buffer while the preview had typed through ~15s of audio):
  1. **In-flight race** (3 of the 4 observed dups): a preview decode already running
     when `on_cut` fired landed AFTER the cut reset `_typed` to 0, so `_revise`
     treated the stale pre-cut hypothesis as the new window's first decode and
     re-typed the whole old window.
  2. **Spill** (the 14:04 dup): words the preview typed whose audio lies PAST the cut
     point are absent from the cut window's authoritative segment text, so they were
     never accounted to the next window — its first decode re-heard and re-typed all
     of them. The existing `_OVERLAP_MAX_WORDS = 8` guard in `_append` cannot catch
     it: overlaps were 10-20 words, and the guard needs an exact head-vs-tail align.
- The aged-word one-decode commit from 9 Aug (`a8e5cf9`) exposed both: the preview
  now types close to the live edge, so any far-back cut guarantees typed spill.
- The slow non-streaming dictation (14:03, first after 17 min idle) was Windows
  paging the model out: VAD 4.3s (normally <1s), STT 2092ms (normally 300-900ms), no
  preview decode finished in time to stream. Self-recovered 30s later.

## Decisions locked + what shipped
- **Race fix** — `_revise` returns early while `_pending_counts` is non-empty: a
  decode landing while a cut awaits its authoritative text is by definition stale
  (`_tick` never starts a decode while pending). One guard in
  `c:\Users\jemer\code\localflow\localflow\desktop\livetyper.py`.
- **Spill fix** — new pure helper `spill_count(screen, newer)` in
  `c:\Users\jemer\code\localflow\localflow\core\live.py`: trailing typed words of a
  cut window unmatched in its segment text. `on_segment_text` (non-final, empty
  tail) credits that spill forward — to `_pending_counts[0]` if another cut is
  already pending, else to `_typed` — so the next window anchors on the spilled
  words by content and never re-types them. Also sets `_slice_jumped = True` so a
  content miss on the next beat DROPS instead of re-typing (Jeremy 27 Jul: no
  duplicates; dropping is the cheaper mistake).
- **Keep-warm** — `keep_warm_loop` in
  `c:\Users\jemer\code\localflow\localflow\desktop\app.py` (right after
  `auto_calibrate_loop`): every 5 min, if the mic has been idle ≥5 min, runs a 1s
  silent decode (`engine.transcribe` + `vad.trim`) under a NON-BLOCKING
  `engine_lock` so the model and Silero stay paged in. Always on, no config key
  (deliberate: harmless and there is no reason to turn it off).
- Tests first (TDD): both livetyper tests reproduced the exact live log failures
  before the fix. Suite went 326 -> 365 passing (some growth predates this session).
- Committed as this session's feat/fix + docs commits on `main` (not pushed).

## Key files for next session
- `c:\Users\jemer\code\localflow\localflow\desktop\livetyper.py` — both fixes live
  here (`_revise` pending guard, `on_segment_text` spill credit).
- `c:\Users\jemer\code\localflow\localflow\core\live.py` — `spill_count` helper.
- `c:\Users\jemer\code\localflow\localflow\desktop\app.py` — `keep_warm_loop`
  (~line 1814, after `auto_calibrate_loop`).
- `c:\Users\jemer\code\localflow\tests\test_desktop.py` —
  `test_livetyper_decode_racing_a_cut_never_types`,
  `test_livetyper_words_typed_past_the_cut_are_not_retyped`.
- `c:\Users\jemer\code\localflow\tests\test_live.py` — `spill_count` unit tests.
- Prior handoff (previous session's thread, still relevant):
  `c:\Users\jemer\code\localflow\plans\handoff-2026-08-09-faster-stream-cleanup-grill.md`

## Running state
- Background processes: none
- Dev servers / ports: none
- Open worktrees / branches: none (all on `main`)

## Verification — how to confirm things still work
- `.venv\Scripts\python.exe -m pytest tests -q` — 365 passed.
- Live check (needs Jeremy): restart the tray (pill right-click -> Restart), then
  hold the hotkey and talk CONTINUOUSLY for 30+ seconds without pausing — the log
  should show "segment cut ... transcribing while still held" and the screen must
  contain no repeated phrase. Previously this triple-typed.
- Keep-warm check: leave the app idle 20+ min, then dictate — words should stream
  within ~2s and the log's stt/VAD times should stay in the normal range (stt under
  ~1s, VAD under ~1s), not 2-4s.

## Deferred / pending tasks + open questions
- Deferred: the 9 Aug handoff's next step is still open — UIA select+replace spike
  (Chrome/VS Code/Notepad, GO/NO-GO), then edit_plan + backends for
  cleanup-to-screen.
- Open: none for this session's work; the fixes need Jeremy's live confirmation
  (tray restart required — the running instance still has the old code).

## Plan
- No plan file for this session (bug fix + hardening, straight from log diagnosis).
- The standing v-next plan remains the cleanup-to-screen grill outcome in
  `c:\Users\jemer\code\localflow\plans\handoff-2026-08-09-faster-stream-cleanup-grill.md`.

## Next steps
1. Jeremy restarts the tray and live-verifies: long continuous dictation (no
   repeats) + first dictation after a long idle (fast, streams).
2. Resume the 9 Aug thread: UIA select+replace spike, then cleanup-to-screen
   edit_plan + backends.

## Next step (for the next session after /clear)
Next step: read c:\Users\jemer\code\localflow\plans\handoff-2026-08-11-cut-duplication-keepwarm.md, then get Jeremy's live sign-off on the cut-duplication + keep-warm fixes (tray restart first), then run the UIA select+replace spike from the 9 Aug handoff.
