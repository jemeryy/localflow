# Session Handoff — Trailing "Thank you." + doubled-final-word filters, and arming a crash catcher for the native _ctypes crash

## Where it started
Jeremy reported live dictation problems: LocalFlow crashes after a while, sometimes doesn't register input, appends "Thank you.", duplicates the last word (both as a new sentence and within a sentence), adds phrases he didn't say ("This is a very good phrase"), and drops sentences. Then he asked to raise his headset mic volume. Then it crashed again while he right-clicked the pill and clicked "Unlock". This built on two prior uncommitted handoffs (input-overflow/timestamp-token fix on 11 Jul, auto-gain/repetition-collapse on 13 Jul).

## Important discoveries and decisions
- **The "Thank you." and duplication symptoms are the fixable ones; the invented/dropped phrases are a mic-SNR problem.** From `%LOCALAPPDATA%\LocalFlow\localflow.log`: clean, correct decodes on the Intel array in the morning (peaks 0.23–0.52); garbage clusters AFTER 13:40 when the default mic switched to the Samsung USB headset (peaks 0.07–0.17). At those levels normalize amplifies hiss and Whisper hallucinates. Code can't fix low capture level.
- **The Samsung mic has NO software headroom.** Read live via pycaw: it is already at 100% and its driver's `GetVolumeRange` is -60 dB to **0 dB** — no boost, no gain left. That's why there's no Microphone Boost slider and why auto-gain went silent. The only levers are: use the Intel array as input (split: Samsung for output, array for input), move the inline mic closer, or accept it. Recommended the Intel array + recalibrate room noise on it.
- **The crashes are a NATIVE access violation Python cannot catch.** Windows Error Reporting (Event Log, Application, Id 1000) shows pythonw.exe faulting in `_ctypes.pyd`, exception `0xc0000005`, fault offset `0x79b7`, IDENTICAL across all occurrences (12:35, 14:20, 21:36 on 13 Jul). It fired while idle, right after a dictation, AND during the menu/unlock click — so the unlock click was likely a bystander, not the cause; a constantly-running background thread is the suspect. The three earlier crashes on 6/10 Jul were a DIFFERENT signature (`tcl86t.dll`, the known tkinter-teardown issue).
- **This afternoon's COM-lock fix did NOT stop it** (21:36 crash happened with it live), so the shared COM lock, while still a reasonable hardening, was not the root cause. WER dumps were already auto-deleted; nothing left to analyze. Hence: arm a crash catcher so the NEXT crash self-diagnoses.
- `_already_running()` uses a Win32 mutex via ctypes; `micgain`/`micwatch` use pycaw COM via comtypes (which sits on `_ctypes`). The fault is in that comtypes/ctypes layer.

## Decisions locked + what shipped
All uncommitted, on `main` (working tree already dirty from prior sessions). App runs from editable install; restart picks up edits.

- **Peel a trailing "Thank you." sentence** — `c:\Users\jemer\code\localflow\localflow\core\hallucination.py`. Renamed `_TRAILING_YOU` -> `_TRAILING_STOCK` (now matches `thank you` OR `you` after a sentence break). Added `_DETACHED_THANKS = re.compile(r"\s+Thank\s+you\b[\s.!?]*$")` — case-SENSITIVE on purpose: capital "Thank you" glued on with no break is Whisper (e.g. "give me the overview Thank you."), lowercase "Like thank you." is real speech and is kept. Both applied in `strip_hallucinations`'s peel loop. **Trade-off Jeremy accepted:** a genuine detached closing "Thank you." after other speech now gets eaten (it was appended to nearly every dictation on the quiet mic, so the old keep-it stance flipped).
- **Collapse a doubled FINAL word** — same file, new `_drop_trailing_double(text)`: if the last two tokens match (case/punct-insensitive) it deletes the second-to-last, keeping the last (it carries closing punctuation). Handles "standardized standardized.", "one Task. Task.", "things things.". Only the very end; mid-sentence "had had" is kept. Called in `strip_hallucinations` after the peel loop.
- **COM serialization (hardening, not the root fix)** — `c:\Users\jemer\code\localflow\localflow\desktop\micgain.py` added module-level `COM_LOCK = threading.Lock()`; `adjust_for_peak` now holds it around all pycaw calls. `c:\Users\jemer\code\localflow\localflow\desktop\micwatch.py` imports `COM_LOCK` and holds it in `_default_endpoint_id`. So the 2s micwatch poll and the post-dictation auto-gain never touch COM concurrently.
- **auto-gain level cache** — `micgain.py`: `_cached_scalar`/`_cached_at` + `_CACHE_TTL_S=900`; `adjust_for_peak` short-circuits with NO COM call when the cached level would produce no change (the common case now that the slider sits at 100%). `invalidate_cache()` added and wired into the mic-switch callback in `app.py` (`on_mic_switch` calls `micgain.invalidate_cache()` then `hook_preprocess()`), because the cached level belongs to one endpoint.
- **faulthandler crash catcher** — `c:\Users\jemer\code\localflow\localflow\desktop\app.py` `main()`: module-level `_crash_log` (kept alive so its fd isn't reused), opens `%LOCALAPPDATA%\LocalFlow\crash.log` (append), writes a start banner, calls `faulthandler.enable(_crash_log, all_threads=True)`, logs "crash handler armed". This can't PREVENT a native crash but dumps every thread's Python stack the instant it faults — turning the next crash into a named code path.

## Key files for next session
- `c:\Users\jemer\code\localflow\localflow\core\hallucination.py` — `_TRAILING_STOCK`, `_DETACHED_THANKS`, `_drop_trailing_double`; the trailing-text logic.
- `c:\Users\jemer\code\localflow\localflow\desktop\micgain.py` — `COM_LOCK`, the level cache, `invalidate_cache`.
- `c:\Users\jemer\code\localflow\localflow\desktop\micwatch.py` — holds `COM_LOCK` in `_default_endpoint_id`.
- `c:\Users\jemer\code\localflow\localflow\desktop\app.py` — faulthandler arming in `main()`; `on_mic_switch` in the watcher setup (~line 1388).
- `c:\Users\jemer\code\localflow\localflow\desktop\overlay.py` — the pill/menu/unlock code (PressArbiter, `drag_ui`, `menu_items`, `on_release` unlock toggle ~line 651); read if the crash turns out to be in the unlock path after all.
- Log (read FIRST for any live report): `C:\Users\jemer\AppData\Local\LocalFlow\localflow.log`.
- **NEW crash log to check after any crash:** `C:\Users\jemer\AppData\Local\LocalFlow\crash.log`.
- Tests: `c:\Users\jemer\code\localflow\tests\test_hallucination.py` (new trailing-thankyou + doubled-word cases), `c:\Users\jemer\code\localflow\tests\test_micgain.py` (cache no-op + invalidate cases).
- Prior handoffs this builds on: `c:\Users\jemer\code\localflow\plans\handoff-2026-07-13-bugfix-autogain-repetition.md` and `...\handoff-2026-07-11-input-overflow-garbage-decodes-timestamp-tokens.md`.

## Running state
- Background processes: none.
- Dev servers / ports: none.
- Open worktrees / branches: none (on `main`, dirty, uncommitted).
- Jeremy's LocalFlow tray app: was RUNNING and crashed at 21:36; last restart loaded config at 19:24 so it is running PRE-faulthandler code. **Needs a restart to arm the crash catcher and pick up the two text-filter fixes.**

## Verification — how to confirm things still work
- `c:\Users\jemer\code\localflow\.venv\Scripts\python.exe -m pytest tests -q` — expect **233 passed** (was 227 at session start; +6 from new hallucination + micgain cases).
- After restart, `localflow.log` should show "crash handler armed -> ...crash.log" near startup, and `crash.log` should have a "--- LocalFlow start ... ---" banner.
- Live: dictations should no longer paste a trailing "Thank you." and doubled final words should collapse (already observed clean post-fix in tonight's log tail).

## Deferred / pending tasks + open questions
- **Open — root-cause the native crash.** Not fixed, only instrumented. Next crash: read `C:\Users\jemer\AppData\Local\LocalFlow\crash.log`, find the faulting thread's Python stack, fix THAT. Suspect a background thread (micwatch COM poll, or overlay/tk), not the unlock click.
- **Optional — Jeremy may enable WER LocalDumps** (needs admin; the `HKLM:\...\Windows Error Reporting\LocalDumps\pythonw.exe` key). One-shot elevated PowerShell command was given in chat to set DumpFolder=`C:\Users\jemer\AppData\Local\LocalFlow\dumps`, DumpCount=5, DumpType=1. He had not run it as of handoff.
- **Mic SNR (the invented/dropped phrases).** Not code-fixable. Recommended: use Intel array as input (Samsung for output only), or move the inline mic closer, then recalibrate room noise on the active mic. Awaiting Jeremy's choice.
- **Known remaining gap:** a stuttered tiny tail like "Task. Task. Thank you." can still lose the real word when chunk timing is bogus (`drop_trailing_hallucinated_chunks`); left alone because tightening it risks re-leaking garbage.
- **Commit** — everything still uncommitted; Jeremy signs off live before any commit; do NOT push. When he's happy, fold into the existing v1.1 live-fix batch.
- **Deferred (unchanged):** CLAUDE.md build-status line says 182 tests (really 233) — update on sign-off. auto-gain has no Settings UI toggle. `pip uninstall torch torchaudio`; delete `.tmp\dfspike`.

## Plan
- No dedicated plan file drove this session (live debugging). Standing plan: `c:\Users\jemer\code\localflow\plans\v1.1-plan.md`. This session's fixes go in its milestone log on sign-off.

## Next steps
1. Jeremy restarts LocalFlow (quit tray, relaunch) — arms faulthandler, loads the two text filters. Confirm "crash handler armed" in `localflow.log`.
2. Decide the mic: switch input to the Intel array (recommended) and recalibrate room noise, OR stay on the Samsung and accept lower accuracy.
3. When it next crashes, read `crash.log`, identify the faulting thread's Python frame, and fix the actual root cause (the COM lock + faulthandler were the setup for exactly this).
4. On live sign-off: commit the batch (confirm split with Jeremy, do NOT push), update v1.1 milestone log + CLAUDE.md test count (233).
