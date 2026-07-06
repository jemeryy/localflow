---
name: review-with-codex
description: Use when Jeremy asks to review a code change / diff with Claude and Codex collaborating, OR when invoked automatically at the end of the plan-with-codex execute phase. Runs a silent two-model review (Claude diff review + Codex adversarial review), reconciles both, fixes correctness issues inline, and outputs ONE chat message. Triggers include "review this with codex", "review my changes with codex", "adversarial review", "cross-check the diff with codex", "review before I merge", or any near-equivalent. Do NOT run this for trivial diffs (single-file doc edits, obvious one-line fixes).
---

# Review with Codex (silent two-model review)

Claude and Codex review the diff in the background, reconcile their findings, fix correctness issues inline, and Jeremy sees ONE final message. Same two principles as plan-with-codex: silent debate, single final output.

This skill runs in two modes:

- **Standalone** — Jeremy asks to review a change directly ("review this with codex"). There may be no plan file.
- **Auto, from plan-with-codex** — the execute phase finishes and the review runs end-to-end without prompting. A plan file exists at `plans/YYYY-MM-DD-<slug>.md`.

## When to use this skill

- A multi-file or contract-changing diff is ready to merge.
- Any work that went through plan-with-codex (the execute phase auto-invokes this skill on completion).
- Jeremy explicitly wants a second-model adversarial pass on a change before merging.

**Don't use this for:** single-file doc edits, obvious one-line fixes, or changes Jeremy has already eyeballed and is happy with.

## Where findings get captured

- **Auto / plan-file mode:** capture into the plan file's Notes sections (`## Pre-merge review — Claude`, `## Pre-merge review — Codex`, `## Pre-merge review — Reconcile`). This is the audit trail.
- **Standalone / no-plan mode:** capture into a scratch file at `.tmp/review-<short-slug>.md` using the same three section headers. Jeremy reads the chat message; the scratch file is for audit/debugging only.

Either way, the messy middle never goes to chat.

## Codex session hygiene (applies to every Codex call in this skill)

**One Codex session per Claude session. Never resume across Claude sessions.**

- The FIRST Codex invocation in a given Claude session always uses `--fresh`. If plan-with-codex already ran Codex earlier in THIS same Claude session (plan critique or execute), then this review's Codex call is a follow-up and uses `--resume-last`.
- If you're unsure whether a prior Codex thread belongs to this Claude session, treat it as foreign — use `--fresh`.

**Every `--fresh` Codex call must include a context preamble.** Codex starts with zero knowledge of the project, the change, or what this session did. Lead the prompt with:

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
- This Claude session is working on: <one-sentence summary of the change
  being reviewed — e.g. "reviewing the tray waveform-pill focus handling in
  localflow/desktop, see plans/2026-06-01-...md">.
- Relevant memory/decisions for this work: <any feedback/project memory or
  decisions/log.md entries that bear on this change — cite the file>.
- Communication rules: no em/en dashes; plain words over analogies; no filler.

Now the actual task:
<review task prompt>
```

On `--resume-last` the preamble isn't needed (Codex has the thread context), but always reference the plan file path (or name the changed files) so Codex re-reads current state.

## TWO HARD RULES (these govern everything below)

### Rule 1 — Reconcile internally, never dump both opinions

Jeremy sees the reconciled result, not "Claude flagged X, Codex flagged Y." Both-flagged and one-flagged-correctness issues get fixed inline before the chat message. Only genuine judgment calls reach Jeremy, and they're presented as a single decision list with Claude's lean, not a side-by-side of the two models.

### Rule 2 — Single final output, no per-step chat

During the review the ONLY chat output is the final message. Forbidden mid-review:
- "Claude review done, running Codex now."
- "Codex flagged 3 issues..."
- Any per-step or per-reviewer status ping.

(Standalone mode may emit one short opening line — "Reviewing with Codex. Will report when done." — since there was no plan-lock message to set expectations. Auto mode emits nothing until the final message, because the execute phase already had the floor.)

## The review (silent)

### Step 1 — Claude reviews the diff (silent)

Read the working-tree diff against `main` (`git diff main`). If a plan file exists, cross-check every plan step against the diff: file-path mistakes, missed steps, UX issues, regressions, broken imports. If standalone, review for correctness, edge cases, regressions, and anything that contradicts the change's stated intent.

Capture findings into `## Pre-merge review — Claude` (plan file or `.tmp/review-<slug>.md`).

### Step 2 — Codex adversarial review (silent)

```
/codex:adversarial-review --base main
```

(Or invoke the `codex-rescue` subagent with an equivalent adversarial-review prompt if the slash command isn't appropriate for the context. `--effort high`, plan/review-only — no `--write`.)

Capture into `## Pre-merge review — Codex`.

### Step 3 — Silent reconcile

Claude buckets every finding from both reviews:
- **Both flag** → real. Fix inline immediately, no ask.
- **One flags, correctness issue** (broken import, wrong path, off-by-one, missing null guard) → real. Fix inline, no ask.
- **One flags, judgment call** (style, optional risk, alternative approach, scope question) → defer to Jeremy.
- **One flags, the other would dismiss it** → DISPUTED. Do not silently drop it. Go through the rebuttal round below before bucketing.

The dangerous failure mode is one model waving off a real bug. The disputed bucket exists to stop that.

#### Step 3a — Disputed-findings rebuttal (one round, only on disputes)

For each disputed finding (one model flags it, the other dismisses or didn't raise it), run ONE rebuttal round. Don't debate findings both models already agree on — only the disputes.

- If **Codex flagged** and **Claude wants to dismiss**: Claude writes a one-paragraph defense of the dismissal (why it's a non-issue or out of scope) into the audit file. Then put the finding back to Codex on `--resume-last`:
  ```
  Rebuttal check — DO NOT write any files.
  You flagged: <finding, one line, with file:line>.
  Claude's reason for dismissing it: <Claude's paragraph>.
  Either: CONCEDE (Claude's reasoning holds, it's not a real issue) or
  HOLD (it's real — give the one-line concrete reason / failing case Claude missed).
  ```
- If **Claude flagged** and **Codex didn't raise it**: Claude states the concrete failing case in the audit file. If it's a clear correctness issue, it's not actually disputed — fix it. Only route to Jeremy if it's genuinely a judgment call.

Resolve each dispute from the rebuttal:
- **Rebuttal collapses** (model CONCEDEs, or HOLD has no concrete failing case) → drop the finding. Note it in the audit file.
- **Rebuttal HOLDs with a concrete case** → it's real. If correctness, fix inline. If judgment, surface to Jeremy.
- One round only. If still genuinely deadlocked after the rebuttal, treat as a judgment call and surface to Jeremy with both positions in one line.

Append the rebuttal outcomes to `## Pre-merge review — Reconcile`.

#### Step 3b — Fix and verify each fix

Apply the inline fixes for everything bucketed as real-and-correctness.

After fixing, verify in two layers:
1. **Targeted fix-verification** — for each specific fix, confirm it actually resolves the specific finding (not just "nothing else broke"). Put the original finding + the diff of the fix back to the model that raised it on `--resume-last`:
   ```
   Fix verification — DO NOT write any files.
   Original finding: <finding>.
   The fix applied (diff): <the hunk>.
   Does this fully resolve the finding? RESOLVED or NOT-RESOLVED (one-line why / what's still wrong).
   ```
   If NOT-RESOLVED, re-fix and re-verify.
2. **Full re-scan** — once all targeted fixes verify, re-run Codex adversarial-review once to catch anything the fixes newly broke. If new blocking issues appear, fix and re-run.

Up to 2 silent fix rounds total across both layers. If still blocking after that, escalate to Jeremy.

### Step 4 — ONE chat message (the only review output)

Detailed-but-readable. Three sections. No per-reviewer breakdown.

**If everything's clean (Ready to merge):**

```
Review complete: <plans/<YYYY-MM-DD-slug>.md | changes on this branch> — Ready to merge.

What was reviewed:
<2-3 sentences in plain language. What the change does. Reference the
key files changed at a high level, not a full diff list.>

Fixed during review (<N> items):
- <Issue 1 in plain language: what was wrong, what was changed.>
- <Issue 2.>
- <... keep concise, one line each.>

Next: merge when ready (your usual flow — `git push` / PR / etc.).
```

**If judgment calls remain (Ready conditional on your input):**

```
Review complete: <plans/<YYYY-MM-DD-slug>.md | changes on this branch> — Awaiting your call on <K> items.

What was reviewed:
<2-3 sentences in plain language.>

Fixed during review (<M> items):
- <Issue 1: what was wrong, what was changed.>
- <Issue 2.>
- <...>

Your call on (<K> items):
1. <Decision needed in one line.>
   My lean: <Claude's recommendation>
   Reason: <why>
   Impact if deferred: <what happens if we ship as-is>

2. <...>

Reply with your call on each, and I'll apply the fixes (if any) and confirm
ready to merge.
```

**Length target:** 15-25 lines depending on how many items were fixed/deferred. Long enough to be informative; short enough that Jeremy doesn't need to open the audit file unless they want the full trail (Claude's vs Codex's individual findings live there).

**What NOT to include in chat:**
- Per-reviewer breakdown ("Claude flagged X, Codex flagged Y"). Reconcile internally; show the reconciled result.
- The raw diff. Jeremy reads the diff in their editor.
- Debate transcripts. Those live in the audit file.

## Failure handling (Codex unavailable, quota exhausted, timeout)

The review must never hang or wait silently. Every Codex call has a 10-min Bash timeout (`--effort high` plus a large diff can take 4-6 min). If a Bash call times out, treat it as a Codex failure.

**Failure signals** (any one = failure): non-zero exit code; stderr contains `rate_limit`/`quota`/`429`/`401`/`403`/`authentication`/`unauthorized`/`out of credits`/`insufficient_quota`; "command timed out"; empty stdout; output missing the adversarial-review structure.

**Classify and act:**
- **Transient (timeout, network blip, malformed output):** retry ONCE with `--effort medium`. If it succeeds, continue and note `review: retried after transient failure` in the audit file.
- **Quota / rate limit:** do NOT retry. Surface to Jeremy.
- **Auth (`401`/`authentication`/`unauthorized`):** do NOT retry. Surface with the fix command (`/codex:setup`).

**Surface message:**

```
Codex unavailable during adversarial review.

Reason: <quota exhausted | auth expired | timeout x2>
<one line of detail from the error>

Options:
1. Continue Claude-only — I'll finish the review using just Claude.
   Single-perspective, so reduced confidence on edge cases.
2. Pause here — fix the Codex issue first.
   <If quota: "Wait until your OpenAI quota resets, or upgrade plan.">
   <If auth:  "Run `/codex:setup` to re-authenticate.">

Reply "claude only" to continue solo, or fix and reply "retry codex".

Preserved so far:
- Claude review findings: <plan file | .tmp/review-<slug>.md>
```

**On "claude only":** Claude does the review solo. The final chat message flags it clearly: `Review complete (Claude-only — Codex was unavailable). Reduced confidence in correctness/edge cases.`

**On "retry codex":** re-run the same Codex call. If it fails again, surface again.

### What Jeremy can check manually

- `/codex:setup` — auth status, whether Codex CLI is reachable.
- `/codex:status` — active/recent Codex jobs.
- `/codex:result <jobId>` / `/codex:cancel <jobId>` — fetch output / kill a stuck job.

## After the review

- Append any meaningful decisions Jeremy made on the judgment calls to `decisions/log.md`.
- Merge happens on Jeremy's say-so (his usual `git push` / PR flow). Never push or merge without an explicit green light — see [[feedback_no_deploy_without_greenlight]].

## Reference: where each model's review strength helps

- **Codex catches:** unstated assumptions, missing failure modes, model-bug root causes, edge cases Claude papers over.
- **Claude catches:** repo-specific context, file-path correctness, UX/edge-case reasoning, long-context coherence, skill/MCP-tool awareness, whether the diff actually matches the plan.

The silent reconcile forces both models' review strengths onto the diff without leaking the messy middle to Jeremy.

## Changelog
- 2026-06-01: created — split out of plan-with-codex so the review is a standalone skill, invokable on any diff (not just plan-with-codex output). plan-with-codex's execute phase now delegates its review phase here.
- 2026-06-01: added Step 3a disputed-findings rebuttal (one round, only on findings the two models disagree on — stops one model silently dropping a real bug the other flagged) and Step 3b targeted fix-verification (the model that raised a finding confirms the specific fix resolves it, before the full re-scan).
