# Session Handoff — Bug hunt pass: quiet-mic auto-gain, repetition collapse, silent-failure feedback, hallucination filter fix

## Where it started
User asked (running model `claude-fable-5[1m]`, then switched to `claude-opus-4-8[1m]`) to go through the LocalFlow code, find bugs, and make things better/faster. That opened into three concrete user-reported symptoms, fixed in sequence across the session: (1) "sometimes when I record nothing gets output", (2) "why is the mic soft for LocalFlow but fine for meetings, and the failure is invisible — after I press stop, nothing happens, no output, no text copied, no history", (3) "can we auto-control gain like meeting apps, and fix repeated words/phrases/sentences". Ponytail mode (lazy senior dev) was active the whole session.

## Important discoveries and decisions
- **Root cause of "nothing gets output" was a real bug in the hallucination filter, not just the mic.** `drop_trailing_hallucinated_chunks` judged the LAST transcript chunk by chars-per-second over its timestamp span. OpenVINO sometimes reports the 30s decode-window end instead of the real speech end, so a genuine ~12s sentence looked like 85 chars over 30s, got judged "too slow = silence artifact", and the WHOLE transcript was dropped → nothing pasted. Confirmed in the live log: at 15:50 on 2026-07-11 the user literally dictated the complaint itself and it was eaten (`"sometimes when i record it still didn't show up as text nothing gets you Thank you." -> ''`).
- **The mic really is very quiet (this is a hardware/Windows-level issue, not fixable in code alone).** Every recent recording in the log peaks at 0.035–0.084 (3–8% of full scale). Meeting apps (Teams/Zoom) run AGC and even move the Windows mic slider, so the same mic sounds fine there. LocalFlow reads the raw stream. Digital normalize (up to 20x) amplifies hiss along with speech, which is what makes Whisper stutter garbage ("you you you", "for for for"). The fix has to raise the CAPTURE level, hence auto-gain.
- **The silent failures WERE in the log** (that's how the eaten dictation was found) but had zero user-visible trace: no paste, no history (history only records dictations that actually pasted, by design), no pill cue.
- **Repeated-word loops and quiet audio are the same disease.** The repetition collapser cleans the symptom; auto-gain treats the cause. Expect both to improve together once the Windows slider settles.
- **Log location:** `%LOCALAPPDATA%\LocalFlow\localflow.log` (i.e. `C:\Users\jemer\AppData\Local\LocalFlow\localflow.log`). It records every dictation's path (capture peak, VAD spans, hallucination filter before/after, inject). This is the primary diagnostic tool — read it first for any "it didn't work" report.

## Decisions locked + what shipped
Four fixes across two model sessions. All 227 tests pass (`.venv\Scripts\python.exe -m pytest tests -q`).

**Earlier in session (first bug-review pass, 213→215 tests):**
- **Cap warning moved off the real-time audio thread** — `C:\Users\jemer\code\localflow\localflow\desktop\capture.py`. `_on_chunk` was calling `log.warning` from the PortAudio callback when a recording hit `max_utterance_s` — the exact file-I/O-on-audio-thread the module forbids (causes the input overflows that make Whisper hallucinate). Now sets `self._capped` and `stop()` reports it, same pattern as the overflow counter.
- **Stale noise calibration survived a Settings mic change** — `C:\Users\jemer\code\localflow\localflow\desktop\app.py` `hook_input_device`. Pinning a different mic swapped the stream but left the previous mic's calibrated noise floor attached. The default-mic hot-swap path already rebuilt the preprocessor (micwatch `on_switch`), but the Settings pin path didn't. Now calls `hook_preprocess()` after the swap and logs honestly when the swap was deferred (recording live).

**Hallucination filter fix (215 tests):**
- **`drop_trailing_hallucinated_chunks` only drops TINY tails now** — `C:\Users\jemer\code\localflow\localflow\core\hallucination.py`. Added `_droppable_text()` gate: timing tests (started-past-audio, too-slow-cps) can only ever drop a chunk of ≤4 words, or one word stuttered (`len(set(words)) <= 2`). A long varied sentence is kept no matter how broken its timestamps are. Two new tests including the user's exact eaten dictation.

**Silent-failure feedback (216 tests):**
- **Pill flash on no-output dictations** — `overlay.py` gained `flash(message)` (any-thread, queues `("flash", msg)`); `app.py` gained `on_no_output` hook + `_notify_no_output()`, wired to `overlay.flash` in `_run()`. Fires on every no-output path in `_finish_dictation`: "Nothing heard - no text" (silence/gated/garbage) or "Mic gave no audio - check the mic" (zero frames). Flash survives the worker's trailing `set_state("idle")` via an `end_flash()` helper + a guard in overlay `apply()`. Never draws over rec/menu/loading.

**Auto-gain + repetition collapse (this session's main ask, 227 tests):**
- **`collapse_repeats(text)`** — `C:\Users\jemer\code\localflow\localflow\core\hallucination.py`. Collapses immediate repetitions to ONE instance: a single word run of 3+ ("for... for... for..." → "for"), or a 2–6 word phrase repeated back-to-back ("do anything do anything" → "do anything"). Case/punctuation-insensitive. A single doubled word ("had had", "gets gets") is deliberately KEPT (real English). Runs per-segment in `pipeline.py` (between chunk-drop and `strip_hallucinations`) AND once more on the joined text in `app.py` `_finish_dictation` (catches a repeat straddling a Tier-1 segment cut). Rides the `filter_hallucinations` toggle. Exported from `core/__init__.py`.
- **Auto mic gain** — new file `C:\Users\jemer\code\localflow\localflow\desktop\micgain.py`. `recommend_volume(peak, current)` (pure, unit-tested): peak < 0.15 → raise Windows input level +0.10; peak > 0.99 → lower −0.10 (floor 0.05, ceiling 1.0); peak <= 0 (dead mic) → None (never cranks on silence). `adjust_for_peak(peak)` applies it to the default capture endpoint via pycaw `IAudioEndpointVolume`. App side: `app.auto_gain` flag (armed from `cfg.auto_gain` in `_run`), `_maybe_adjust_gain(peak)` called at the end of `_finish_dictation` — ONLY when following the system default mic (not pinned, not fallback-active), only after a dictation that decoded speech. Peak is tracked through the worker via `_pending_peak` (max abs across segments). Config key `auto_gain: bool = True` added to `config.py` (both the Pydantic model and the `DEFAULT_CONFIG_TOML` template).

## Key files for next session
- `C:\Users\jemer\code\localflow\localflow\core\hallucination.py` — the two filter fixes (`_droppable_text`, `collapse_repeats`) live here; the trickiest logic of the session.
- `C:\Users\jemer\code\localflow\localflow\desktop\micgain.py` — new auto-gain module (pure policy + pycaw COM wrapper).
- `C:\Users\jemer\code\localflow\localflow\desktop\app.py` — `_finish_dictation`, `_maybe_adjust_gain`, `_notify_no_output`, `_pending_peak` plumbing, `hook_input_device` calibration fix.
- `C:\Users\jemer\code\localflow\localflow\desktop\overlay.py` — `flash()` / `end_flash()` / `apply()` idle-guard.
- `C:\Users\jemer\code\localflow\localflow\desktop\capture.py` — cap-warning-off-audio-thread fix.
- `C:\Users\jemer\code\localflow\localflow\desktop\config.py` — `auto_gain` field + toml template.
- Log: `C:\Users\jemer\AppData\Local\LocalFlow\localflow.log` — read FIRST for any live-behaviour question.
- Plan file: none drove this session (ad-hoc bug hunt). `C:\Users\jemer\code\localflow\plans\v1.1-plan.md` is the standing v1.1 plan.
- Memory files touched: none.

## Running state
- Background processes: none.
- Dev servers / ports: none.
- Open worktrees / branches: none (on `main`, uncommitted changes present — see below).

## Verification — how to confirm things still work
- `cd C:\Users\jemer\code\localflow && .venv\Scripts\python.exe -m pytest tests -q` — expect 227 passed.
- New tests specifically: `test_hallucination.py` (collapse_repeats cases + `test_chunk_filter_never_drops_a_long_real_sentence_on_bad_timestamps`), `test_micgain.py` (5 policy cases), `test_config.py::test_auto_gain_default_and_load`, `test_desktop.py::test_no_output_hook_fires_when_dictation_yields_no_text`.
- **NOT yet verified live by Jeremy.** All four fixes are code+test only. He needs to restart LocalFlow (Start Menu "LocalFlow" or `localflow-tray`) and dictate to confirm: (a) auto-gain steps the Windows mic slider up over a few dictations and logs "auto-gain: ... input volume X% -> Y%", (b) repeated words collapse, (c) a silent failure now flashes the pill, (d) real long sentences no longer vanish.

## Deferred / pending tasks + open questions
- Deferred: **auto-gain has NO Settings UI toggle** — it's config-file only (`auto_gain = false` in localflow.toml to disable). Ponytail-minimal on purpose. If Jeremy wants it in the M8 settings window, add it to `settings_ui.py` `_build_audio`/`_COERCERS`/`_read_values` + a live hook in `app.py` `live_hooks` (currently there's no `auto_gain` hook — it's read once at startup; making it live-appliable is a small add).
- Deferred: **no Settings toggle for the repetition collapse** either — it rides `filter_hallucinations`. Fine unless it ever over-collapses.
- Open: **Does auto-gain actually move the slider on Jeremy's hardware?** The pycaw `IAudioEndpointVolume` path is unit-tested only for the pure policy; the COM call is exercised live for the first time on restart. If it throws, it's a silent debug-log no-op (never breaks dictation) — check the log for "auto-gain adjustment failed".
- Open: **Should auto-gain also fire on the fallback mic?** Currently skipped (fallback_active guard) — the reasoning is the fallback mic is a temporary hunt, not the user's choice. Revisit if Jeremy lives on a fallback mic often.
- Reminder given to Jeremy: recalibrate room noise while on the Samsung headset (saved calibration is for the Intel array and doesn't apply cross-mic — the log shows the mismatch fallback firing).

## Plan
- No plan file drove this session. The standing plan is `C:\Users\jemer\code\localflow\plans\v1.1-plan.md` (v1.1 M6–M10, all code-complete). This session was ad-hoc bug-fixing on top of committed v1.1.
- Overarching thread: make dictation reliable on a quiet mic. Two-pronged — treat the cause (auto-gain raises capture level → better SNR → fewer garbage decodes) and the symptom (collapse_repeats + tighter hallucination filter clean what garbage still gets through), plus make failures visible (pill flash).

## Next steps
1. **Jeremy live-tests the four fixes after a restart** (see Verification). This is the gate before committing.
2. **Commit** — the working tree has uncommitted changes across capture.py, app.py, overlay.py, hallucination.py, pipeline.py, config.py, core/__init__.py, micgain.py (new), micgain test (new), and edits to test_hallucination.py / test_config.py / test_desktop.py. Suggested message theme: "fix(v1.1): quiet-mic auto-gain, decode-loop repetition collapse, tighter trailing-hallucination filter, visible no-output feedback". Nothing has been committed this session.
3. If auto-gain works well, consider surfacing it (and current mic level) in the Settings window so Jeremy can see/toggle it without editing the toml.
4. If the quiet-mic problem persists even with auto-gain, the deeper lever is Windows mic Boost / a hardware gain check — auto-gain only moves the software input scalar (0–100%), not the +dB boost.
