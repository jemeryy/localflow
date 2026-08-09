# Session Handoff — numbers-as-digits, Gemma cleanup adoption, sliding preview window, pill Restart

## Where it started
Jeremy (on Parakeet, `stt_model = "parakeet"`): "cloud" keeps coming out instead of
"Claude" — fix it WITHOUT hardcoding and without a dictionary; numbers must show as
digits. Later: live typing falls behind on long holds (diagnose), cleanup LLM
experiment with a small Gemma, and a Restart row on the pill's right-click menu.

## Important discoveries and decisions
- Parakeet's runtime (onnx-asr 0.12.0) has NO hotword/context biasing — there is no
  model-level fix for cloud->Claude. Jeremy REJECTED blanket dictionary rules
  ("cloud" = "Claude" removed from `localflow.toml` same day) and rejected using the
  dictionary for this at all. The accepted mechanism is the cleanup LLM (context-gated).
- Post-STT transforms now reach the live-typed screen: `app.post_stt` (dictionary +
  `spelled_to_digits`) is applied identically to preview decodes and segment texts via
  `LiveTyper.transform`. Before this, dictionary rules never touched the screen.
- Live-typing slowness is a PROCESSING problem, not pause-waiting: the preview
  re-decoded the WHOLE hold every beat (2-6s per decode at 25s+ on CPU), and the
  half-wall-clock throttle stretched the cadence; the stream fell 10-20s behind.
- Gemma cleanup spike (`c:\Users\jemer\code\localflow\docs\gemma-cleanup-spike.md`):
  few-shot examples are what make small models edit-not-answer. Qwen2.5-3B 2/7,
  Gemma 3 1B few-shot 6/7, Gemma 3 4B few-shot 7/7 (fixes cloud->Claude from context,
  leaves genuine "cloud on AWS" alone) — 4B ADOPTED.
- gemma-3-4b-it is multimodal; optimum won't export it as text. Fix: safetensors key
  surgery (drop `vision_tower.*`/`multi_modal_projector.*`, strip `language_model.`
  prefix, rebuild config from `text_config`), then normal int4 export.
- huggingface_hub transfers are broken on this network (0-byte stalls, xet AND plain
  HTTP; unauthenticated throttling). Direct `curl.exe` on resolve URLs runs ~80MB/s —
  use curl for all future model pulls.
- Surgical cleanup-to-screen (patch only changed words after release) is PARKED by
  Jeremy. His caret question answered: keystrokes always need the caret, but classic
  edit controls (EM_SETSEL/EM_REPLACESEL) and UIA TextPattern apps can be edited
  near-instantly with the caret restored; Chromium is the weak case. He'd prefer
  "words magically change", ideally while still recording — design not started.

## Decisions locked + what shipped
- `spelled_to_digits` — new pure ITN-lite in
  `c:\Users\jemer\code\localflow\localflow\core\numbers.py` ("forty seven"->"47",
  "twenty six point two seven"->"26.27"; bare "one" never converted; documented
  ceilings: year pairs "twenty twenty six"->"20 26", "a hundred" stays, "five
  thirty"->"5 30"). Config `numbers_as_digits` (default false = v1.2 surface;
  Jeremy's toml = true). Commit 1c2390f.
- Dictionary + digits run as one `post_stt` pass in `app.py`, fed to
  `LiveTyper(transform=...)` — both live paths + final text agree. Commit 1c2390f.
- cloud->Claude dictionary rules REMOVED (e0fa548). `[replacements]` is empty.
- **Tag `baseline-2`** = numbers + live-transform, pre-Gemma. `v1.2` still the older
  fallback.
- Cleanup model: Gemma 3 4B int4 at `models\gemma-3-4b-ov` (2.3GB, 0.5-2s/utterance
  GPU). `core/cleanup.py` rewritten: few-shot `build_prompt` + `cleanup_terms` config
  (prompt hint list, NOT a dictionary; his terms in toml). Defaults changed in
  `config.py` + toml. Qwen (`models\qwen2.5-3b-ov`) and Gemma 1B
  (`models\gemma-3-1b-ov`, 691MB, 6/7) kept as fallbacks. Commit 6d2c8fb.
- `segment_seconds` 30 -> 15 in `localflow.toml` (window drains sooner; preview and
  release wait both shrink).
- Sliding preview window in `localflow\desktop\livetyper.py`: `PREVIEW_MAX_S = 16`,
  `PREVIEW_KEEP_S = 10`; slice jump resets agreement, `_slice_jumped` makes the
  append_tail fallback conservative (drop a beat, never duplicate); `on_cut`/`_reset`
  zero the slice. Segment/final decodes untouched (full audio) so final text quality
  is unchanged by construction. Commit 0309178.
- Pill right-click menu gained "Restart" (between Settings and Quit), same path as
  tray Restart (`restart_ref["want"] + request_quit`), works with --no-tray. Commit
  e7b68f5.

## Key files for next session
- `c:\Users\jemer\code\localflow\docs\gemma-cleanup-spike.md` — spike verdict + the
  model-export surgery recipe + curl note.
- `c:\Users\jemer\code\localflow\localflow\desktop\livetyper.py` — sliding window +
  transform seam (the load-bearing live-typing changes to defend).
- `c:\Users\jemer\code\localflow\localflow\core\numbers.py` — ITN ceilings listed in
  the docstring.
- `c:\Users\jemer\code\localflow\localflow\core\cleanup.py` — few-shot prompt
  (`build_prompt`), `cleanup_terms`.
- `c:\Users\jemer\code\localflow\localflow.toml` — his live config: parakeet,
  numbers_as_digits=true, segment_seconds=15, cleanup gemma-3-4b + terms.
- Plan file: none this session (feature work was conversation-driven).
- Memory files touched: none.

## Running state
- Background processes: none (all downloads/exports finished; zombie hf downloader
  PID 25492 was killed; fara test process 26988 was someone else's — left alone).
- Dev servers / ports: none.
- Open worktrees / branches: none — everything on `main`, clean tree.

## Verification — how to confirm things still work
- `cd c:\Users\jemer\code\localflow; .venv\Scripts\python.exe -m pytest tests -q` —
  355 passed (was 326 at session start).
- Restart LocalFlow from tray, hold a 60s+ monologue: words should stay ~2-3s behind
  throughout; final text quality unchanged.
- Tray toggle cleanup ON, dictate "ask cloud to review the diff" (paste mode or check
  history): history final_text should read Claude; "in the cloud on AWS" must stay.

## Deferred / pending tasks + open questions
- Deferred: surgical cleanup-to-screen ("magic" word replacement, ideally without
  caret walking, maybe mid-recording) — parked by Jeremy; when revived, start from
  per-app text APIs (EM_SETSEL/EM_REPLACESEL, UIA TextPattern) with keystroke-patch
  fallback, and note it reverses the 27 Jul no-revision rule for LLM fixes only.
- Pending HIS LIVE TEST of: sliding preview + segment 15 (watch for dup/drop right
  after a long unbroken stretch — the slice-jump seam), numbers-as-digits live,
  Gemma cleanup quality on real dictations, pill Restart row.
- Open: none blocking.

## Plan
- No plan file. Overarching thread: Parakeet-era polish — deterministic post-STT
  shaping (done), preview latency (done, pending live test), context-aware cleanup
  (model adopted, screen application parked).

## Next steps
Get Jeremy's live verdict on: (1) preview keeping pace on long holds, (2) digits
output, (3) Gemma cleanup via tray toggle, (4) pill Restart. Fix what his runs
surface (`%LOCALAPPDATA%\LocalFlow\localflow.log`). If all pass, consider porting the
sliding-window learnings into docs and revisiting the parked surgical cleanup design.

## Next step (for the next session after /clear)
Next step: read c:\Users\jemer\code\localflow\plans\handoff-2026-08-09-numbers-gemma-cleanup-sliding-preview.md, then collect Jeremy's live verdict on sliding preview / digits / Gemma cleanup / pill Restart and fix what surfaces.
