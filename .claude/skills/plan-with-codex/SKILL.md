---
name: plan-with-codex
description: Use when Jeremy asks to plan a non-trivial project, feature, or refactor with Claude and Codex collaborating. Runs a silent back-and-forth between Claude and Codex until BOTH genuinely agree, then outputs ONLY the final plan. Triggers include "plan this with codex", "let's plan with codex", "plan and cross-check with codex", "bounce this off codex first", or any near-equivalent. Do NOT run this for trivial changes (single-file edits, bug fixes with a clear root cause).
---

# Plan with Codex (silent debate loop)

Claude and Codex debate the plan in the background until they converge. Jeremy sees only the final agreed plan, never the back-and-forth.

## When to use this skill

- Multi-step features touching more than one file or system.
- Refactors that change a contract (API, schema, hook signature).
- Anything that would otherwise get a row in `plans/`.

**Don't use this for:** single-file edits, bug fixes with an already-identified root cause, doc-only changes, or work where Jeremy already aligned on the approach.

## Codex session hygiene (applies to every Codex call in this skill)

**One Codex session per Claude session. Never resume across Claude sessions.**

- The FIRST Codex invocation in a given Claude session always uses `--fresh`, even if the work is a continuation of a prior session's plan. `--resume-last` would pick up the previous Claude session's Codex thread, which had different context and may have diverged.
- Subsequent Codex calls WITHIN the same Claude session use `--resume-last` so the debate/execution stays coherent.
- If you're unsure whether a prior Codex thread belongs to this Claude session, treat it as foreign — use `--fresh`.

**Every `--fresh` Codex call must include a context preamble.** Codex starts with zero knowledge of the project, the active plan, or what this Claude session has been working on. Lead the prompt with:

```
Context — project and session:
- Project: localflow (this repo). A personal, fully-local voice-to-text dictation
  tool for Windows (a Wispr Flow clone): hold a hotkey, speak, release, and the text
  is typed into the focused app. Whisper on OpenVINO runs on the Intel Arc iGPU/NPU.
  Python. Key dirs: localflow/ (core + desktop packages), tests/, scripts/, plans/,
  docs/ (PRD.md is the locked spec), .claude/skills/.
- Hard constraints: fully local (no network for transcription, no audio/text leaves
  the machine); `localflow/core/` stays a pure package (no desktop imports, no
  clipboard/keyboard, no network); Intel Arc GPU/NPU target (no CUDA); log which
  device (GPU/NPU/CPU) actually ran.
- This Claude session is working on: <one-sentence summary of what this session
  is doing — e.g. "wiring the optional Qwen cleanup toggle into the tray app,
  see plans/2026-07-xx-cleanup-toggle.md">.
- Relevant memory/decisions for this work: <any feedback/project memory or
  decisions/log.md entries that bear on this task — cite the file>.
- Communication rules: no em/en dashes; plain words over analogies; no filler.
  Output must follow the structured format requested below.

Now the actual task:
<task prompt — critique / re-critique / execute / adversarial-review / etc.>
```

The preamble is mandatory on `--fresh`. On `--resume-last` it's not needed (Codex already has the thread context), but ALWAYS reference the plan file path so Codex re-reads if anything changed.

## How to call Codex (the invocation contract)

Every Codex call in this skill goes through the hardened wrapper `tools/codex_exec.py`. Do NOT use the `/codex:*` slash commands (they are user-only, marked disable-model-invocation, so the model cannot run them) and do NOT route these calls through the codex-rescue subagent.

```
python tools/codex_exec.py --prompt-file <f> [--resume-last] --effort high [--write]
```

- Preflight availability: `python tools/codex_exec.py --pong` (prints PONG).
- Write the prompt to a file under `.tmp/` and pass `--prompt-file` (piped via stdin, no argv-length limit).
- Session hygiene: the FIRST Codex call of the Claude session is fresh (omit `--resume-last`) and MUST include the context preamble above; follow-up calls in the same session use `--resume-last` (no preamble) but always reference the plan file path.
- Plan-critique calls are read-only: no `--write`. Only the execute phase passes `--write`.
- Run every round synchronously (foreground Bash, 10-min timeout).
- Exit codes: 0 ok, 3 usage-limit, 4 timeout, 5 auth, 6 codex CLI not found, 1 other (already retried once if transient). Branch on the exit code.

## TWO HARD RULES (read these first, they govern everything below)

### Rule 1 — Genuine convergence, not one-shot dumping

The loop only ends when **both Claude and Codex independently sign off with no remaining BLOCKING issues**. One side saying "converged" is not enough. Specifically:

- After Codex critiques, Claude MUST revise the plan to address every BLOCKING and RISKS item (accept or rebut in the plan file). Then Claude flags "ready for re-critique".
- Codex re-reviews the revised plan. If Codex still has BLOCKING items, the loop continues.
- Convergence requires Codex explicitly returning **CONVERGED, no blocking** AND Claude verifying the revised plan addresses every prior issue.
- If after 3 rounds they still don't agree, escalate to Jeremy with the specific disagreement.

Do not exit the loop just because the rounds-counter ran out. Run as many rounds as needed (up to 3) until genuine agreement.

### Rule 2 — Single final output, no per-round chat

During the loop, the ONLY chat output Claude produces is:

- At the start: one line — "Planning <X> with Codex. Will report when locked."
- At the end: one chat message with the locked plan summary (see "Final output" below).

Forbidden during the loop:
- "Round 1 Codex critique back. <N blocking>..."
- "Claude said X, Codex said Y."
- "Revising to address..."
- Any per-round status ping.
- Any side-by-side listing of Claude's vs Codex's positions.

The debate transcript lives in the plan file's Notes section (for audit/debugging). Jeremy reads the plan file, not the chat.

## The loop (silent, no chat output)

### Round 0 — Frame

Claude writes a one-paragraph problem framing into the plan file (`plans/YYYY-MM-DD-<slug>.md`):
- Goal (1 sentence)
- Constraints (deadlines, dependencies, files/systems involved)
- Definition of done (1 sentence)

### Round 1 — Claude drafts v1

Use `templates/plan.md`. Plan includes:
- Goal + Why now
- **Approach:** architecture choice, files to touch, order of operations
- **Steps:** ordered checklist
- **Open questions:** invitations for Codex to push
- **Alternatives considered:** at least one rejected alternative with reason

### Round 2 — Codex critiques v1

Invoke via `python tools/codex_exec.py --prompt-file <f> --effort high` (fresh: omit `--resume-last`, include the preamble; plan-only — no `--write`):

```
Plan critique only — DO NOT write any files.
Read plans/<YYYY-MM-DD-slug>.md and any files it references.
Return a structured critique with:
- BLOCKING: issues that would make the plan fail or cause irreversible damage
- RISKS: lower-severity concerns worth addressing
- MISSING: edge cases, failure modes, dependencies not covered
- ALTERNATIVES: stronger approaches Claude didn't consider, with reasoning
- AGREE: parts you actively endorse (so they don't get rewritten out)
Be specific. Cite file paths and line numbers. No vague "consider X" — say what to do and why.
```

Append Codex's full critique into the plan's Notes under `## Codex critique — round 1` (for audit).

### Round 3 — Claude revises (v2)

For every BLOCKING and RISKS item:
- **Accepted** → revise the relevant plan section, update Steps.
- **Rejected** → write a one-paragraph rebuttal under `## Claude rebuttal — round 1` in the plan's Notes. Rebuttals must be substantive; "I disagree" doesn't count.

For ALTERNATIVES that are clearly stronger: accept and update Approach. Note the swap in Changelog.

### Round 4 — Codex re-reviews v2

Invoke `python tools/codex_exec.py --prompt-file <f> --resume-last --effort high` again (plan-only — no `--write`):

```
Plan re-critique only — DO NOT write any files.
Read the revised plan: plans/<YYYY-MM-DD-slug>.md
Read "Codex critique — round 1" and "Claude rebuttal — round 1" in the plan's Notes.
Return ONLY:
- STILL BLOCKING: round-1 issues the revision didn't resolve (one-line why)
- NEW BLOCKING: new blocking issues exposed by the revised approach
- CONVERGED: state explicitly if you have no remaining blocking objections
```

Append under `## Codex critique — round 2`.

### Round 5 — Convergence check

Three outcomes:

1. **Codex returns CONVERGED, no blocking.** Plan locks. Update status: active, append `YYYY-MM-DD: plan locked after Claude/Codex debate.` to Changelog. Go to Final output.
2. **Codex still has blocking items, judgment call.** Claude writes a `## Final reconciliation` section in the plan proposing a decision, then escalates to Jeremy in chat: one line — "Codex blocks on <X>. I think <Y> because <Z>. Your call." Do NOT lock. Wait for Jeremy.
3. **Codex still has blocking items, clear correctness issue.** Run one more silent revision round (round 6: Claude revises, round 7: Codex re-checks). If still blocking after that, escalate to Jeremy.

Loop until genuine agreement, NOT until rounds run out. If you hit round 3 (Codex's second re-check) with blocking issues still present, escalate. Don't fake convergence.

## Final output after planning (the only chat message)

When the plan locks, Claude writes ONE detailed-but-readable chat message. Three sections, in order. No per-round status, no "Codex said X". This is the only thing Jeremy sees from the entire debate.

**Template:**

```
Plan locked: plans/<YYYY-MM-DD-slug>.md

What we're building:
<2-3 sentences in plain language. What the change accomplishes, the
key approach choice, and what success looks like. Not the steps —
the why and what.>

Steps (will run when you say go):
1. <step 1 in plain language — what gets done, not how>
2. <step 2>
3. <...keep to 5-8 high-level steps; sub-bullets in the plan file>

Executor: <claude|codex>
<one sentence on why this executor for this plan — cite the matching
signal from the executor-selection rules, e.g. "FA math + tightly-scoped
to 2 files = codex" or "touches 6 files across tools/projects/skills,
needs Gmail MCP = claude".>

Reply "go" to start. Reply "execute with <other>" to override the executor.
```

**Length target:** 12-18 lines. Long enough that Jeremy doesn't have to open the plan file to understand the change; short enough to read in 15 seconds. The plan file has the full Steps with sub-bullets, Open questions, Alternatives, debate transcripts, etc.

**If escalation is needed instead of convergence**, the message becomes:

```
Plan draft at plans/<YYYY-MM-DD-slug>.md — NOT locked.

What we're building:
<same 2-3 sentence summary>

Codex and I converged on most of the plan, but <K> open call(s) need your input:

1. <Decision needed in one line.>
   My lean: <Claude's recommendation>
   Reason: <why>
   Codex's view: <one line>

2. <...>

Reply with your call on each, and I'll lock the plan.
```

## Failure handling (Codex unavailable, quota exhausted, timeout, etc.)

Codex can fail mid-loop. The skill must never wait silently or hang. Every Codex call has a bounded timeout, and every failure either retries once or falls back with Jeremy notified.

### Timeouts to set on every Codex call

- **Plan critique (rounds 2 and 4):** Bash timeout = 10 min (600000 ms). `--effort high` plus a long plan can take 4-6 minutes; 10 min gives headroom.
- **Codex executor (execute phase):** Bash timeout = 10 min per invocation. For long jobs, split into multiple `--resume-last` calls rather than one giant call.
- **Status checks (`/codex:status`):** quick, default Bash timeout is fine.

(The review phase has its own timeout + failure handling — it lives in the `review-with-codex` skill now.)

If a Bash call times out, treat it as a Codex failure (see decision tree below).

### Failure detection signals

After every Codex call, check the subagent's result for any of:

1. **Non-zero exit code** in the Bash result.
2. **stderr contains** any of: `rate_limit`, `rate limit`, `quota`, `usage_limit_exceeded`, `429`, `401`, `403`, `authentication`, `unauthorized`, `out of credits`, `insufficient_quota`.
3. **"command timed out"** from the Bash tool.
4. **Empty stdout** (Codex returned nothing).
5. **Malformed output** (claimed to be a critique but missing the BLOCKING/RISKS structure, etc.).

Any of these = failure. Don't retry on the same Codex call hoping it'll work — diagnose first.

### Decision tree on failure

**Step 1 — Classify the failure** from the stderr / output:

- **A. Transient (timeout, network blip, malformed output):** Retry ONCE with `--effort medium` (faster) and the same prompt. If second attempt succeeds, continue the loop and note in plan file: `<phase>: retried after transient failure`.
- **B. Quota / rate limit exhausted (any of the quota keywords above):** Do NOT retry. Surface to Jeremy immediately. See Step 2.
- **C. Auth failure (`401`, `authentication`, `unauthorized`):** Do NOT retry. Surface to Jeremy with the auth fix command. See Step 2.
- **D. Codex tool error during execution (Codex reported a Python script failed):** Surface to Jeremy with the underlying error. See Step 2.

**Step 2 — Surface to Jeremy with a structured chat message:**

```
Codex unavailable during <plan critique round 2 | execution | adversarial review>.

Reason: <quota exhausted | auth expired | timeout x2 | tool error>
<one line of detail from the error>

Options:
1. Continue Claude-only — I'll <complete the plan | finish execution | run review> 
   using just Claude. Quality drops to single-perspective but the work moves.
2. Pause here — fix the Codex issue first.
   <If quota: "Wait until your OpenAI quota resets, or upgrade plan.">
   <If auth:  "Run `/codex:setup` to re-authenticate.">
   <If other: "Investigate the underlying error.">

Reply "claude only" to continue solo, or fix and reply "retry codex".

What's been preserved so far:
- Plan file: plans/<YYYY-MM-DD-slug>.md (whatever was captured pre-failure)
- <if execution: list which steps completed, which didn't>
```

**Step 3 — On "claude only":**
- Plan phase: Claude finishes the plan solo. Mark in Changelog: `YYYY-MM-DD: locked Claude-only after Codex <reason>.`
- Execute phase: Claude takes over from wherever Codex stopped. Plan file gets a note in the Codex execution log.
- Review phase: Claude does the review solo. The chat report flags it clearly: `Review complete (Claude-only — Codex was unavailable). Reduced confidence in correctness/edge cases.`

**Step 4 — On "retry codex":**
- Re-run the same Codex call. If it succeeds, resume the loop. If it fails again, go back to Step 2.

### Heartbeat for long-running Codex calls

If Codex is running in foreground Bash and the call takes > 4 minutes without returning, that's normal for `--effort high`. Don't surface anything mid-call — wait for the timeout boundary.

If you ever run Codex via the **background job** mode (`run_in_background: true` on the Bash call), use `/codex:status <jobId>` to poll, and apply the same timeout/failure rules. Background mode is useful for very long execution jobs where you want Claude to do other work in parallel.

### What Jeremy can check manually

If you suspect Codex is stuck or quota-exhausted and want to verify outside the skill:

- `/codex:setup` — checks auth status, shows whether Codex CLI is reachable.
- `/codex:status` — lists active and recent Codex jobs with their status (queued/running/completed/failed).
- `/codex:result <jobId>` — fetches the output of a completed job.
- `/codex:cancel <jobId>` — kills a stuck job.

The Codex CLI itself logs to its own usage page; OpenAI ChatGPT/Codex quota is visible at chatgpt.com/codex usage.

## Executor selection (Claude picks, no ask needed)

When the plan locks, Claude picks the executor using these rules. Don't ask Jeremy — pick.

**Codex executor when ANY of:**
- Dense, self-contained logic: the audio -> text core (OpenVINO / Whisper), the silence gate, or hotkey / paste-injection timing in `localflow/core` and `localflow/desktop`.
- Root-cause fix where Claude already burned 2+ rounds.
- Model-bug fix where overrides are tempting but wrong.
- Tightly-scoped refactor: 1-3 files, clear interface, dense logic.

**Claude executor when ANY of:**
- Touches 5+ files or crosses subsystems.
- Content, copy, docs: READMEs, workflows, SOPs, decision-log, skill SKILL.md.
- Anything needing MCP tools (WebSearch, Gmail / Sheets / Docs) that Codex can't reach.
- Plan steps requiring TodoWrite / decision-log / memory updates as they go.
- Anything where the recipe is "use the X skill".

**Default Claude on toss-ups.** Codex doesn't have MCP tools or skill awareness.

**Codex invocation when picked:** `python tools/codex_exec.py --prompt-file <f> --write --effort high` (`--resume-last` if a Codex call already ran this session, else fresh with the preamble). Prompt references the plan file: `Read plans/<YYYY-MM-DD-slug>.md and implement steps 1-N. Resume the plan-debate session so the implementation reflects the agreed approach.`

### Execute-start checkpoint (re-confirm the executor before starting)

Before Claude writes the first line of code (or invokes the Codex executor) on an approved plan, pause and re-check the executor choice. The plan-lock chose an executor based on the plan as written; the actual work about to happen may have drifted (extra files surfaced, math turned out denser than expected, the recipe ended up being "use the X skill", etc.).

**Run this check before touching code, every time:**

1. Re-read the plan's Steps and the file paths involved.
2. Apply the executor-selection rules above to what you're actually about to do, not what the plan abstractly said.
3. If the answer flipped (plan said Claude, but steps 1-3 are dense FA math you'd benefit from Codex on — or plan said Codex, but step 2 needs Gmail MCP), switch the executor without asking Jeremy.

**On switch:** one chat line — "Switching executor to <codex|claude> for this run because <one-sentence reason>. Starting." Then proceed.

**On no switch:** no chat line. Just start.

This is not an invitation to second-guess on every plan — most plans execute as routed. It's a safety net for the cases where the plan's signal was weak or the work shape changed. Codex is genuinely better for math-dense, tightly-scoped, root-cause work; if you spot that shape entering execution, use it.

## Review phase (auto-runs silently after execute, ONE final message)

The review is now a standalone skill: **`review-with-codex`**. When execution completes, invoke it automatically (no prompting) to run the silent two-model pre-merge review (Claude diff review + Codex adversarial review → reconcile → one chat message). It writes its audit trail into this plan's Notes (`## Pre-merge review — Claude/Codex/Reconcile`) and produces the single "Ready to merge" or "Awaiting your call" chat message.

Pass it the plan file path so it runs in plan-file mode rather than standalone mode. Everything about how the review runs — the steps, the reconcile rules, the chat-message templates, and the Codex-failure handling for the review — lives in `review-with-codex/SKILL.md`. Do not duplicate that logic here.

The two principles still hold across the handoff: silent debate, single final output. The execute phase keeps the floor until the review's final message, so don't emit a separate "starting review" line — flow straight from execution into the review skill.

## What's in chat vs what's in the plan file

| Phase | Chat shows | Plan file shows |
|---|---|---|
| Plan start | "Planning X with Codex. Will report when locked." | (file being drafted) |
| Plan debate rounds | nothing | every round's critique, rebuttal, revision |
| Plan locked | one-line lock confirmation + executor choice | full converged plan + audit trail |
| Execute | normal progress (or Codex execution log if Codex executor) | step check-offs |
| Review debate | nothing | both reviews + reconcile |
| Review done | one reconciled message (clean OR judgment calls) | full review + fix log |

## After the loop

- Append meaningful decisions to `decisions/log.md`.
- Pre-merge gate: review phase already ran. Merge when Jeremy clears any judgment calls.

## Reference: where each model's strengths help

- **Codex catches:** unstated assumptions, missing failure modes, alternative architectures, model-bug root causes Claude papers over.
- **Claude catches:** repo-specific context, file path correctness, UX/edge-case reasoning, long-context coherence, skill/MCP-tool awareness.

The silent debate forces Codex's planning strength onto every plan without leaking the messy middle to Jeremy. The review half of that (forcing both models' review strengths onto the diff) now lives in `review-with-codex`.

## Changelog
- 2026-07-18: Codex now invoked via the hardened `tools/codex_exec.py` wrapper instead of the codex-rescue subagent / slash commands (which the model cannot reliably run). Planning debate keeps its resume semantics; only the invocation mechanism changed.
- 2026-05-17: created.
- 2026-05-17: rewrote for genuine convergence + single final output after Jeremy flagged that the loop was dumping both opinions instead of converging silently.
- 2026-05-18: added Execute-start checkpoint — Claude re-confirms (or switches) the executor right before starting execution, in case the work shape drifted from what the plan-lock anticipated.
- 2026-05-18: added Codex session hygiene — one Codex session per Claude session (never `--resume-last` across Claude sessions), and every `--fresh` call must include a project + session context preamble.
- 2026-06-01: split the review phase out into the standalone `review-with-codex` skill (invokable on any diff, not just plan-with-codex output). plan-with-codex's execute phase now delegates its pre-merge review to that skill instead of inlining it.
