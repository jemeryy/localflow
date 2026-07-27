# Session Handoff — Tier 1 (VAD-segmented streaming transcription) built + tests green, live DoD pending

## Where it started
Jeremy said: "read plans/handoff-2026-07-06-longdictation-resets-deepfilternet.md
and build Tier 1." Tier 1 was the pre-designed, Jeremy-approved next step from that
handoff: VAD-segmented, pipelined transcription so you can talk indefinitely and the
wait on release stays short. Design was already agreed in that prior session; this
session was pure build + test. Not committed (Jeremy's live-test-first workflow).

## Important discoveries and decisions
- **Segmentation is orthogonal to `max_utterance_s` and needs no special-casing.**
  The recorder keeps `max_seconds=cfg.max_utterance_s` (300s). Because the segmenter
  drains the buffer every ~30s, `_frames` never grows near the cap, so the cap never
  trips while segmenting (talk indefinitely). With segmentation off (0), the cap
  applies exactly as before. Confirmed by construction, not just asserted.
- **The segmenter MUST use its own Silero VadGate, not the worker's.**
  `get_speech_timestamps` mutates the model's internal state, so sharing one gate
  across the segmenter thread and the worker thread would corrupt it. Two gates =
  two ONNX sessions; VAD scan (CPU) then runs parallel to STT (GPU), no lock needed.
- **Ordering (mid-cut vs on-release final) is provably safe via one lock.**
  `emit_segment` and `on_hotkey_up` both do drain+enqueue under `app._produce_lock`.
  A new dictation's id only advances in `on_hotkey_down`, and a new dictation can only
  start after `on_hotkey_up` (which needs `_produce_lock`). So while `emit_segment`
  holds the lock, the id can't move and its check-then-drain is race-free. The slow
  work (snapshot + resample + VAD) happens OUTSIDE the lock; only the numpy-slice
  drain + queue put are inside, so the pynput hotkey thread never blocks meaningfully.
- **Cut lands at the MIDPOINT of the LAST qualifying silence gap** (min_gap 0.6s,
  min_lead 5s, edge_guard 1s). edge_guard keeps the live edge (maybe still speech)
  uncut. Verified against the real recorded utterance: cut landed in clean silence
  (trailing 25ms RMS 0.0000) and drained segments reconstruct the buffer exactly, at
  both 16kHz and an upsampled 48kHz mic rate.
- **Default `segment_seconds = 30` (ON).** Chosen so Jeremy can live-test immediately;
  0 disables it (single-shot on release). This is a core-behaviour change but only
  affects holds > 30s (short ones are byte-identical to before).

## Decisions locked + what shipped (NOT committed)
- **Pure core cut-finder** — `c:\Users\jemer\code\localflow\localflow\core\segment.py`
  new: `find_cut_point(spans, total, sample_rate, min_lead_s, min_gap_s,
  edge_guard_s)`. Exported from `localflow.core` (`__init__.py`). Imports only
  `.audio` (stays pure; AT-7 purity test still green).
- **VadGate.speech_spans()** — `...\localflow\core\vad.py`: raw (start,end) spans for
  the segmenter, separate from `trim()`.
- **Recorder drain/snapshot** — `...\localflow\desktop\capture.py`:
  `buffered_frames()`, `snapshot()` (copies the chunk list under the lock, concats
  outside it), `drain_segment(up_to_frame)` (returns the front, keeps the contiguous
  tail; returns empty if not recording so a stop() race emits nothing; resets `_capped`).
- **Segmenter thread** — `...\localflow\desktop\segmenter.py` new: polls every 1s while
  recording; once buffer >= `segment_seconds` does snapshot -> `ensure_16k_mono` ->
  `vad.speech_spans` -> `find_cut_point` -> maps the 16k cut back to native frames ->
  `app.emit_segment`. No silence found past `segment_seconds`+15s -> force a cut at the
  target (accepts a rare mid-word split). `set_segment_seconds()` for live-apply.
  Constants `POLL_INTERVAL_S=1.0`, `HARD_EXTRA_S=15.0`.
- **Worker refactor** — `...\localflow\desktop\app.py`: new `_Segment` dataclass (mid
  vs final). `on_hotkey_down` bumps `_dictation_id`; `on_hotkey_up` enqueues a final
  `_Segment` under `_produce_lock`; `emit_segment(cut, id)` atomically drains+enqueues a
  mid segment. Worker `_drain` -> `_handle_segment` accumulates raw texts
  (`_pending_texts` etc.) under a `dictation_id`; `_transcribe_segment` runs raw STT
  (cleanup=None) under `engine_lock`; `_finish_dictation` joins on the final and runs
  cleanup + dictionary + inject + history ONCE. Legacy 3-tuple queue puts still work
  (treated as a one-segment final) so the old warm-gate test still passes. Wiring:
  builds `app.segmenter` after `start_worker`; arms its private VadGate + starts the
  thread in `warm_in_background` after `app.warm.set()`; `hook_segment_seconds` +
  `hook_vad` updates the segmenter; `apply_settings` dispatches `segment_seconds` ->
  `hooks["segment"]`; segmenter stopped in the `_run` teardown finally.
- **Config** — `...\localflow\desktop\config.py`: `segment_seconds: float = 30.0` with
  validator (0 or 10-600) + `DEFAULT_CONFIG_TOML` line. Also added to the repo-root
  `c:\Users\jemer\code\localflow\localflow.toml`.
- **Settings UI** — `...\localflow\desktop\settings_ui.py`: "Segment length (s)"
  spinbox in Behaviour (0-600, step 15), `_read_values` + `_reset_widgets_to` +
  `_COERCERS` + `RESET_ALL_KEYS` all include `segment_seconds`.
- **Tests 163 -> 182** — new `c:\Users\jemer\code\localflow\tests\test_segment.py`
  (9 cut-point cases); additions to `tests\test_desktop.py` (drain/snapshot/emit-guard/
  multi-segment-join/cleanup-once/segmenter x8, plus `segment` in the apply-dispatch
  test); `tests\test_config.py` (default+validation); `tests\test_settings_ui.py`
  (coerce + default_tuning).
- **Docs** — `...\plans\v1.1-plan.md` milestone log (new top entry), `...\CLAUDE.md`
  build status (182 tests + Tier 1 paragraph).

## Key files for next session
- Plan file: `c:\Users\jemer\code\localflow\plans\v1.1-plan.md` — read the milestone
  log TOP entry (Tier 1) first.
- `c:\Users\jemer\code\localflow\localflow\desktop\app.py` — `_Segment`,
  `emit_segment`, `_handle_segment`/`_finish_dictation`, `_produce_lock`,
  `_dictation_id`, and the `_run` wiring (segmenter build + arm + teardown).
- `c:\Users\jemer\code\localflow\localflow\desktop\segmenter.py` — the poll loop.
- `c:\Users\jemer\code\localflow\localflow\core\segment.py` — `find_cut_point`.
- `c:\Users\jemer\code\localflow\localflow\desktop\capture.py` — `drain_segment`,
  `snapshot`, `buffered_frames`.
- Prior handoff (context for the whole long-dictation arc):
  `c:\Users\jemer\code\localflow\plans\handoff-2026-07-06-longdictation-resets-deepfilternet.md`
- Integration smoke (throwaway, safe to delete):
  `C:\Users\jemer\AppData\Local\Temp\claude\c--Users-jemer-code-localflow\2a29de9a-786b-4ec4-a55f-90eb6870c35e\scratchpad\tier1_integration.py`
- Memory files touched: none.

## Running state
- Background processes: none. (All bash commands this session ran in the foreground.)
- Dev servers / ports: none.
- The LocalFlow tray app: NOT restarted this session — any running instance is on an
  OLD build without Tier 1. Must be restarted to live-test (command below).
- Open worktrees / branches: none (on `main`, working tree DIRTY — Tier 1 uncommitted
  on top of the still-uncommitted 6 Jul long-dictation/reset/DF changes).

## Verification — how to confirm things still work
- `c:\Users\jemer\code\localflow\.venv\Scripts\python.exe -m pytest tests -q` — expect
  182 passed.
- Tier 1 integration smoke (real Silero, no mic/GPU):
  `c:\Users\jemer\code\localflow\.venv\Scripts\python.exe "C:\Users\jemer\AppData\Local\Temp\claude\c--Users-jemer-code-localflow\2a29de9a-786b-4ec4-a55f-90eb6870c35e\scratchpad\tier1_integration.py"`
  — prints "TIER1 INTEGRATION PASS".
- Restart the app to live-test:
  `c:\Users\jemer\code\localflow\.venv\Scripts\python.exe -m localflow.desktop.app`
  (or the Start Menu "LocalFlow" tray entry). Log:
  `%LOCALAPPDATA%\LocalFlow\localflow.log`.

## Deferred / pending tasks + open questions
- **Uncommitted.** Tier 1 + the prior 6 Jul long-dictation/reset/DeepFilterNet changes
  are all uncommitted on `main`. Suggested split when Jeremy is happy: commit the 6 Jul
  batch first, then a `feat: Tier 1 VAD-segmented streaming` commit. Confirm with him.
- **LIVE DoD pending Jeremy** (needs an app restart onto this build):
  1. Hold the hotkey and talk past ~30s, keep talking — the log shows
     `segment cut at ...s` lines DURING the hold; on release the FULL joined text
     pastes with a SHORT wait (not the old ~14s for a long buffer).
  2. A short (<30s) dictation still pastes normally with no behaviour change.
  3. Settings -> Behaviour -> Segment length = 0 -> Apply -> a long hold transcribes
     in one shot on release (old behaviour).
  4. Sanity: segment seams read cleanly (no dropped/duplicated words at the joins).
- **Open (tuning):** `segment_seconds` default 30 and `HARD_EXTRA_S` 15 are first-pass.
  If Jeremy finds 30s too long/short a wait, or seams awkward, tune live.
- Deferred (unchanged from the prior handoff, after Tier 1 lands): the real-mic
  spectral-denoise A/B to pick the default `denoise_strength`; optional
  `pip uninstall torch torchaudio` from `.venv` (~2GB, unused); the `.tmp\dfspike`
  venv can be deleted.

## Plan
- Overarching plan: `c:\Users\jemer\code\localflow\plans\v1.1-plan.md`. Tier 1 is new
  scope beyond M6-M10; its milestone-log entry is now the top entry. No separate Tier 1
  plan file.

## Next steps
1. Jeremy live-tests Tier 1 (the 4-point DoD above) on a restarted app. Watch
   `%LOCALAPPDATA%\LocalFlow\localflow.log` for `segment cut at ...` lines while holding.
2. Fold any live tuning back into `segment_seconds` / `HARD_EXTRA_S` and re-run the
   suite.
3. On sign-off, commit (confirm the commit split with Jeremy — do NOT push/tag; commit
   to `main` is the convention but he tests live first).
4. Then the deferred real-mic denoise A/B to lock the default `denoise_strength`.
