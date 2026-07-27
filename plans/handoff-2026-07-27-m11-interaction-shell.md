# Session Handoff — v1.2 M11 interaction shell (tap/hold + keyboard suppression + Esc cancel)

## Where it started
Fresh session after the STT-research handoff. Jeremy said: read
`c:\Users\jemer\code\localflow\plans\handoff-2026-07-27-stt-research-v12-plan.md`,
then build M11 of `plans/v1.2-live-typing-plan.md`. M11 is the de-risking
milestone: tap-to-toggle + hold both work, the physical keyboard goes dead
while dictating, Esc cancels. No streaming yet — output is still v1.1
paste-on-release. Ponytail mode (full) was active for the whole session.

## Important discoveries and decisions
- The plan called for the `keyboard` PyPI lib (suppress=True hooks). Rejected:
  it is a NEW dependency for something **pynput already does**.
  `keyboard.Listener(win32_event_filter=fn)` + `listener.suppress_event()`
  gives system-wide per-key suppression. Verified against the installed
  pynput source (`_util/win32.py` SystemHook returns 1 on SuppressException;
  `keyboard/_win32.py` `_convert` calls the filter before posting).
- The filter has THREE outcomes, and the distinction is load-bearing:
  `True` = key reaches the app AND our on_press/on_release; `False` = the app
  only, our callbacks skipped; `suppress_event()` = nobody gets it.
- Because `suppress_event()` raises before pynput posts the event, a
  suppressed hotkey NEVER reaches on_press/on_release. So while suppressing,
  the hotkey and Esc must be **dispatched from inside the filter**. That is
  the one genuinely awkward part of the design; it is deliberate.
- Injected events (`LLKHF_INJECTED | LLKHF_LOWER_IL_INJECTED` = 0x10|0x02)
  must return `False`, not `True`: the app has to receive our typed text and
  our synthetic modifier release, but our own hotkey handlers must NOT react
  to them. Without this the synthetic Alt-up at dictation start would fire
  on_release(alt) and instantly end the dictation.
- Suppressing the combo keys is REQUIRED, not optional. A physically held Alt
  auto-repeats; those repeats would keep re-asserting Alt in the focused app
  and undo the one-shot modifier clear, breaking M13's mid-hold typing.
- The hook thread runs under Windows' ~300ms LowLevelHooksTimeout. Dispatching
  from the filter is no worse than v1.1: pynput's `_process` already runs
  on_press/on_release on the SAME thread that owns the hook, so the exposure
  is unchanged. Noted in the code comment; do not "fix" it with threads
  without measuring.
- `_combo_vk_map` was checked empirically for alt+`, ctrl+space, f9 and
  ctrl+shift+d — all four map completely. (An earlier worry that
  `canonical(Key.space)` would not match the parsed combo was wrong:
  `HotKey.parse('<space>')` already yields the canonical KeyCode.)
- `VkKeyScanW` needs explicit `argtypes = (ctypes.c_wchar,)`. Without it
  ctypes passes a str as a pointer and the call raises.

## Decisions locked + what shipped
- `TapHoldGate` (pure state machine) —
  `c:\Users\jemer\code\localflow\localflow\desktop\app.py`. Events:
  `key_down(now)`, `key_up(now)`, `click()`, `cancel()` -> `START | STOP |
  CANCEL | NONE`. `TAP_MAX_S = 0.3`. A tap latches recording on; the second
  tap ends it on the DOWN (its up is swallowed via `_swallow_up`). No OS, no
  clock of its own — the caller passes the timestamp.
- The gate also swallows the key-up of a REFUSED start (mic test owns the
  recorder). This **replaced and deleted** `app._hotkey_suppressed`.
- `PushToTalkListener` gained `suppress: threading.Event | None` and
  `on_cancel`, plus `_combo_vk_map()` and `_win32_filter()` (same file). The
  filter fails OPEN on any exception — a bug there must never leave the
  keyboard dead. Hooks die with the process, so a crash cannot either.
- `LocalFlowApp` split: `on_hotkey_down/up`, `toggle_recording` and
  `cancel_dictation` now all go through `_dispatch(action)` ->
  `_start_dictation()` / `_stop_dictation()` / `_cancel_dictation()`.
  `_start_dictation` returns False when refused and the caller rolls the gate
  back.
- `_grab_keyboard()` / `_release_keyboard()` on the app. `suppress_keys=None`
  (tests, embedders) means no hook and NO synthetic keys are ever sent;
  `_run()` passes a real `threading.Event()`.
- `inject.clear_modifiers()` —
  `c:\Users\jemer\code\localflow\localflow\desktop\inject.py`. One synthetic
  release per currently-held modifier at dictation start. `_MODIFIER_VKS` is
  now derived from the new `_MODIFIER_KEYS` tuple (no duplication).
- Esc cancel: `app.cancel_dictation()` ALWAYS clears suppression (escape
  hatch, even if the gate thinks nothing is running), drops the audio, and
  queues `_Segment(final=True, cancelled=True)`. `_handle_segment` sees
  `cancelled` and resets the pending accumulation without pasting — this is
  what stops a cancelled Tier-1 dictation leaking its segments into the next
  one. Segments already on the queue are still transcribed, then discarded
  (accepted waste).
- `_Segment` gained a `cancelled: bool = False` field.
- Tests — `c:\Users\jemer\code\localflow\tests\test_desktop.py`. New helper
  `_hold_mode(app)` sets `tap_max_s=0` so the existing down/up pairs still
  mean "one hold dictation" without a real 0.3s wait; applied to `_bare_app`
  and the six inline `LocalFlowApp(...)` constructions in the worker tests.
  Six new tests: gate hold, gate tap-latch, gate cancel/click, app-level
  tap-toggle, Esc cancel, suppression lifecycle, and filter routing (the
  filter test asserts `SystemHook.SuppressException` is raised).
- Build log appended to the plan file under "M11 build log (27 Jul 2026) -
  CODE COMPLETE, live DoD pending Jeremy".

## Key files for next session
- Plan file: `c:\Users\jemer\code\localflow\plans\v1.2-live-typing-plan.md` —
  read FIRST. M12 and M13 are next, with their own DoD. The M11 build log is
  inline above the M12 section.
- `c:\Users\jemer\code\localflow\localflow\desktop\app.py` — `TapHoldGate`,
  `PushToTalkListener._win32_filter`, `_dispatch`/`_start_dictation`/
  `_stop_dictation`/`_cancel_dictation`, `_grab_keyboard`. M12's preview loop
  must respect `app.engine_lock` and `app._produce_lock` exactly as the
  segmenter does.
- `c:\Users\jemer\code\localflow\localflow\desktop\segmenter.py` — the exact
  pattern M12's `livetyper.py` preview loop copies (private VAD, snapshot-only
  reads, emit under the app's produce lock).
- `c:\Users\jemer\code\localflow\docs\stt-streaming-research.md` — the
  LocalAgreement-2 reasoning behind M12's `commit_prefix`.
- `c:\Users\jemer\code\localflow\plans\handoff-2026-07-27-stt-research-v12-plan.md`
  — the previous session's handoff (research + how the plan was locked).
- Memory files touched: none this session.

## Running state
- Background processes: none
- Dev servers / ports: none
- Open worktrees / branches: main, no worktrees. Nothing committed this
  session — M11 is uncommitted alongside the pre-existing uncommitted v1.1
  work already in the tree.

## Verification — how to confirm things still work
- `c:\Users\jemer\code\localflow\.venv\Scripts\python.exe -m pytest tests -q`
  — 256 pass (baseline before this session was 248; the older 182 figure in
  CLAUDE.md and the previous handoff is stale).
- Combo vk-map spot check (Windows only):
  `c:\Users\jemer\code\localflow\.venv\Scripts\python.exe -c "from localflow.desktop.app import PushToTalkListener; p=PushToTalkListener('alt+`',lambda:0,lambda:0); print({hex(k):v for k,v in p._vk_keys.items()})"`
  — expect 0x12 / 0xa4 / 0xa5 (alt) and 0xc0 (backquote).

## Deferred / pending tasks + open questions
- Pending: M11's LIVE definition of done, which only Jeremy can run. While
  dictating, physical typing must do nothing in the focused app; alt+` tap
  must toggle on/off; a hold must still paste on release; Esc must cancel with
  no text; and the keyboard must be alive again afterwards INCLUDING after a
  forced kill of the tray process.
- Deferred: no `live_typing` config key yet — M11 changes behaviour
  unconditionally. The plan puts the toggle in M13. If live testing goes badly
  this is the first thing to add.
- Deferred: the read-aloud `GlobalHotKeys` listener still fires during a
  dictation. Its hook is installed later so it sits earlier in the chain and
  sees the key before our suppression. Known, low priority, not in the plan.
- Deferred (from the plan, unchanged): Parakeet TDT OpenVINO A/B on the NPU,
  beam-size/decoding knobs, AlignAtt early stopping.
- Open: none. Jeremy asked what "hold still pastes" meant — answered: it is
  M11's regression check, streaming for BOTH modes arrives in M13. He then
  asked for a handoff before starting M12.

## Plan
- `c:\Users\jemer\code\localflow\plans\v1.2-live-typing-plan.md` — M11
  interaction shell (DONE, code-complete), M12 streaming commit engine
  (log-only: pure `localflow/core/live.py` + a `livetyper.py` preview loop),
  M13 live injection + final patch + `live_typing` config. Live sign-off DoD
  lives in M13.

## Next steps
Build M12. Two pieces: (1) `localflow/core/live.py`, pure and unit-tested —
`commit_prefix(prev_words, new_words, committed_len) -> int` (LocalAgreement-2
longest common word-prefix, never retreats) and `patch_plan(typed, final) ->
(n_backspaces, retype)` for M13's final patch. (2)
`localflow/desktop/livetyper.py`, a Segmenter-style ~1s preview loop that
snapshots the recorder's uncommitted buffer, does
`engine_lock.acquire(blocking=False)` and SKIPS the tick if busy (never
queues), fast-decodes with no timestamps and no hallucination filter, feeds
`commit_prefix`, and LOGS newly committed words — no injection. On a Tier-1
segment cut the segment's raw text commits wholesale and the window resets
past the cut. Follow the tdd skill. Then M13.

## Next step (for the next session after /clear)
Next step: read
`c:\Users\jemer\code\localflow\plans\handoff-2026-07-27-m11-interaction-shell.md`,
then build M12 of `plans/v1.2-live-typing-plan.md`.
