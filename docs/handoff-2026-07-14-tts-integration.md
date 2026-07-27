# Handoff — Integrate text-to-speech into LocalFlow (from the personal-assistant session, 2026-07-14)

## Where it started
Jeremy wants to cut reading fatigue when working with Claude. Today's session (in the
personal-assistant repo) built a working interim TTS setup: Claude's replies are spoken
aloud after every turn, and a global hotkey reads any selected text in any app. Jeremy
now wants this capability to live INSIDE LocalFlow, not as separate scripts, so one tool
handles voice in (dictation, already built) and voice out (TTS, this handoff).

## What exists and works today (interim, in the personal-assistant repo)
All committed on `main` of the personal-assistant repo (commits f1da0e2, 7b8fbb3).

- `C:\Users\jemer\code\personal assistant\tools\tts_speak.py` — the speech engine glue.
  Three modes:
  - no args: Claude Code Stop-hook mode. Reads hook JSON on stdin, extracts the last
    assistant message from the transcript JSONL, strips markdown (code blocks, links,
    formatting chars), caps at 1500 chars, then re-spawns itself detached with `--play`.
  - `--play FILE`: kills any previous speech (pid file `%TEMP%\claude_tts.pid`,
    taskkill /T), synthesizes with `python -m edge_tts`, plays with
    `ffplay -nodisp -autoexit`, falls back to offline Windows SAPI on any failure.
  - `--clipboard`: speaks the clipboard (used by the hotkey listener).
- `C:\Users\jemer\code\personal assistant\tools\read_selection.py` — background
  listener (pythonw). Registers global hotkey Ctrl+Alt+R via user32.RegisterHotKey,
  simulates Ctrl+C to copy the current selection (must inject an Alt KEYUP first or the
  app receives Ctrl+Alt+C), waits 150ms for the clipboard, then spawns
  `tts_speak.py --clipboard`. Second press kills the speech process tree.
  Caveat: it overwrites the clipboard.
- Config: `C:\Users\jemer\.claude\tts.json` -> `{"voice": "en-US-AndrewNeural",
  "rate": "+20%"}`. Re-read every utterance, no restart needed. Jeremy's requirements:
  neutral, fluid, natural, MALE voice; faster than normal; speed user-controllable.
- Global Claude hook: `C:\Users\jemer\.claude\settings.json` has a Stop hook (async)
  running tts_speak.py, so every Claude session in every repo speaks its replies.
- Kill switch: create `C:\Users\jemer\.claude\tts_off` to silence everything.

## The one decision Jeremy must make before building
The current engine is **edge-tts**: Microsoft's free neural voices, but ONLINE — the
reply text is sent to speech.platform.bing.com. LocalFlow's hard constraint is
"fully local, no text leaves the device". Two options:

1. **Local neural TTS (fits the PRD).** Piper (ONNX, fast on CPU, decent male voices)
   or Kokoro-82M (better quality, ONNX, could run via OpenVINO on the Arc iGPU like
   Whisper does). Fully local, matches the repo's ethos and the existing OpenVINO
   stack. Quality is good but a notch below edge-tts's Andrew.
2. **Keep edge-tts as an explicit, documented online exception** with a toggle.
   Jeremy accepted edge-tts today for the interim setup, but that acceptance was for
   standalone scripts, not for code inside LocalFlow. Do not silently carry the
   network call into a repo whose CLAUDE.md forbids text leaving the machine.

Ask Jeremy which he wants before writing code. Recommendation: option 1 (Kokoro via
OpenVINO first, Piper as the simpler fallback), keeping SAPI as the offline last resort.

## Suggested integration shape (respect the core seam)
- `localflow/core`: TTS engine, pure text in -> audio out (mirror of the STT seam).
  No clipboard, no keyboard, no playback in core.
- `localflow/desktop`: playback, a Read Aloud hotkey (reuse the existing hotkey infra
  and the copy-selection trick from read_selection.py), tray toggle, and Settings
  additions: voice picker, speech-speed slider (Jeremy explicitly wants speed control),
  read-aloud hotkey binding.
- Expose a tiny CLI entry point, e.g. `localflow-speak` reading text from stdin or a
  file. Then the Claude Stop hook glue (which is Claude-specific, not LocalFlow's
  concern) stays in the personal-assistant repo but shells out to `localflow-speak`
  for synthesis+playback. Clean split: Claude glue there, speech engine here.
- Port these behaviours, they came from live debugging today: one-voice-at-a-time via
  pid file kill; markdown stripping before speech; Alt-KEYUP-before-Ctrl+C in the
  copy-selection simulation; press-hotkey-again-to-stop.

## Cleanup once LocalFlow's TTS is live
- Kill the interim listener: `Get-CimInstance Win32_Process -Filter "Name =
  'pythonw.exe'" | Where-Object { $_.CommandLine -like '*read_selection*' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }` — it holds Ctrl+Alt+R.
- Point the Stop hook in `C:\Users\jemer\.claude\settings.json` at `localflow-speak`
  (keep the transcript-parsing part of tts_speak.py, replace its speak() internals).
- Retire `read_selection.py`; keep or retire `tts_speak.py` depending on the CLI split.

## Verification
- Interim setup: select text anywhere, Ctrl+Alt+R -> Andrew voice reads it, press
  again -> stops. Any Claude reply in any repo -> spoken.
- After integration: same two behaviours, but the speaking process is LocalFlow's and
  (if option 1) works with WiFi off.

## Open questions for Jeremy
- Local TTS (Piper/Kokoro) or keep online edge-tts inside LocalFlow? (see above)
- Read Aloud hotkey: keep Ctrl+Alt+R or pick something nearer alt+` for muscle memory?
- Auto-start on boot: the interim listener has NO boot persistence (permission denied,
  Jeremy has not approved). LocalFlow's tray app presumably already starts with
  Windows, which would solve this for free — confirm.

## Next steps
1. Ask Jeremy the engine question (local vs online).
2. Spike the chosen engine on this machine (male voice sample + speed control), get
   Jeremy's ear-approval on the voice BEFORE wiring UI.
3. Build core TTS seam -> desktop hotkey/playback -> settings UI, per the repo's usual
   milestone style, then do the cleanup list above.
