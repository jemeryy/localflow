# Session Handoff — live typing rebuilt append-only, target-locked injection, focus-scoped key lock

## Where it started
Fresh session after the google-style-interim-typing handoff. Task was the v1.2
live sign-off; Jeremy's live runs drove three rounds of feedback: (1) the
interim engine kept rewriting the sentence, keyboard fully dead, stream
followed focus; (2) after the first fix the rewrites became garbled non-words;
(3) he then dropped the revision requirement entirely: "the streaming itself
is fine, no correction — just no duplicates or extra words when I stop."
Ponytail mode (full) active.

## Important discoveries and decisions
- **Garbled rewrites root cause (round 2, the big one):** posting a raw
  WM_KEYDOWN VK_BACK to a classic edit control makes the TARGET app's own
  TranslateMessage enqueue the deleting WM_CHAR at the BACK of its queue —
  BEHIND the replacement WM_CHAR text we had already posted. Deletes executed
  after the retype; every revision compounded the corruption. Fix: backspace
  STYLE is picked by window class (`inject._keydown_deletes`): classic
  controls get WM_CHAR 0x08 directly (FIFO with the text), Chromium/Gecko
  (`Chrome_RenderWidgetHostHWND`, `MozillaWindowClass`) delete on the
  key-down itself and ignore 0x08, so they get the key pair. ponytail comment
  marks the class-sniff ceiling (WPF/Qt apps delete on neither list — extend
  the tuple if one shows up).
- **Jeremy's final contract (round 3): append-only, no correction, ever.**
  A word types once two consecutive decodes agree (reused the already-tested
  `core.live.commit_prefix`, LocalAgreement-2) and is then FINAL. The whole
  revision machinery (patch_plan use in the livetyper, freeze tail, edge
  hold, final correction patch) was DELETED. Accepted trade: words land ~1
  beat later and a misheard word stays until fixed by hand.
- **Tail ordering rule:** append-only makes order sacred. After a Tier 1 cut
  the preview PAUSES (skips decodes too) until that window's authoritative
  segment text lands and appends its missing tail; the final drain's tail
  arrives the same way (the worker now calls `on_segment_text` for the final
  segment as well). One-word overlap guard (`_norm` compare) against
  count-drift duplicates ("I'm" typed vs "I am" decoded).
- **Targeted injection (round 1, survives all rounds):** the focused control
  HWND is captured at dictation start (`inject.focused_control`, via
  GetGUIThreadInfo) and all live typing + the silence take-back are POSTED
  straight to that HWND. Stream keeps landing in the original field when
  Jeremy switches window/app. Dead target = patch dropped (never spill into
  the current focus). Limitation told to Jeremy: another tab in the SAME
  browser window shares one input surface (one Chromium HWND), so the stream
  follows the visible tab there; separate windows are fine.
- **Focus-scoped suppression (round 1):** while dictating, only keys headed
  into the target field are swallowed; the keyboard stays alive everywhere
  else. Esc cancels only from inside the target (elsewhere it keeps its
  normal meaning); the hotkey works from anywhere. No target info -> old M11
  full-lock rule (also the fallback when capture fails).
- Esc discard: `LiveTyper.close(id, discard=True)` blocks segment tails too;
  a normal stop only blocks preview decodes (tails must still land).
- The silence take-back stays (the ONE remaining correction): a dictation
  whose final produced nothing erases what it typed (`_patch_typed(typed,
  "")` -> `patch_text`).

## Decisions locked + what shipped
- `c:\Users\jemer\code\localflow\localflow\desktop\livetyper.py` — full
  rewrite (second one today): append-only committed stream. State is
  `_prev`/`_committed` for the live window, `_pending_counts` (FIFO of typed
  word counts per cut window), `_screen`, `_closed`/`_discard`.
  `on_segment_text` appends the missing tail for the oldest unreconciled
  window and is the ONLY post-release typing path.
- `c:\Users\jemer\code\localflow\localflow\desktop\inject.py` —
  `focused_control()`, `_post_backspaces` (class-sniffed style),
  `_post_text` (WM_CHAR, UTF-16 units), `_target_alive`; `live_patch` and
  `patch_text` take `target=` and post when it is set (SendInput fallback
  when None). `patch_text` skips the modifier wait on the posted path.
- `c:\Users\jemer\code\localflow\localflow\desktop\app.py` — `_live_target`
  captured in `_start_dictation` (live typing only); LiveTyper wired
  `on_patch=lambda n,t: live_patch(n,t,target=app._live_target)`;
  `PushToTalkListener(target=...)` + `_focus_on_target()` scope the key
  filter (both construction sites: startup + hook_hotkey); worker calls
  `on_segment_text` for every non-cancelled segment INCLUDING the final;
  `_finish_dictation` no longer patches (only the silence take-back);
  cancel path closes the livetyper with discard=True; "[ok] streamed" log.
- Tests: livetyper block rewritten to the append-only contract; new tests
  for both backspace styles, dead-target drop, focus-scoped filter, discard;
  `_patching_app`/stubs updated for the new signatures.
  **294 pass** (`.venv\Scripts\python.exe -m pytest tests -q`).
- Committed on `main` (NOT pushed): 42b23e5 (freeze+target+focus-scope,
  superseded in part), c9facfe (backspace FIFO fix), a619b45 (append-only).
  The two 27 Jul handoff docs from earlier sessions got committed in 42b23e5.

## Key files for next session
- `c:\Users\jemer\code\localflow\localflow\desktop\livetyper.py` — the
  append-only engine; invariant: typed words are FINAL, order is everything.
- `c:\Users\jemer\code\localflow\localflow\desktop\inject.py` — targeted
  posting; read `_post_backspaces`' docstring before touching ordering.
- Plan file: `c:\Users\jemer\code\localflow\plans\v1.2-live-typing-plan.md`
  — NOTE: its M12/M13 build logs describe designs now TWICE superseded
  (append-only committed stream -> interim revisions -> append-only again,
  now target-locked). Update after sign-off.
- Previous handoff (context for the two superseded designs):
  `c:\Users\jemer\code\localflow\plans\handoff-2026-07-27-google-style-interim-typing.md`
- Memory files touched: none.

## Running state
- Background processes: none.
- Dev servers / ports: none.
- The LocalFlow tray app IS RUNNING on the append-only build (relaunched
  from `.venv\Scripts\localflow-tray.exe` this session). Kill:
  `Get-Process localflow-tray | Stop-Process -Force`. Log:
  `%LOCALAPPDATA%\LocalFlow\localflow.log` ("live type (+...)" lines).
- Open worktrees / branches: `main` only, working tree clean.

## Verification — how to confirm things still work
- `c:\Users\jemer\code\localflow\.venv\Scripts\python.exe -m pytest tests -q`
  — 294 pass (GPU end-to-end tests need >2GB free RAM, else 2 fail on
  OpenVINO "bad allocation" — machine pressure, not a bug).
- Fast loop: `...python.exe -m pytest tests/test_live.py tests/test_desktop.py -q`
  — 117 pass.
- Live smoke: dictate into Notepad — words appear in batches ~1.5-2s behind
  speech, NOTHING is ever backspaced/rewritten, the spoken tail lands once
  ~1-2s after release, no duplicates, no "You.". Switch to another window
  mid-dictation: stream keeps landing in Notepad and the keyboard works in
  the other window.

## Deferred / pending tasks + open questions
- Pending (the whole remaining v1.2 scope): **live sign-off, Jeremy only** —
  Notepad, browser textbox, VS Code; tap + hold; Esc; switch-away typing;
  target-field lock. Three fix rounds this session; the append-only build
  has NOT yet had a full live pass.
- Pending: update `plans/v1.2-live-typing-plan.md` M12/M13 logs + project
  CLAUDE.md build-status blob to the append-only target-locked design after
  sign-off.
- Watch: cleanup LLM + dictionary no longer reach the screen for live-typed
  dictations (no correction pass) — they only shape history/last_text. If
  Jeremy wants cleanup applied live, that is a design conversation (it
  conflicts with "no correction").
- Watch: same-window browser tab switch streams into the visible tab (one
  Chromium HWND). Known, told to Jeremy, no cheap fix.
- Watch: WPF/Qt targets may delete on neither backspace style — extend
  `_KEYDOWN_DELETE_CLASSES` if a real app surfaces.
- Deferred (unchanged): Parakeet TDT A/B on NPU, turbo decoding-knob pass,
  Settings live-typing checkbox has no direct test.
- Open: none.

## Plan
- `c:\Users\jemer\code\localflow\plans\v1.2-live-typing-plan.md` — M11-M13
  code-complete on the third design iteration; only the live sign-off (and
  the post-sign-off doc updates) remain in v1.2.

## Next steps
Jeremy runs the live sign-off on the append-only build (tray already
running). Fix what surfaces. After sign-off: update the v1.2 plan's M12/M13
milestone log and the CLAUDE.md build-status blob, then consider a push.

## Next step (for the next session after /clear)
Next step: read
`c:\Users\jemer\code\localflow\plans\handoff-2026-07-27-append-only-target-locked-typing.md`,
then run the v1.2 live sign-off on the append-only build and fix what it
surfaces; after sign-off update `plans/v1.2-live-typing-plan.md` and the
CLAUDE.md build status.
