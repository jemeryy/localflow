# Session Handoff — faster live stream shipped; surgical cleanup-to-screen grilled and approved

## Where it started
Jeremy asked why the sliding preview uses 16s and how to make the stream feel
faster. Two latency levers were agreed and built. He then said "let's work on the
gemma cleanup" — the parked surgical cleanup-to-screen design — which was
self-grilled to a 10-decision design he approved ("we continue with the cleanup
plan next session"). Tag `baseline-3` = this known-good state.

## Important discoveries and decisions
- Felt lag is two decode beats (poll + decode, twice for LocalAgreement-2), not
  the window size. PREVIEW_MAX_S 16 pairs with segment_seconds 15 on purpose:
  silence cuts must drain the window before the slice-jump backstop fires.
  Lowering segment 15 -> 12 buys fractions of a second, costs more cut stalls
  and seams — rejected.
- Jeremy's verdict on the faster stream: "feels a bit better, not instant but
  good enough".
- test_transcribe_audio_end_to_end_on_gpu decoding to EMPTY was environmental:
  iGPU (shared RAM) squeezed by the long-running tray instance. Fails clean-tree
  too; passes after tray restart. Nothing to fix.
- Keystroke patching cannot deliver cleanup-to-screen: tight matching rewrites
  the whole line (caps fix at word 1), loose matching skips mid-text fixes.
  That is exactly why Jeremy parked it. Surgical span edits are the design.
- comtypes 1.4.16 is ALREADY in .venv — UIA needs no new dependency. The
  backspace+retype machinery (`_patch_typed`/`patch_plan`/`patch_text`) is
  alive (silence take-back uses it) — do not rebuild it.
- Chromium only exposes UIA text editing once accessibility is activated —
  the ONE load-bearing unknown; spike before building.

## Decisions locked + what shipped
- Faster stream (commit a8e5cf9, tag `baseline-3`): POLL_INTERVAL_S 0.7 -> 0.4
  (half-wall-clock rule still caps engine duty at 50%), and aged-word commit —
  `edge_guard` param on `commit_prefix` in
  `c:\Users\jemer\code\localflow\localflow\core\live.py`; words older than the
  newest EDGE_GUARD_WORDS = 5 (~2s of speech, set in
  `c:\Users\jemer\code\localflow\localflow\desktop\livetyper.py`) type after ONE
  decode; the newest 5 still need two agreeing looks. 360 tests pass.
- Surgical cleanup-to-screen design (grilled, approved by Jeremy):
  1. On release, the live-typed screen text morphs into the cleaned final text.
  2. Gated by the existing tray cleanup toggle — NO new config key.
  3. Phase 1 = on-release only (0.5-2s after); mid-hold per-segment cleaning is
     phase 2, only after phase 1 feels good live. (Assumed OK — was a (?))
  4. Surgical span edits (select changed words, replace in place, restore
     caret), never suffix backspace+retype.
  5. Backends: classic edit controls via EM_SETSEL/EM_REPLACESEL; Chromium/
     VS Code/rest via UIA TextPattern (comtypes).
  6. No backend usable -> do nothing on screen; history/Copy-last keep the
     cleaned text. (Assumed OK — was a (?))
  7. Guard: verify the typed text still sits at the target before editing
     (user may have typed meanwhile); mismatch = skip. validate_cleanup stays.
  8. Pure `edit_plan(typed, final) -> char spans` in core.live with tests;
     backends in the desktop layer.
  9. Build order: UIA spike FIRST (GO/NO-GO in Chrome text box + VS Code +
     Notepad) before any feature code.
  10. Esc / dead target / paste-mode paths unchanged. This reverses the 27 Jul
     no-revision rule for LLM fixes only (already sanctioned in the 9 Aug
     handoff).

## Key files for next session
- `c:\Users\jemer\code\localflow\localflow\desktop\app.py` — worker release path
  (~line 880): cleanup runs on the joined text, `_typed_text`/`_patch_typed`
  show where the screen-edit hook belongs.
- `c:\Users\jemer\code\localflow\localflow\desktop\inject.py` — target-locked
  posting, `_keydown_deletes`, `patch_text`; the new backends live beside these.
- `c:\Users\jemer\code\localflow\localflow\core\live.py` — `patch_plan` (word
  alignment to reuse for `edit_plan`), new `edge_guard` in `commit_prefix`.
- `c:\Users\jemer\code\localflow\localflow\core\cleanup.py` — few-shot prompt +
  `validate_cleanup` guard.
- Plan file: none written — the 10 decisions above ARE the plan.
- Memory files touched: none.

## Running state
- Background processes: none.
- Dev servers / ports: none.
- Open worktrees / branches: none — everything on `main`, clean tree, tags
  `baseline-3` (= HEAD a8e5cf9), `baseline-2`, `v1.2`.

## Verification — how to confirm things still work
- `cd c:\Users\jemer\code\localflow; .venv\Scripts\python.exe -m pytest tests -q`
  — 360 passed. If test_transcribe_audio_end_to_end_on_gpu decodes empty,
  restart the tray first (iGPU memory squeeze, not a code bug).
- Dictate a long hold: words should appear ~1-2s sooner than before; no
  duplicates at slice jumps or cuts.

## Deferred / pending tasks + open questions
- Deferred/pending: STILL uncollected from the 9 Aug handoff — Jeremy's live
  verdicts on digits-as-numbers, Gemma cleanup quality (tray toggle), and the
  pill Restart row. Only the stream-speed verdict came in ("good enough").
- Deferred: mid-hold per-segment cleanup edits (phase 2) — after phase 1 lands.
- Open: none blocking. The two grill (?)s were assumed yes (phase-1-first,
  do-nothing fallback); Jeremy can overturn next session.

## Plan
- No plan file; the "Decisions locked" list above is the build plan for
  surgical cleanup-to-screen. Overarching thread: Parakeet-era polish — stream
  latency (done), cleanup to the screen (spike next), then maybe mid-hold edits.

## Next steps
1. UIA spike: throwaway `scripts/uia_edit_spike.py` — select+replace a word
   span via UIA TextPattern in a Chrome text box, VS Code, and Notepad; restore
   the caret; GO/NO-GO verdict (watch: Chromium accessibility activation).
2. If GO: pure `core.live.edit_plan` + tests, then the two backends in
   inject.py, then the worker hook behind the cleanup toggle.
3. Collect the outstanding live verdicts (digits, Gemma quality, pill Restart).

## Next step (for the next session after /clear)
Next step: read c:\Users\jemer\code\localflow\plans\handoff-2026-08-09-faster-stream-cleanup-grill.md, then run the UIA select+replace spike (Chrome/VS Code/Notepad, GO/NO-GO) and continue the cleanup-to-screen build.
