# Session Handoff — Local TTS read-aloud in LocalFlow + root-cause fix for the native comtypes crash

## Where it started
Jeremy asked to implement text-to-speech from `C:\Users\jemer\code\localflow\docs\handoff-2026-07-14-tts-integration.md`, with a hard framing: narrate ONLY on demand (press the hotkey, or right-click LocalFlow and pick Narrate with text selected), NEVER auto-narrate every Claude reply. Mid-session he added volume control, then reported misreading/spacing, then that nothing-selected still narrated, then a crash when narrating. This built on the prior `C:\Users\jemer\code\localflow\plans\handoff-2026-07-13-thankyou-trailing-doubles-native-crash.md` (which had armed a faulthandler crash catcher for exactly this native crash).

## Important discoveries and decisions
- **Engine = local neural Piper** (Jeremy chose "Local neural" over edge-tts/SAPI). Voice `en_US-hfc_male-medium` at `length_scale=0.8` (~25% faster), both ear-approved live. `piper-tts` 1.4.2 installs clean into `.venv` (only adds `pathvalidate`; reuses existing onnxruntime 1.27 + numpy 2.4.6 — NO torch, no numpy downgrade, so NO isolated venv needed unlike the DeepFilterNet spike).
- **Playback = stdlib `winsound`** (not ffplay). A new async sound replaces the old one (one-voice-at-a-time for free) and `SND_PURGE` stops it — no pid-file/taskkill dance the interim script needed.
- **Voice models live in `models/piper/`** (gitignored, like the other models). Only `en_US-hfc_male-medium.onnx` + `.onnx.json` kept; ryan-high was downloaded for the A/B then deleted.
- **Killed the interim `read_selection.py` listener** (was PID 26524) — it held Ctrl+Alt+R globally with the ONLINE edge-tts voice and would double-fire/conflict. There is NO active auto-narration Stop hook anywhere in `~/.claude` (checked); nothing to disable there.
- **The crash Jeremy hit is NOT the TTS code.** `crash.log` fault stack: overlay Tk thread, `on_motion` -> `highlight_menu` -> tkinter `itemconfigure`, during "Garbage-collecting" running a `comtypes` `Release` in `__del__`. This is the SAME pre-existing native `_ctypes` access-violation the 13-14 Jul handoffs left OPEN. **Root cause now identified:** pycaw/comtypes COM objects (created every 2s by `micwatch._default_endpoint_id`, per-dictation by `micgain._endpoint_volume`) become cyclic garbage; when Python's GC finalizes one ON A THREAD THAT NEVER CALLED `CoInitialize`, comtypes' `Release` access-violates. The overlay Tk thread was such a thread; a pill-menu hover triggered GC at the wrong moment. Jeremy hit it because he used the PILL MENU to narrate (the hotkey path never opens the menu).

## Decisions locked + what shipped
All UNCOMMITTED on `main` (working tree already dirty from prior sessions). App runs from editable install; restart picks up edits. 247 tests pass (was 233 start of prior session; +14 net from new `test_tts.py`, minus none).

- **Core TTS seam** — `C:\Users\jemer\code\localflow\localflow\core\tts.py` (new): `TtsEngine` (loads Piper voice once; `length_scale` + `volume` are plain attrs for live update; `synthesize_wav`) and `clean_for_speech`. Pure — no clipboard/keyboard/playback/network (respects the core seam). Exported from `localflow/core/__init__.py`.
- **Desktop Narrator** — `C:\Users\jemer\code\localflow\localflow\desktop\narrate.py` (new): owns clipboard + copy-selection keystrokes + winsound playback. `toggle_selection()` (hotkey/pill/tray), `toggle_clipboard()` (kept, unused by UI now), `stop()`, `set_length_scale`/`set_volume`. Lazy-loads the voice on first use. Copy-selection ported from the interim script (Alt-keyup before Ctrl+C).
- **No-selection => silence** — `_copy_selection` now CLEARS the clipboard first as a sentinel; if nothing is selected, Ctrl+C copies nothing, it stays empty, returns "" -> no narration. Original clipboard restored either way. (Fixed the "narrate re-speaks old clipboard when nothing selected" bug.)
- **clean_for_speech pacing** — Jeremy wants his natural spacing honoured as PAUSES. Every line break, paragraph, and list point ends its line with sentence punctuation (a period if none), so the voice pauses. Bullet/number markers stripped ("- ", "1." not read aloud). PDF/web line-break hyphenation rejoined ("inter-\nnational" -> "international"). A pause is inserted before a bracket ("text (note)" -> "text, (note)"). NOTE: I reversed an earlier "join soft-wrapped lines with a space" behaviour — Jeremy explicitly wants EVERY line break to pause.
- **Volume** — Piper `SynthesisConfig.volume` multiplier. Config `tts_volume` (0.1-2.0, default 1.0), live via the settings slider.
- **Triggers wired** (all speak the SELECTION, all run OFF the caller thread via `narrate_selection()` in app.py so the ~0.2s copy-settle never blocks the pynput hook thread or Tk thread):
  - Hotkey `ctrl+alt+r` (PRESS, not hold) via `keyboard.GlobalHotKeys` — `C:\Users\jemer\code\localflow\localflow\desktop\app.py`.
  - Pill right-click menu "Narrate selection" — `C:\Users\jemer\code\localflow\localflow\desktop\overlay.py` (the RELIABLE surface: pill is WS_EX_NOACTIVATE so the source app keeps its selection).
  - Tray "Narrate selection" — `C:\Users\jemer\code\localflow\localflow\desktop\tray.py` (works when focus returns to the source app after the menu closes; less reliable than the pill).
- **Settings "Read aloud" section** — `C:\Users\jemer\code\localflow\localflow\desktop\settings_ui.py`: Speech speed slider (0.4-1.5) + Volume slider (0.1-2.0), both live via the M8 apply pipeline (`tts_speed`/`tts_volume` hooks in app.py `apply_settings` + `live_hooks`).
- **Config** — `C:\Users\jemer\code\localflow\localflow\desktop\config.py`: added `tts_enabled` (True), `read_aloud_hotkey` ("ctrl+alt+r"), `tts_voice` ("models/piper/en_US-hfc_male-medium.onnx", added to `_PATH_FIELDS`), `tts_length_scale` (0.8, validated 0.4-2.0), `tts_volume` (1.0, validated 0.1-2.0). Only `tts_length_scale`+`tts_volume` are LIVE; changing `tts_enabled`/`read_aloud_hotkey`/`tts_voice` needs a restart (they fall to the `else: restart.add` branch).
- **NATIVE CRASH ROOT-CAUSE FIX** — `comtypes.CoInitialize()` (never uninitialized; the app hard-exits with `os._exit`) added to the three long-lived threads that run GC and can finalize a pycaw COM object: the overlay UI thread (`overlay.py` `_ui_thread`, the confirmed crash site), the worker `_drain` thread (`app.py`, where micgain auto-gain runs), and the Piper synth thread (`narrate.py` `_run`, allocates heavily). `pyproject.toml`: added `piper-tts`.

## Key files for next session
- Standing plan: `C:\Users\jemer\code\localflow\plans\v1.1-plan.md` (this session's work folds into its milestone log on sign-off).
- Source handoff this implemented: `C:\Users\jemer\code\localflow\docs\handoff-2026-07-14-tts-integration.md`.
- Prior native-crash handoff (context for the crash fix): `C:\Users\jemer\code\localflow\plans\handoff-2026-07-13-thankyou-trailing-doubles-native-crash.md`.
- `C:\Users\jemer\code\localflow\localflow\core\tts.py` — the seam + `clean_for_speech` pacing logic (read if pacing needs more tuning).
- `C:\Users\jemer\code\localflow\localflow\desktop\narrate.py` — Narrator (clipboard/keys/playback + the CoInitialize guard).
- `C:\Users\jemer\code\localflow\localflow\desktop\app.py` — narrator construction (~line 969), `narrate_selection()`, hooks, `_drain` CoInitialize (~line 433), shutdown (`narrate_ref`/`narrator.stop`).
- `C:\Users\jemer\code\localflow\localflow\desktop\overlay.py` — pill Narrate item + `_ui_thread` CoInitialize.
- `C:\Users\jemer\code\localflow\tests\test_tts.py` — clean_for_speech + Narrator/copy-selection tests.
- Logs (read FIRST on any live report): `C:\Users\jemer\AppData\Local\LocalFlow\localflow.log` and (crashes) `C:\Users\jemer\AppData\Local\LocalFlow\crash.log`.
- Memory files touched: none.

## Running state
- Background processes: none (the only `run_in_background` shell this session, `bq8lomt38`, a grep, has completed).
- Dev servers / ports: none.
- Open worktrees / branches: none (on `main`, dirty, all uncommitted).
- Jeremy's LocalFlow tray app: running PRE-fix code — **needs a restart** to load TTS + the crash fix. Its last run at 20:40 loaded the narrate hotkey but is pre-comtypes-fix.

## Verification — how to confirm things still work
- `C:\Users\jemer\code\localflow\.venv\Scripts\python.exe -m pytest tests -q` — expect **247 passed**.
- Real synth verified this session (played through speakers): `Narrator("models/piper/en_US-hfc_male-medium.onnx").toggle_clipboard()` synthesizes + plays + auto-resets the speaking flag; volume 0.5 vs 1.5 audibly different; pacing sample paused at line breaks/points/brackets.
- Live (after restart): select text -> Ctrl+Alt+R speaks it; press again stops; nothing selected -> silence; right-click pill -> "Narrate selection" works without crashing.

## Deferred / pending tasks + open questions
- **Open (asked, not answered): harden the mic-COM code** so pycaw objects are RELEASED on their own thread instead of relying on the CoInitialize safety net — belt-and-suspenders for the native crash. Jeremy didn't answer.
- **Open (asked, not answered): re-download ryan-high** (~120MB, better pronunciation) to compare. Pronunciation accuracy is Piper's inherent limit (the tradeoff for staying offline).
- **Native crash fix is a timing race — NOT verified live.** faulthandler is still armed; if it recurs, read `crash.log`, find the faulting thread, and check whether ANOTHER GC-running thread (e.g. the pystray MAIN thread) needs CoInitialize too.
- **Commit** — everything uncommitted; Jeremy signs off live before any commit; do NOT push. Fold into the v1.1 live-fix batch.
- **On sign-off: update `C:\Users\jemer\code\localflow\CLAUDE.md`** build-status line (add TTS read-aloud) and the test count (now 247; the CLAUDE.md still says 182, the 13 Jul handoff says 233).
- Interim personal-assistant scripts (`tts_speak.py`, `read_selection.py` under `C:\Users\jemer\code\personal assistant\tools\`) can be retired now that LocalFlow owns read-aloud; left in place (reversible). The running listener was killed this session.

## Plan
No dedicated plan file drove this session (feature build + live debugging off the 14 Jul TTS handoff). Standing plan: `C:\Users\jemer\code\localflow\plans\v1.1-plan.md`.

## Next steps
1. Jeremy restarts LocalFlow (quit tray, relaunch) to load TTS + the comtypes crash fix. Confirm `localflow.log` shows "read-aloud hotkey live -> ctrl+alt+r".
2. Live-test narrate: hotkey on a selection, pill "Narrate selection", nothing-selected silence, and specifically re-try the pill-menu path that crashed before (to confirm the CoInitialize fix holds).
3. If a voice/pronunciation complaint recurs, offer ryan-high; if a crash recurs, read `crash.log` and extend CoInitialize to whatever thread faults next.
4. On live sign-off: commit the batch (confirm split, do NOT push), update v1.1 milestone log + CLAUDE.md build-status + test count (247).

## Next step (for the next session after /clear)
Next step: read C:\Users\jemer\code\localflow\plans\handoff-2026-07-17-tts-narrate-and-comtypes-crash-fix.md, then have Jeremy restart LocalFlow and live-test narrate (hotkey + pill menu, incl. the previously-crashing pill path) before committing.
