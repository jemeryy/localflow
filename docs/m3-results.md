# M3 results — push-to-talk hotkey + clipboard injection

**Date:** 2 July 2026
**Verdict: PASS — AT-1, AT-2 and AT-3 all passed live at the mic (Jeremy),**
on top of a fully-machine-verified injection and hotkey stack. LocalFlow is now
a usable dictation tool: hold alt+`, speak, release, text appears in the
focused app.

## What was built

```
localflow/
  desktop/
    capture.py          #   + StreamRecorder — persistent mic stream, 0.3s
                        #     pre-roll, start()/stop() on key-down/up (FR-1/3)
    inject.py           #   inject_text() — FR-10 clipboard save/paste/restore,
                        #     inject_mode paste|type (FR-12), UIPI documented
                        #     (FR-11), waits for physical modifiers to clear
    app.py              #   `localflow` entry point: hotkey listener + worker
                        #     thread + per-utterance FR-7 logging
scripts/m3_inject_check.py  # live FR-10 harness vs its own tkinter window
tests/test_desktop.py       # 8 headless tests (recorder, inject, hotkey)
pyproject.toml              # + pynput, pyperclip; [project.scripts] localflow
```

Threading model: pynput listener callbacks only start/stop the recorder and
enqueue audio; a single worker thread does resample → VAD → STT → inject.
Models load once at startup (FR-14 half; tray is M4). Engine + VAD + mic
stream are all resident.

## Verification matrix

| Check | Result |
|---|---|
| pytest suite (15 tests: 7 core + 8 desktop) | **15/15 pass** |
| `scripts/m3_inject_check.py` — paste lands in focused window | PASS |
| — clipboard `PRESERVE_ME` survives the paste (AT-2 mechanism) | PASS |
| — empty text injects nothing (AT-3 shape) | PASS |
| — `inject_mode=type` lands text (FR-12) | PASS |
| Full loop, synthetic alt+` hold, silent room (real mic) | PASS — hook fired, 3.0s hold captured 3.30s (pre-roll works), VAD gated, nothing pasted |
| **AT-1 live (Jeremy, Notepad)** | **PASS** — "The quick brown fox jumps over the lazy dog." pasted; 1247ms first utterance, ~450ms warm (budget: p50 < 1.2s) |
| **AT-2 live** | **PASS** — clipboard preserved after dictation |
| **AT-3 live** | **PASS** — 3s silent hold → nothing pasted |

## Bugs found and fixed during M3

1. **Stream-open latency ate the first spoken words.** Opening the WASAPI
   stream on key-down cost ~300ms — warm-run dictations lost "The quick".
   Fix: the mic stream now opens once at startup and stays open; while idle
   only a rolling **0.3s pre-roll** is kept (then discarded — never stored,
   G2 intact), and key-down seeds the recording with it. Verified: 3.0s hold
   → 3.30s captured.
2. **A still-held hotkey modifier corrupted the paste.** Release ` while
   still holding Alt and the injected Ctrl+V arrives as Ctrl+Alt+V (silent
   misfire). Fix: `inject_text` polls `GetAsyncKeyState` until all physical
   modifiers (shift/ctrl/alt/win) are up, 1.5s timeout, before sending keys.
3. **cp1252 console crash killed the whole app.** The `●` recording glyph
   raised `UnicodeEncodeError` inside the pynput callback, which killed the
   listener thread and the app with it. Fix: ASCII-only console output,
   `sys.stdout.reconfigure(errors="replace")` (transcripts may contain
   unencodable chars), and hotkey callbacks wrapped so no exception can kill
   the listener again.

## Decisions

1. **Default hotkey is `` alt+` ``** (Jeremy's choice; Ctrl+Space collides
   with VS Code IntelliSense anyway). `--hotkey` accepts e.g. `ctrl+space`,
   `f9`; FR-13's config file lands in M4. Note: `` ` `` assumes a US layout.
2. **Persistent mic stream with 0.3s pre-roll** (see bug 1). Idle audio
   lives ≤0.3s in RAM and is discarded; nothing leaves the process.
3. pynput `HotKey.parse` + `Listener.canonical` for combo matching —
   left/right modifier variants normalize; key auto-repeat is guarded.

## Watch items for M4

- One silence-gated run took 879ms release-to-decision vs 45–81ms typical —
  unexplained outlier; watch during AT-4 latency measurement.
- First utterance after launch pays ~0.5–1s GPU warmup (M2 finding, still
  true: 950ms first vs ~250ms warm). If AT-4's p50 suffers, add one dummy
  `generate()` at startup.
- UIPI (FR-11): pasting into elevated windows silently fails. Documented,
  accepted, not fought.

## Environment deltas

- New in `.venv`: pynput 1.8.2, pyperclip 1.11.0.
- `localflow` console script installed (`.venv\Scripts\localflow`), runs
  `localflow.desktop.app:main`. Also runnable as
  `python -m localflow.desktop.app`.
