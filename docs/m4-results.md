# M4 results — config file + tray + fallback + latency

**Date:** 2 July 2026
**Verdict: machine-verifiable work DONE (FR-13 config, FR-14/FR-2 tray,
AT-5 fallback, startup warmup, AT-4 instrumentation). AT-4 and AT-6 need
Jeremy live at the mic — checklist at the bottom.**

## What was built

```
localflow/
  desktop/
    config.py           # FR-13: localflow.toml (pydantic + tomllib), search
                        #   --config > ./localflow.toml > %APPDATA%/LocalFlow,
                        #   CLI flags override, typos are errors, relative
                        #   paths resolve against the config file's dir
    tray.py             # FR-14 + FR-2: pystray icon — grey idle / red rec /
                        #   amber transcribing; menu: status, cleanup (greyed
                        #   until M5), open settings file, quit
    app.py              # config wiring, startup warmup pass, tray on the
                        #   MAIN thread (--no-tray keeps M3 console mode),
                        #   AT-4 latency collection + p50/p95 summary on quit
localflow.toml          # commented default config at the repo root
scripts/m4_at5_fallback.py  # AT-5 headless harness (NONSENSE -> CPU + real STT)
tests/test_config.py        # 10 config tests
tests/test_desktop.py       # + 3 M4 tests (latency stats, FR-2 states, tray images)
pyproject.toml              # + pydantic, pystray, pillow
```

Threading model unchanged for audio (listener enqueues, worker transcribes +
injects) with one addition: pystray owns the **main thread** for its Win32
message loop; `tray.set_state()` is called from the listener/worker threads
and never raises. `--no-tray` restores the M3 console-only loop.

## Verification matrix

| Check | Result |
|---|---|
| pytest suite (28 tests: 7 core + 11 desktop + 10 config) | **28/28 pass** |
| FR-13: default launch loads `localflow.toml` from repo root | PASS (startup log) |
| FR-13: CLI flag overrides config (`--device NONSENSE` beat `device="GPU"`) | PASS |
| FR-13: unknown keys / bad values / bad TOML are readable errors | PASS (tests) |
| **AT-5: normal launch logs device=GPU** | **PASS** — "LocalFlow ready on GPU", model loaded on GPU in 2.0s |
| **AT-5: `--device NONSENSE` → CPU fallback, warning logged, still transcribes** | **PASS** — warning + "ready on CPU"; harness transcribed the M1 test WAV verbatim on CPU (stt 13609ms — CPU is slow, as budgeted) |
| FR-14: tray constructs and runs on the main thread (smoke launch) | PASS — no errors; menu quit path is live-checked below |
| Warmup pass at startup (kills the first-utterance penalty) | PASS — 876ms on GPU at startup; the M3 879ms VAD outlier cost now happens before "ready" |
| AT-4 instrumentation: per-utterance totals + p50/p95 summary on quit | Built + unit-tested; **live numbers pending** |

## Decisions

1. **Config lives in `localflow.toml`** (TOML via stdlib `tomllib`, validated
   by pydantic with `extra="forbid"` so typos fail loudly instead of being
   silently ignored). Search order: `--config PATH` > `./localflow.toml` >
   `%APPDATA%\LocalFlow\localflow.toml` > built-in defaults.
2. **Relative paths in a config file resolve against the config file's
   directory**, so `localflow` works from any cwd once a config exists.
   CLI-flag paths stay cwd-relative.
3. **Warmup is unconditional** (one VAD pass + one Whisper generate on 0.5s
   of silence at startup, output discarded). It costs <1s on GPU before the
   "ready" line and removes both known first-run penalties (M3: 1247ms first
   utterance; 879ms VAD outlier). On CPU fallback it costs ~8s — acceptable
   for an already-degraded mode.
4. **`cleanup_enabled = true` warns and is ignored** until M5 builds the
   CleanupEngine; the tray shows it greyed out.
5. **Tray failure degrades, never blocks**: if pystray can't start, the app
   logs it and runs console-only, same as `--no-tray`.

## Watch items

- Killing the process leaves a ghost tray icon until mouse-over (Windows
  behaviour, cosmetic). Quitting from the menu cleans up properly.
- Ctrl+C while the tray message loop owns the main thread may not interrupt
  until an event arrives — quit from the tray menu; Ctrl+C remains the
  documented quit for `--no-tray`.
- CPU warmup is ~8s. If CPU ever becomes a deliberate daily mode (it
  shouldn't), gate warmup on device.

## Waveform overlay (3 July, Jeremy's request — Wispr-Flow-style)

- `localflow/desktop/overlay.py` — **WaveformOverlay**, a borderless
  always-on-top capsule at bottom-center, now PERSISTENT (v2, same day —
  "I want some indication that localflow is running"):
  - idle: small chip — green dot + "LocalFlow" wordmark, always visible
  - launch: "loading models..." then a "Ready - hold alt+` or click to
    dictate" flash, then the chip
  - recording: near-white smoothed waveform (fast-attack/slow-decay easing
    + 3-tap spatial blend, round-capped bars) + breathing red dot; levels
    are per-chunk RMS from `StreamRecorder.on_level`
  - transcribing: amber frozen waveform
  - **left-click: toggle dictation** (start / stop+transcribe) via
    `LocalFlowApp.toggle_recording()`; **right-click: inline
    Settings | Quit** menu drawn in the capsule (auto-collapses)
- Never steals focus: `WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW` — clicking the
  pill leaves the paste target focused, so click-to-dictate pastes into
  the right app (FR-2). DPI-aware (SetProcessDpiAwareness) — crisp at the
  machine's 165% scaling, all geometry scaled by `system_scale()`.
- Threading: Tk runs on its own dedicated thread (pystray keeps the main
  thread); all UI calls stay on the Tk thread via queues; the audio path
  pushes levels lock-free (`deque.append`); quit = `on_quit` callback that
  stops the tray loop (or the hotkey listener under `--no-tray`).
- Teardown gotcha fixed: tkinter's module-global `_default_root` must be
  cleared and GC'd **on the Tk thread**, else Tcl aborts the process at
  exit ("Tcl_AsyncDelete: async handler deleted by the wrong thread").
- Config: `overlay_enabled = true` (FR-13 file) / `--no-overlay` flag;
  overlay failure degrades to no-overlay, never blocks dictation.
- Verified by `scripts/m4_overlay_check.py` (screenshot-based, DPI-scaled):
  loading pill 38904 px, waveform 10086 px while recording, idle chip
  6930 px and no expanded pill. PASS. Suite: 34/34.

## Real-app packaging (3 July, follow-up to M4)

- **`localflow-tray`** gui-script (no console window). `print()` is a no-op
  there (sys.stdout is None); logs go to
  `%LOCALAPPDATA%\LocalFlow\localflow.log`, fatal startup errors raise a
  message box pointing at the log.
- **Single-instance guard** (named Win32 mutex): a second launch prints/logs
  "already running" and exits with code 2 — two instances would paste every
  dictation twice. Verified live.
- **Start Menu shortcut** "LocalFlow" (icon: `assets/localflow.ico`,
  Start-in: repo root so `localflow.toml` is found). Windowless launch
  verified: no window, ready on GPU in ~5s, log written, killed cleanly.
- **Auto-start at login NOT set up** — pending Jeremy's explicit OK (it's a
  copy of the same shortcut into `shell:startup`).

## Environment deltas

- New in `.venv`: pydantic 2.13.4, pystray 0.19.5, pillow 12.3.0.
- New file at repo root: `localflow.toml` (checked in; it's the live config).
- New: `assets/localflow.ico`, `localflow-tray.exe` gui-script, Start Menu
  shortcut `LocalFlow.lnk` (points into `.venv` — recreate if the repo moves).

## Live checklist for Jeremy (closes M4)

Run `.venv\Scripts\localflow` from the repo root, then:

1. **Indicator sanity (FR-2/FR-14):** on launch the pill shows "loading
   models..." then flashes "Ready". Hold alt+` → pill shows a live red
   waveform that moves with your voice (tray dot also red); release →
   amber flicker → pill hides. Tray menu shows "whisper-turbo-ov on GPU",
   cleanup greyed, "Open settings file" opens localflow.toml, Quit exits
   cleanly.
2. **AT-4 (latency):** dictate the SAME ~5s sentence 10 times (e.g. "the
   quick brown fox jumps over the lazy dog and runs back home again").
   Quit from the tray — the console prints
   `[at-4] 10 dictations: p50 ...ms, p95 ...ms`. Pass = p50 < 1200ms.
3. **AT-6 (three apps):** one dictation each into (a) Notepad, (b) a Chrome
   text box or the address bar, (c) the VS Code editor. Pass = text lands
   correctly in all three. (If an elevated window is involved, silent
   failure is expected — FR-11, not a bug.)

Then record the numbers in this file, flip the verdict to PASS, and update
CLAUDE.md's build status.
