# Self-drive run state — LocalFlow v1.1 (M6-M10)

Living handoff for the self-drive loop on `plans/v1.1-plan.md`. Rewritten after
every milestone so a fresh session can resume with nothing lost.

## Grounding (discovered once, reused every step)
- Test / gate command: `.venv\Scripts\python.exe -m pytest tests -q`
- Run command (real app): `Start-Process .venv\Scripts\localflow-tray.exe
  -WorkingDirectory c:\Users\jemer\code\localflow`; log at
  `%LOCALAPPDATA%\LocalFlow\localflow.log`. Console mode: `.venv\Scripts\localflow`.
- Kill command: `Get-Process | Where-Object { $_.ProcessName -match
  'localflow|pythonw' } | Stop-Process -Force`
- Ship line (NEVER run): `git push`, tags, releases. Loop ends at local commits.
- Working branch: `main` (project convention, v1 committed straight to main).

## Current position
- Plan: `plans/v1.1-plan.md` (locked). Definition of done per milestone.
- Gate 0: CLEARED — Jeremy signed off AT-4/AT-6 live at session start (6 Jul).
- M6 faster startup: **DONE**, commit c20cf24. Hotkey live 6.5-7.2s, warm ~9s.
  Jeremy live-verified the warmup-window dictation pastes.
- M7 pill v2: **DONE**, commit 699dfce (three Jeremy feedback rounds; final
  spec in the plan's REVISED blocks; self-tested with real mouse input via
  scripts/m7_pill_check.py - 21/21; unit suite 74 pass).
- M8 settings window: **code-complete + committed 8e3d0a1** (review-clean
  after 2 Codex blockers fixed: mic-test recorder theft via hotkey release,
  warm-wait timeout escape). Machine E2E scripts/m8_settings_check.py
  passed. LIVE items pending Jeremy: hotkey live-apply, pill size rebuild,
  Test mic, cleanup toggle from settings. NOTE: Jeremy's machine went to
  the LOCK SCREEN mid-M8 verification (he is away) - all later live/input
  checks are deferred to his return.
- M9 machine half: **committed 2d2b0ac** (vad_threshold slider live-applied,
  calibrated noise floor: core estimate_noise_floor + reduce_noise
  noise_floor param + noiseprofile.npz store + settings Calibrate/Clear,
  claim_mic_session arbitration, micwatch on_switch rebuild). Review-clean
  (1 blocker + 1 minor fixed and verified). DEFERRED M9 items for Jeremy:
  DeepFilterNet spike (needs his OK to install torch), live sensitivity
  test, calibration A/B with fan noise, denoise A/B + default strength.
- M10: **DONE code-complete, commit bf85221** (dictionary + history +
  miner + settings sections; review clean, 143 tests).
- **LOOP STATE: all buildable work in the plan is DONE and committed.
  STOP-C surfaced to Jeremy: the live checklist (M8/M9/M10 live DoD
  items) + two decisions (DeepFilterNet torch install; preset value
  tuning). The tray app is RUNNING with the full v1.1 build. Nothing was
  pushed anywhere (ship line intact).**
- Current step: **M8 - real settings window.** M8a (live-apply plumbing) is
  BUILT + unit-tested (77 tests pass): app.apply_settings(changes, cfg,
  hooks) with RESTART_KEYS; _run hooks for hotkey rebuild (hotkey_ref),
  preprocess rebuild, restore_delay, inject_mode, input_device (micwatch
  now always runs; check_once skips switches while _configured_device
  pinned), visibility, cleanup, pill_size (rebuild_overlay tears down and
  rebuilds the pill UI thread - NEVER call from the UI thread itself);
  level fan-out (level_sinks) feeds pill + settings meter; app.engine_lock
  keeps Test-mic and the worker off the engine simultaneously; quit path
  in --no-tray mode now waits on quit_event (a live hotkey rebuild must
  not look like a quit). wire_settings_ui builds SettingsContext and sets
  overlay.settings_factory; overlay gained open_settings() (queue op);
  tray gained "Settings..." item. M8b (the window itself,
  localflow/desktop/settings_ui.py + tests/test_settings_ui.py) is being
  built by a subagent against the SettingsContext contract in app.py
  (search "wire_settings_ui"). Then M8c: verify live, review, commit.
  Jeremy's M7-round-2 direction honored: cleanup + visibility controls in
  the settings window, pill size slider (0.5-2.0), pill menu stays 4 items.
- Then M9 noise, M10 learning.
- Round count: M6 0 fails; M7 3 design-feedback rounds (not bugs), all closed.

## State that matters
- LocalFlow tray app is RUNNING with the M6 build (launched 11:21 by this loop,
  Samsung headset mic, GPU). Jeremy verified live dictation on it.
- Baseline tests: 61 pass.
- Review audit for M6: `.tmp/review-m6-startup.md` (gitignored scratch).
- Codex session: one thread live this session (adversarial review of M6);
  future Codex calls this session use --resume-last.

## Standing decisions in force
- Do NOT re-add multi-word hot_words. AT-4 measured with cleanup OFF.
- Tcl teardown on the overlay's own thread; ASCII-only console output.
- Auto-start at login stays NO.
- DeepFilterNet (M9): ask Jeremy before adding torch.
- tomlkit (M8) is pre-approved by the plan.
