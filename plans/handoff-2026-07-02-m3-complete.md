# Session handoff — M3 complete (2 July 2026)

## Where the project stands

**M1, M2, M3 are DONE and fully signed off** (`docs/m1-results.md`,
`docs/m2-results.md`, `docs/m3-results.md`). LocalFlow is now a working
dictation tool: run `.venv\Scripts\localflow`, hold **alt+`**, speak, release
→ text pastes into the focused app. AT-1/2/3 passed live at the mic.
**Next milestone: M4 — fallback + latency + tray + config (AT-4/5/6).**
M5 (LLM cleanup) stays v1.1.

Key M3 decisions locked (do not relitigate):
- **Default hotkey is `` alt+` ``** (Jeremy's explicit choice). US-layout
  assumption on the backquote key is accepted.
- **The mic stream stays open for the app's lifetime** with a 0.3s pre-roll
  ring (discarded when idle). Opening the stream per-utterance cost ~300ms
  and ate the first spoken words — do not go back to open-on-keydown.
- **Injection waits for physical modifiers to clear** (GetAsyncKeyState poll,
  1.5s timeout) before sending Ctrl+V — a still-held Alt turned the paste
  into Ctrl+Alt+V. Keep this.
- **Console output must stay ASCII** (cp1252 consoles) and stdout is
  reconfigured with errors="replace". An encoding crash in a pynput callback
  previously killed the whole app; callbacks are wrapped — keep them wrapped.

## What exists (committed with M3)

- `localflow/core/` — unchanged, still PURE (AT-7 tested). GPU primary.
- `localflow/desktop/capture.py` — `record_audio()` (M2) + `StreamRecorder`
  (persistent stream, `open()/start()/stop()/close()`, 60s cap, pre-roll).
- `localflow/desktop/inject.py` — `inject_text(text, mode)`, FR-10 exactly
  (save → set → Ctrl+V → 120ms → restore), `type` mode, empty ⇒ nothing.
- `localflow/desktop/app.py` — `localflow` entry point: `PushToTalkListener`
  (HotKey.parse + canonical matching, auto-repeat guard, crash-proofed),
  `LocalFlowApp` (worker thread; listener thread only enqueues), CLI flags
  `--model --device --fallback --cache --hotkey --inject-mode --input-device`.
  Already prints per-utterance release-to-paste totals (feeds AT-4).
- `scripts/m3_inject_check.py` — self-contained live FR-10 harness (own
  tkinter window, aborts if it can't take focus). Reusable after refactors.
- `tests/` — 15 tests (7 core + 8 desktop, all headless-safe).
  Run: `.venv\Scripts\python.exe -m pytest tests -v`

## Environment (gitignored, on disk)

- `.venv/` — adds pynput 1.8.2, pyperclip 1.11.0. `localflow` console script.
- `models/whisper-turbo-ov/` (do NOT re-export) + `models/ov_cache/` (keep;
  NPU blob costs 188s to recompile). GPU warm load 1.4–2.9s observed.
- Mic: 'Headset Microphone (Samsung USB' @ 44.1kHz, low level but accurate.

Fresh-session sanity check: pytest 15/15, then run `localflow` and dictate.

## Next: M4 — fallback + latency + tray + config (PRD §9, AT-4/5/6)

Suggested shape:
1. **Config file (FR-13):** pydantic + TOML at repo root (or %APPDATA%);
   fields: hotkey, stt_model_dir, device, fallback_device, cache_dir,
   cleanup_enabled, cleanup_model_dir, inject_mode, input_device. CLI flags
   become overrides. Device/model changes take effect on restart.
2. **Tray (FR-14 + FR-2):** pystray + Pillow (new deps). Icon state change
   while recording replaces the console [rec] line; menu = status, toggle
   cleanup (greyed until M5), open settings file, quit. Keep the console
   fully functional when run without tray.
3. **AT-4 latency:** 10 dictations of a fixed ~5s sentence; log p50/p95
   release-to-paste (app already prints totals — just collect). p50 < 1.2s.
   M3 observed ~450ms warm, 1247ms first — likely passes; if the first
   utterance drags the numbers, add one dummy generate() at startup.
4. **AT-5 fallback:** `SttEngine` fallback logic already exists — run it:
   launch with `--device NONSENSE`, confirm CPU fallback + warning log +
   still transcribes.
5. **AT-6 three apps:** Notepad, Chrome text box/address bar, VS Code editor.

Watch items carried into M4:
- One 879ms silence-decision outlier (typical 45–81ms) — check whether it
  recurs during AT-4; suspect first-run soxr/ONNX warmup in the worker.
- pystray runs its own event loop thread — same rule as pynput: keep
  transcribe/inject on the worker, and mind the main-thread ownership
  (pystray on Windows wants the main thread for its message loop; the
  hotkey listener joins fine from a non-main thread).
- Injection into elevated apps silently fails (UIPI, FR-11) — if AT-6 hits
  it, that's expected; document, don't fight.
