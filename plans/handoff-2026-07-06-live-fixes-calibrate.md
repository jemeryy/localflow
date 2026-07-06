# Session Handoff — v1.1 live-fix pass (3 settings crashes) + calibrate auto-tunes audio sliders

## Where it started
Jeremy asked "what do we do next?" against the v1.1 self-drive handoff. Answer at
session start: all M6-M10 code was already done/committed; only live testing +
two decisions remained. Then he started live-testing and hit real crashes, so
this session became a live-fix pass, plus one new feature request: make
"Calibrate room noise" also set the audio-tuning sliders.

## Important discoveries and decisions
- The v1.1 build was code-complete before this session (commit bf85221, 143 tests).
- Three DISTINCT crashes surfaced live, none caught by the existing tests
  because two were native Tcl aborts / PortAudio host errors (no Python
  traceback in the log):
  1. Settings window would not reopen after changing pill size.
  2. Changing pill size + reopening settings crashed the whole app natively.
  3. The app would not start at all (pinned Samsung mic wedged, then its
     index pointed at an OUTPUT endpoint).
- Root cause of the pill-size crashes: pill_size live-apply was tearing down
  and REBUILDING the entire overlay Tk root on a worker thread while the
  settings window sat over it. That mechanism was inherently fragile.
- The Samsung USB-C headset is genuinely flaky at the OS level: it wedges
  (WDM-KS host error -9999) and its device indices shift on reconnect, so a
  raw-index pin (`input_device = 14`) is unreliable. Decision (Jeremy):
  unpin, follow the system default.
- Calibrate auto-tune breakpoints (-55 dBFS quiet -> 0.25/0.42, -35 dBFS
  noisy -> 0.75/0.62) are FIRST-PASS guesses. They need live tuning against
  Jeremy's real room. He live-tested calibrate in his quiet room and it wrote
  denoise 0.25 / vad_threshold 0.42 (confirms the feature works end to end).

## Decisions locked + what shipped
All in ONE commit, `901d6e6` on `main` (project convention: commit straight to
main; ship line = push/tags, NOT done):
- **Settings reopen fix** — `open_settings` now catches `RuntimeError` as well
  as `TclError` on the stale-window check. `c:\Users\jemer\code\localflow\localflow\desktop\settings_ui.py`
- **Pill-size live rescale (replaces root rebuild)** — `overlay.set_pill_size(size)`
  enqueues `("rescale", size)`; the overlay's own thread updates a mutable
  `scale` cell and re-lays-out. `rebuild_overlay` DELETED from app.py. The
  settings window now stays open during a resize.
  `c:\Users\jemer\code\localflow\localflow\desktop\overlay.py`,
  `c:\Users\jemer\code\localflow\localflow\desktop\app.py` (`hook_pill_size`).
- **Startup mic robustness** — (a) `recorder.open_or_fallback()` hunts for any
  working input device when the configured mic won't OPEN, instead of killing
  startup; (b) `StreamRecorder.__init__` / `_resolve_device` falls back to the
  system default when the pinned device won't RESOLVE as an input (e.g. index
  now points at an output endpoint). Only zero usable mics is fatal.
  `c:\Users\jemer\code\localflow\localflow\desktop\capture.py`,
  `c:\Users\jemer\code\localflow\localflow\desktop\app.py` (`open_recorder`).
- **Removed stale pin** — `input_device = 14` deleted from
  `c:\Users\jemer\code\localflow\localflow.toml` (now follows system default).
- **Calibrate auto-tunes the sliders** — pure `core.recommend_tuning(noise_rms)
  -> (denoise, sensitivity)` in
  `c:\Users\jemer\code\localflow\localflow\core\preprocess.py` (exported in
  `core/__init__.py`). `calibrate_noise` measures broadband RMS of the
  high-passed quiet clip and passes `(message, tuning)` to the callback; the
  settings window's `_apply_calibrated_tuning` moves both sliders, flips the
  preset to Custom, and live-applies. `app.py` + `settings_ui.py`.
- **Tests 143 -> 156.** New: settings reopen (test_settings_ui.py), overlay
  rescale command + recorder fallback/construction (test_desktop.py),
  recommend_tuning mapping (test_noise.py).
- Docs updated: `plans/v1.1-plan.md` milestone log (top entry) and
  `c:\Users\jemer\code\localflow\CLAUDE.md` build status (156 tests, pill_size
  rescale not rebuild, startup fallback, calibrate auto-tune).

## Key files for next session
- Plan file: `c:\Users\jemer\code\localflow\plans\v1.1-plan.md` — read the
  milestone log FIRST; the top entry summarizes this session and lists what is
  still deferred.
- `c:\Users\jemer\code\localflow\plans\handoff-2026-07-06-selfdrive-v1.1.md` —
  the prior handoff (self-drive run state); still valid for the M6-M10 build.
- `c:\Users\jemer\code\localflow\localflow\desktop\settings_ui.py` — the
  settings window (calibrate auto-tune, reopen fix live here).
- `c:\Users\jemer\code\localflow\localflow\desktop\overlay.py` — the pill; the
  new `("rescale", size)` command and mutable `scale` cell.
- `c:\Users\jemer\code\localflow\localflow\desktop\capture.py` — mic fallback
  (`open_or_fallback`, resilient `_resolve_device`).
- `c:\Users\jemer\code\localflow\localflow\core\preprocess.py` — `recommend_tuning`.
- Memory files touched: none.

## Running state
- Background processes (run_in_background shells): none.
- Dev servers / ports: none.
- The LocalFlow tray app IS running (launched by this session ~19:55 via
  `Start-Process`, a detached Windows process, not a tracked shell). It is on
  the new 901d6e6 build, GPU, following the system default mic. Log:
  `%LOCALAPPDATA%\LocalFlow\localflow.log`.
  Kill: `Get-Process | Where-Object { $_.ProcessName -match 'localflow|pythonw' } | Stop-Process -Force`
- Open worktrees / branches: none (on `main`, clean after the commit).

## Verification — how to confirm things still work
- `c:\Users\jemer\code\localflow\.venv\Scripts\python.exe -m pytest tests -q` —
  expect 156 passed.
- Relaunch: `Start-Process .venv\Scripts\localflow-tray.exe -WorkingDirectory c:\Users\jemer\code\localflow`
  then `tail` the log — expect "following the system default mic" and
  "hotkey live ... at ~4s" with NO traceback.
- Overlay rescale sanity (spawns Tk windows ~4s, prints PASS):
  `c:\Users\jemer\code\localflow\.venv\Scripts\python.exe "C:\Users\jemer\AppData\Local\Temp\claude\c--Users-jemer-code-localflow\6fcd9148-098b-4e5e-bf88-1b0d2527b853\scratchpad\rescale_check.py"`
  (scratch file; safe to delete).

## Deferred / pending tasks + open questions
- Deferred: **DeepFilterNet spike** (M9) — needs Jeremy's OK to install torch
  before running. Not started.
- Deferred: **preset breakpoint tuning** — the recommend_tuning breakpoints and
  the Quiet/Noisy/Headset preset values are first-pass; tune live against
  Jeremy's real room (quiet + fan noise A/B).
- Deferred: the full **M8/M9/M10 live DoD checklist** (hotkey live-apply, Test
  mic, cleanup toggle from settings, sensitivity A/B, calibration A/B with fan
  noise, dictate->correct->suggestion->accept->re-dictate). Jeremy drives these.
- Open: does Jeremy want the calibrate auto-tune to set BOTH sliders (current
  behavior) or only denoise? He asked for "audio tuning as well"; built both.
  Confirm when he tests it live.

## Plan
- Overarching plan file: `c:\Users\jemer\code\localflow\plans\v1.1-plan.md`
  (M6-M10, all code-complete; this session was a live-fix + small-feature pass
  layered on top). Definition of done per milestone is in that file.

## Next steps
1. Jeremy live-tests on the running 901d6e6 build: change pill size in Settings
   (should resize live, no crash, window stays open), then Calibrate room noise
   (both sliders should move, status shows measured dBFS). Fix anything he
   reports.
2. Tune the calibrate breakpoints + presets against his real room (quiet vs
   fan noise). Adjust `recommend_tuning` in core/preprocess.py and the PRESETS
   in settings_ui.py.
3. If Jeremy approves torch, run the DeepFilterNet spike (M9 plan item 3).
4. Work the remaining M8/M9/M10 live DoD checklist items with him.
5. Nothing is pushed anywhere; ship line intact. Do not push/tag without asking.
