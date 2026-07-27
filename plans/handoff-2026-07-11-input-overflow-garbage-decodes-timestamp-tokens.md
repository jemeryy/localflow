# Session Handoff — LocalFlow "nothing happens" / garbage decodes: input-overflow fix + Whisper timestamp-token strip

## Where it started
Jeremy reported (dictating live into LocalFlow): (1) "sometimes when I speak it doesn't
register the text, after I stop nothing happens"; (2) "still a lot of trailing extra
words"; then mid-session it escalated to (3) "localflow is not working any more, when I
stop no text is generated"; and finally (4) a dictation pasted with a literal
`<|7.00|>` prefix ("why got this error"). No plan file drove this — it was live
debugging off the running app's logs.

## Important discoveries and decisions
- **The real failure was NOT VAD gating real speech, and NOT clipping.** Evidence from
  `%LOCALAPPDATA%\LocalFlow\localflow.log`:
  - The 30 "VAD: no speech" silence gates were almost all **Calibrate room-noise runs**
    (2s of intentional silence; a "noise profile saved" line follows each). Not failures.
  - The hallucination filter never stripped anything to empty in the older log (0 times).
  - The catastrophic case (00:08:14): a real 8s hold (VAD found **4 speech spans**)
    decoded as `'<|9.52|> you! you! you! Thank you.'` — pure repetitive garbage, 4631ms
    decode (~6x normal). Filter correctly stripped it to `''` → nothing pasted. **That
    recording had an input overflow mid-capture.**
- **Root cause of the garbage decodes = input overflow.** 25 "input overflow" events in
  the log. An overflow drops samples → punches a gap into the buffer → Whisper decodes a
  discontinuous clip as repetitive junk ("you you you"). Overflow was self-reinforcing:
  the audio callback wrote to the **log file on the real-time PortAudio thread** on every
  overflow, stalling the callback and causing the next overflow.
- **The mic is quiet, not hot.** "Waveform maxed out" (Jeremy's words) is the overlay
  drawing a low-level signal at high gain, not clipping. Post-fix log peaks are 0.06–0.12
  (6–12% of full scale). No clipping anywhere.
- **External mic probing is unreliable while the app owns the device.** A second WASAPI
  shared-mode `sd.rec()` alongside the running app returned real ambient once (peak
  0.025) then all-zeros the next run (contention/starvation). Do NOT diagnose the mic
  from a second process — instrument inside the app instead. (My concurrent probes may
  have briefly worsened a wedge; I stopped.)
- **`<|7.00|>` is a Whisper timestamp token leaking.** `filter_hallucinations=true` runs
  Whisper in `return_timestamps` mode; openvino_genai sometimes spills a raw
  `<|7.00|>` / `<|9.52|>` token into the decoded string. Nothing stripped it. The
  timestamp-chunk filter caught the trailing " you" but left the **leading** token.
- **Jeremy's steer on the two original symptoms:** Issue 1 — he rejected the quiet-speech
  theory ("waveform clearly shows sound, maxed out"), which correctly pointed away from
  VAD and toward corrupted-but-present audio. Issue 2 (trailing words) — "can just clean
  up with llm", i.e. don't over-build heuristic trailing-word filters; lean on the Qwen
  cleanup toggle. Trailing-words work was therefore DEFERRED (see below).

## Decisions locked + what shipped
All changes are uncommitted, on `main` (working tree was already dirty with the
6 Jul + Tier 1 + 10 Jul live-feedback work). App runs from the editable install, so a
restart picks up source edits with no reinstall.

- **Kill the audio-thread overflow feedback loop** —
  `c:\Users\jemer\code\localflow\localflow\desktop\capture.py`:
  `_on_chunk` no longer calls `log.warning` on status (that was file I/O on the PortAudio
  thread). It now just bumps `self._overflows` when `status.input_overflow`. New
  `self._overflows` field (init + reset in `start()`).
- **More input buffer headroom** — same file, `open()`: the persistent `sd.InputStream`
  is now created with `latency="high"` so a busy moment (GPU doing STT, overlay redraw,
  a heavy background process) can't as easily starve the callback into an overflow.
- **Per-recording diagnostics** — same file, `stop()`: logs
  `recording stopped: %.2fs captured, peak %.3f[, N input-overflow(s)]`. Peak near the
  rail = clipping; near 0 = wedged/silent endpoint; overflows > 0 = a gapped buffer.
  **Verified live: 0 overflows across four 13–30s recordings post-restart** (was 25).
- **Strip Whisper special/timestamp tokens at the source** —
  `c:\Users\jemer\code\localflow\localflow\core\stt.py`: new module-level
  `strip_special_tokens(text)` using `_SPECIAL_TOKEN = re.compile(r"<\|[^|>]*\|>")`.
  Applied in `transcribe()` to both `text = strip_special_tokens(str(result)).strip()`
  and each chunk's text (chunk text is NOT `.strip()`ped — the pipeline joins chunks and
  relies on Whisper's leading spaces to keep words apart). This is the core-seam
  chokepoint every paste routes through, so it covers the timestamp path, the no-chunk
  fallback, and the chunk-rebuild path in one place.

## Key files for next session
- `c:\Users\jemer\code\localflow\localflow\desktop\capture.py` — overflow counter,
  `latency="high"`, peak/overflow logging in `stop()`.
- `c:\Users\jemer\code\localflow\localflow\core\stt.py` — `strip_special_tokens` +
  its two call sites in `transcribe()`.
- `c:\Users\jemer\code\localflow\localflow\core\pipeline.py` — read to understand the
  filter flow: `transcribe_audio(..., filter_hallucinations=True)` requests timestamps,
  runs `drop_trailing_hallucinated_chunks` then `strip_hallucinations`.
- `c:\Users\jemer\code\localflow\localflow\core\hallucination.py` — the trailing filter
  (unchanged this session; relevant if the deferred trailing-words work resumes).
- Prior handoff (the uncommitted base this sits on):
  `c:\Users\jemer\code\localflow\plans\handoff-2026-07-10-live-feedback-startup-hallucination.md`
- Log Jeremy is running against: `%LOCALAPPDATA%\LocalFlow\localflow.log`
  (= `C:\Users\jemer\AppData\Local\LocalFlow\localflow.log`); history at
  `...\LocalFlow\history.jsonl`.
- Plan file: none for this session.
- Memory files touched: none.

## Running state
- Background processes: none. Two `run_in_background` mic-capture probes ran and
  COMPLETED (IDs `brbz1r46u`, `bb6tn77lq`) — nothing to kill.
- Throwaway probe script (safe to delete):
  `C:\Users\jemer\AppData\Local\Temp\claude\c--Users-jemer-code-localflow\8296b1e3-29ea-4310-a0e5-ec6ac8d3eebf\scratchpad\clip_probe.py`
  and its `clip_result.json`.
- Dev servers / ports: none.
- Open worktrees / branches: none (on `main`, dirty, uncommitted).
- Jeremy's LocalFlow tray app: RUNNING. It already picked up the `capture.py` fix (log
  shows the new `peak` format, 0 overflows) but NOT yet the `stt.py` token strip (the
  `<|7.00|>` leaked at 13:16, before that edit). **Needs one more restart** to load the
  token fix.

## Verification — how to confirm things still work
- `c:\Users\jemer\code\localflow\.venv\Scripts\python.exe -m pytest tests/test_hallucination.py tests/test_desktop.py -q` — expect **87 passed** (ran this session).
- Token strip self-check (ran, passed):
  `c:\Users\jemer\code\localflow\.venv\Scripts\python.exe -c "from localflow.core.stt import strip_special_tokens as s; assert s('<|7.00|> replaced by the tool').strip()=='replaced by the tool'; assert s('a <|en|><|transcribe|> b')=='a  b'; print('ok')"`
- Full suite (has a GPU end-to-end test that needs the model + a free GPU; the app holds
  the GPU, so quit it first): `...\.venv\Scripts\python.exe -m pytest tests -q` — the
  10 Jul handoff baseline was 213 passed; this session added no new test file, so still
  ~213 (self-check above is inline, not a pytest case).
- Live: after Jeremy restarts, watch the log for
  `recording stopped: ... peak ... 0 input-overflow(s)` and confirm no `<|...|>` tokens
  in pasted text and no `you you you` empties.

## Deferred / pending tasks + open questions
- **Deferred — trailing extra words.** Per Jeremy ("can just clean up with llm"): do NOT
  build more heuristic trailing-word stripping. The path forward is the Qwen cleanup
  toggle (`cleanup_enabled`, tray toggle) removing trailing junk. Cleanup does nothing
  for a garbage→empty decode (no raw text to clean), so it only ever addressed symptom 2,
  which is now secondary. Revisit only if Jeremy asks.
- **Deferred — quiet-mic gain.** Mic peaks are ~0.06–0.12; `normalize_peak` applies large
  gain (max 20x). Fine while decodes are clean, but low SNR is a latent risk. Option: ask
  Jeremy to raise the Windows input level / enable mic boost for the Intel Smart Sound
  array, OR soften normalize. Not touched — only do it if garbage returns.
- **Deferred — retry-without-timestamps fallback.** If a decode still comes back as a
  pure repetitive hallucination, a cheap safety net is to re-run `transcribe` with
  `timestamps=False` once. Skipped as YAGNI now that overflows are at 0.
- **Open — did the token fix + overflow fix fully resolve it?** Needs Jeremy's live
  confirmation after the pending restart. The `capture.py` fix is already proven live
  (0 overflows); the `stt.py` fix is only unit-verified.
- **Open — commit.** Everything is uncommitted. Jeremy tests live and signs off before
  any commit; do NOT push/tag. Suggested split when he's happy: fold this session's two
  fixes into a "v1.1 live-fix: input-overflow + timestamp-token" commit on top of the
  existing uncommitted batches (see the 10 Jul handoff for the (a)/(b)/(c) split).
- **Deferred (unchanged from prior handoffs):** CLAUDE.md build-status line still says
  182 tests (really ~213); update only after live sign-off. Real-mic denoise A/B for
  `denoise_strength`; optional `pip uninstall torch torchaudio`; delete `.tmp\dfspike`.

## Plan
- No dedicated plan file for this session. Overarching plan remains
  `c:\Users\jemer\code\localflow\plans\v1.1-plan.md`. This session's two fixes should be
  noted in its milestone log when Jeremy signs off.

## Next steps
1. Jeremy QUITS the running tray instance and relaunches (picks up the `stt.py` token
   strip; `capture.py` is already live). Do a few normal dictations.
2. Confirm from the log: `peak ... 0 input-overflow(s)`, no `<|...|>` in pasted text,
   no `you you you` empties. If clean, the "nothing happens" + token issues are resolved.
3. If garbage decodes recur: read the new peak/overflow numbers — overflow > 0 means the
   `latency="high"` headroom is still not enough (raise input blocksize next); peak
   consistently tiny with clean-audio garbage means low-SNR (address quiet mic). Only
   then reach for the retry-without-timestamps fallback.
4. On sign-off: commit the two fixes (confirm the split with Jeremy; do NOT push), and
   update the v1.1 plan milestone log + CLAUDE.md test count.
