# Session handoff — usability fixes: preprocessing, lazy cleanup, mic self-heal (3 July 2026, session 2)

Follows `plans/handoff-2026-07-03-m4-m5-built.md`. That doc still describes the
M4/M5 baseline; this one layers four fixes on top, all driven by Jeremy's live
complaints: glitchy pill menu, slow load, no audio, high memory/CPU.

## Where the project stands

**M1–M3 signed off. M4/M5 built, still not signed off, still NOT committed.**
This session added four fixes on top of that uncommitted pile. **Whole working
tree is uncommitted** — see "Commit plan" below.

Suite: **51 tests pass** (was 41). `.venv\Scripts\python.exe -m pytest tests -q`.

## The live diagnosis (root cause of Jeremy's complaints)

Read the log first next session: `%LOCALAPPDATA%\LocalFlow\localflow.log`.

The Samsung USB headset was **wedged at the Windows/WASAPI level**, not in
LocalFlow. Proven by a minimal sounddevice script outside the app:

- The headset delivered **0 frames to _any_ process**, at any sample rate,
  while the built-in Intel array worked fine. Every dictation logged
  `recording stopped: 0.00s captured`. → "doesn't pick up audio".
- Opening a stream on the wedged endpoint took **15.4s** (vs ~0.3s normal);
  ~25s of a measured 42.7s startup was this stall. → "takes a long time to load".
- The Qwen2.5-3B cleanup LLM was loading at startup: **~2–3GB shared iGPU
  memory, 5.5s load, 0.5–1.8s per dictation** (session p50 was 2383ms vs the
  1200ms AT-4 target). → "takes a lot of memory/processing".

**Fix for the mic itself is physical: replug the headset** (or switch the
Windows default input to the Intel array). A `Restart-Service Audiosrv` would
also reset it but needs admin (this session's shell had none). LocalFlow
auto-follows the default across the unplug/replug cycle.

## What was built this session (all tested)

### 1. Overlay right-click menu fix — `localflow/desktop/overlay.py`
- **Bugs:** the inline Settings|Quit menu closed the instant the mouse skimmed
  off the tiny pill (`on_leave` → `apply("idle")`), and the click targets were
  the text glyphs only, so near-miss clicks on "Settings" fell through and did
  nothing.
- **Fix:** each option is now a **full-height hit box** (left half = Settings,
  right half = Quit), tagged `hit_settings`/`hit_quit` (clickable) and
  `rect_settings`/`rect_quit` (recolour). Added a `<Motion>` hover highlight.
  The menu **no longer closes on `<Leave>`** — it closes on a button click, an
  off-button click, or the 6s `_MENU_TIMEOUT_MS`.
- **Kept:** WS_EX_NOACTIVATE (pill never steals focus). Deliberately did NOT
  switch to a native `tk_popup` menu because that would grab focus off the
  paste target — the whole invariant of the pill.

### 2. Audio preprocessing before STT — `localflow/core/preprocess.py` (NEW)
- Pure numpy + scipy (scipy was already present transitively; **added to
  `pyproject.toml` deps** since we now import it directly). Lives in the core
  seam — no desktop imports, no network — so FARA reuses it.
- `AudioPreprocessor.process()` chain: DC removal → high-pass ~80Hz
  (Butterworth `sosfiltfilt`) → **gentle spectral-gating denoise** (STFT,
  per-frequency noise floor from the quietest frames, soft mask with time/freq
  smoothing so it never nulls a bin → no musical noise) → **percentile-based
  peak normalization** (boosts quiet mics, leaves already-loud audio alone,
  brickwall-clips outliers).
- Runs **before VAD** in `transcribe_audio(..., preprocess=...)` so the noise
  estimate can see the non-speech regions (pre-roll + trailing pad).
- Config (all three new, defaults ON/gentle): `denoise_enabled=true`,
  `normalize_audio=true`, `denoise_strength=0.6`. Wired in `app.py` + warmup.
- **Verified:** transcription of the known-good test WAV is **byte-identical**
  with preprocessing ON vs OFF (no accuracy regression), negligible latency.
- **`denoise_strength` is the safety valve** — the M4 handoff warns that
  over-processing hurt WER (hot_words). If real-mic accuracy regresses, lower
  it toward 0.3 or set `denoise_enabled=false`. Normalization alone is safest.

### 3. Lazy-loaded cleanup LLM — `app.py`, `tray.py`, config
- Live config flipped to **`cleanup_enabled = false`**. Qwen is now NOT loaded
  at startup. If the model dir exists, `_run` hands the app a `cleanup_loader`
  closure; the **first tray "Cleanup" toggle loads it in a background thread**
  (dictation keeps working raw meanwhile), and subsequent toggles are instant
  flips. `cleanup_enabled=true` still preloads-and-on as before.
- `LocalFlowApp.cleanup_available` (engine loaded OR loader present) drives
  whether the tray shows a live toggle; `toggle_cleanup()` handles the
  optimistic-ON-while-loading and load-failure-flips-back-off paths.
- Effect: startup skips the 5.5s Qwen load, ~2–3GB memory freed, and AT-4
  latency should now land well under budget (cleanup off by default, per PRD §7).

### 4. Mic self-heal + non-blocking key-down — `capture.py`, `micwatch.py`, `app.py`
- `StreamRecorder` now stamps `_last_frame` on every callback. `stale()` =
  stream open + not recording + no frames for >2s. `heal_if_stale()` reopens
  via full PortAudio reinit, **rate-limited to once per 10s** (no reinit storm
  when the mic is wedged at the OS level and a replug is really needed).
- Healing is driven by the **micwatch tick** (its unchanged-device branch now
  calls `heal_if_stale()`), NOT by key-down.
- **`start()` is now non-blocking**: it acquires `_reopen_lock` with a 0.05s
  timeout and skips the insurance `open()` if it can't get it. This is
  load-bearing — key-down runs on the pynput keyboard-hook thread, and a hook
  that blocks (e.g. the 15.4s wedged-stream open) **freezes ALL keyboard input
  in Windows**. Recording still starts; chunks flow once the reopened stream
  is live.
- A 0-frame capture now logs a **loud warning naming the device** instead of a
  silent "nothing captured".

### 5. Auto-fallback to a working mic — `capture.py` (session 3, 3 July)
- **Why:** the wedged Samsung headset came back (0 frames to any process,
  again). The heal loop reopened it forever; dictation stayed dead until a
  replug. Jeremy: "it still doesnt record my voice now".
- **Fix:** `heal_if_stale()` now escalates. Heal 1 = plain reopen (as
  before). If the stream is STILL silent at the next heal (`_heal_streak >=
  2`) and we're following the system default, `_fallback_to_working_device()`
  probes every WASAPI input (`_find_live_input`: open + wait for a real
  callback frame — a wedged endpoint opens fine but never fires one) and
  switches to the first device that delivers audio, logged loudly as
  "FALLBACK MIC". A pinned `input_device` NEVER falls back.
- **Recovery:** every `refresh_default_device()` restores the configured
  device and clears the fallback — unplug/replug fires two default-endpoint
  changes, so micwatch swaps back automatically. Wedged-name exclusion is by
  prefix (MME truncates names to 31 chars; WASAPI doesn't).
- Suite now **55 tests** (4 new). The old second-heal test was updated: a
  second consecutive heal now IS the fallback hunt.

### 6. Settings button fix — `overlay.py`, `tray.py`, `config.py` (session 3)
- **Bugs (two, stacked):** `.toml` has NO file association on this machine,
  so `os.startfile` could only ever raise; and the overlay menu's click
  detection used Tk's `"current"` item tag, which never registered the hit
  on the NOACTIVATE window (nothing in the log = the handler never ran).
- **Fix:** overlay menu clicks + hover now use pure coordinate hit-testing
  against `menu_boxes()`. Opening moved to `config.open_settings_editor()`
  (shared by tray + overlay): `os.startfile`, falling back to `notepad.exe`
  on OSError. Logs "opening settings file ..." on every click.

### 7. Cleanup LLM releases its memory on toggle-off — `app.py`, `cleanup.py`
- Jeremy: "make it so that the LLM cleanup doesn't pick up RAM, only when I
  use it." `CleanupEngine.close()` (new) drops the ov_genai pipeline;
  `toggle_cleanup()` OFF now unloads (close + gc.collect), and the loader
  re-arms the next ON. Toggling OFF mid-load discards the engine on arrival.
  The loader is now created whenever the model dir exists, so even a
  `cleanup_enabled=true` preload can be unloaded from the tray.
- **Measured (commit charge):** STT-only 1959MB; +cleanup 3887MB (+1.9GB);
  after toggle-off 2429MB — **~1.5GB returned**; ~0.5GB is one-time
  OpenVINO-GenAI library/heap retention that does not grow per cycle.

## Commit plan (nothing is committed yet)

The prior handoff proposed a 5-commit split for M4/M5. Append this session:

6. `feat(core): audio preprocessing (high-pass + denoise + normalize) before STT`
   — `core/preprocess.py`, `core/__init__.py`, `core/pipeline.py`, pyproject
   (scipy), config fields, `tests/test_core.py`.
7. `fix(overlay): right-click menu — full-height hit boxes, hover, no close-on-leave`
   — `desktop/overlay.py`.
8. `perf(desktop): lazy-load cleanup LLM on tray toggle; default off`
   — `desktop/app.py`, `desktop/tray.py`, config, `localflow.toml`,
   `tests/test_desktop.py`.
9. `fix(capture): self-heal wedged mic stream; non-blocking key-down start`
   — `desktop/capture.py`, `desktop/micwatch.py`, `desktop/app.py`,
   `tests/test_desktop.py`.
10. `feat(capture): auto-fallback to any working mic when the default stays
    wedged` — `desktop/capture.py`, `tests/test_desktop.py`.

Refresh the "Build status" block in `CLAUDE.md` at commit time.

## Remaining to close v1

1. **Replug the headset, restart LocalFlow**, confirm audio returns and load is
   fast (~5s, no 15s stall).
2. Jeremy's pending live checks (from `docs/m4-results.md`): overlay menu
   sanity, AT-4 (10× fixed sentence, **cleanup off** — now the default), AT-6
   (Notepad/Chrome/VS Code). AT-4 should pass comfortably now.
3. A/B the denoise on the real mic; dial `denoise_strength` (or off) if it hurts.
4. Commit everything (split above); flip verdicts in m4/m5-results.md.

## Decisions locked this session
- **Auto-start at login: NO** (Jeremy's answer). Do not install the
  shell:startup shortcut.
- Cleanup is **off by default and lazy** — memory-minimal startup wins; the
  toggle is one click when wanted.
- Preprocessing **defaults ON but gentle**; `denoise_strength` is the dial.
- Pill menu stays a custom inline draw (never a focus-stealing native popup).

## Watch items
- The Samsung headset can wedge at the OS level (0 frames + 15s open). The heal
  logic recovers transient cases; a hard wedge still needs a replug. If it
  recurs often, consider pinning `input_device = "Intel"` in `localflow.toml`.
- Denoise default ON: watch real-mic accuracy; back off via `denoise_strength`.
- `input overflow` warnings in the log around 09:28 were the assistant running
  GPU verification against the live instance — transient, not a real fault.
- Ghost tray icons after force-kill (cosmetic; menu-quit is clean) — unchanged.
