# Session handoff — M4 + M5 built, live ATs pending (3 July 2026)

## Where the project stands

**M1–M3: signed off** (see docs/m1..m3-results.md — do not redo).
**M4: built, machine-verified, NOT signed off** — AT-5 (fallback) PASSED;
AT-4 (latency p50/p95) and AT-6 (three apps) still need Jeremy at the mic
(docs/m4-results.md has the checklist).
**M5 (cleanup): built, AT-8 PASSED machine-verified** on GPU
(docs/m5-results.md). Live feel-check by Jeremy pending.

**NOTHING from this session is committed.** Working tree holds M4 + overlay
+ packaging + M5 + fixes. First task next session (after Jeremy's live
checks or on his word): commit as a few clean commits, e.g. (1) feat(m4)
config+tray+fallback+latency, (2) feat(overlay) waveform pill v2,
(3) feat(packaging) gui-script/icon/single-instance/Start-Menu,
(4) feat(m5) cleanup engine + Qwen export, (5) fix(inject) paste race.

**LocalFlow is RUNNING on the machine right now** (localflow-tray.exe,
started 09:08) with: cleanup ON, hot_words OFF, following the system
default mic (currently the built-in Intel array — Samsung headset was
unplugged; it auto-switches back when replugged).

## What was built this session (all tested, suite 41/41)

1. **FR-13 config** — `localflow/desktop/config.py`, `localflow.toml` at
   repo root (checked in, live). pydantic extra="forbid"; search --config >
   ./localflow.toml > %APPDATA%\LocalFlow; CLI overrides; config-file
   relative paths resolve against the file's dir.
2. **FR-14/FR-2 tray** — `tray.py`: grey/red/amber dot, status, LIVE
   cleanup toggle ("slower, cleaner"), open settings, quit.
3. **Overlay v2** — `overlay.py`: persistent bottom-center pill.
   Idle = chip (green dot + wordmark); rec = smoothed white waveform +
   breathing red dot; busy = amber; left-click toggles dictation;
   right-click = inline Settings|Quit. WS_EX_NOACTIVATE (never steals
   focus), DPI-aware (display is 165%), Tk on its own thread.
   CRITICAL teardown: tkinter._default_root must be cleared + GC'd on the
   Tk thread or Tcl aborts the process at exit.
4. **Packaging** — `localflow-tray` gui-script (no console; print() no-ops;
   logs to %LOCALAPPDATA%\LocalFlow\localflow.log), single-instance mutex,
   assets/localflow.ico, Start Menu shortcut. **Auto-start at login NOT
   installed** — permission classifier blocked it and Jeremy hasn't said
   yes yet; it's just copying the shortcut to shell:startup.
5. **Default-mic auto-follow** — `micwatch.py` polls WASAPI default capture
   endpoint via pycaw/COM every 2s; StreamRecorder.refresh_default_device()
   does close → sd._terminate()/_initialize() → reopen (PortAudio can't see
   device changes otherwise). Defers while recording. Only when
   input_device is unset.
6. **M5 cleanup** — `core/cleanup.py` CleanupEngine (LLMPipeline chat mode,
   FR-9 fixed prompt, validate_cleanup guardrails: strip quotes / reject
   empty / reject >2.5x expansion → raw text wins). Model:
   `models/qwen2.5-3b-ov` (Qwen2.5-3B-Instruct INT4, 1.65GB, exported via
   throwaway .venv-export, since deleted). Sync "clean mode", tray-toggled,
   warmup at startup. AT-8 PASS: "um so like the meeting is uh on tuesday"
   → "The meeting is on Tuesday." 572–1161ms/utterance on GPU.
7. **FR-10 paste-race FIX** (found live: a busy app pasted Jeremy's OLD
   clipboard — a shell command with fara dev keys — instead of speech).
   Settle 120ms → 500ms (`restore_delay_ms` in config); restore skipped if
   clipboard changed mid-settle. Regression-tested.
8. **Hot words** — built (SttEngine initial_prompt), then REVERTED to
   `hot_words = []` same day: a 5-word list caused split/dropped words.
   Mechanism stays for targeted use; keep lists to 1–3 words if ever.

## Decisions locked this session

- Overlay is the primary indicator; it must NEVER take focus.
- Cleanup default OFF in template, ON in Jeremy's live config; AT-4 must be
  measured with cleanup OFF (PRD §7 defines the budget without it).
- Startup warmup is unconditional (kills first-utterance penalty; ~2-3s
  with cleanup, inside the "loading" pill).
- pystray owns the main thread; Tk overlay its own thread; audio callbacks
  push levels lock-free.

## Environment deltas

- `.venv` adds: pydantic, pystray, pillow, pycaw (+deps). `localflow-tray`
  gui-script. `models/qwen2.5-3b-ov` (1.65GB). Start Menu LocalFlow.lnk
  (points into .venv — recreate if repo moves).
- Log file: `%LOCALAPPDATA%\LocalFlow\localflow.log` (windowless mode).
- Suite: `.venv\Scripts\python.exe -m pytest tests -q` → 41 tests.

## Remaining to close v1 (+ cleanup as v1.1)

1. Jeremy's live checks (docs/m4-results.md checklist): pill/tray sanity,
   AT-4 (10× fixed sentence, cleanup OFF, quit, read `[at-4]` line from the
   log), AT-6 (Notepad/Chrome/VS Code).
2. Record results in m4-results.md, flip verdict, update CLAUDE.md status.
3. Commit everything (see split above).
4. Jeremy's pending decisions: auto-start at login (yes/no); whether the
   accuracy complaints persist on the headset mic (if yes: investigate mic
   levels/VAD, not hot words).

## Watch items

- Transcription accuracy on the built-in mic array is noticeably the
  weak point; headset auto-switches now when plugged in.
- Paste race: if a slow app ever pastes stale clipboard again, raise
  restore_delay_ms (500 now).
- Ghost tray icons after force-kill (cosmetic; menu-quit is clean).
- Ctrl+C can't interrupt the tray message loop — quit via tray/pill.
