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
- Next step: **M7** (pill v2: visibility modes, hold-1s-to-drag, reset position).
  Files: overlay.py, tray.py, config.py. Drag state machine must be a pure
  testable class. Then M8 settings window, M9 noise, M10 learning.
- Round count: 0 fails on M6 (review fixes applied pre-commit, all green).

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
