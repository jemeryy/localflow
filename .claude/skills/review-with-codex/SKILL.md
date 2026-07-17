---
name: review-with-codex
description: Use when Jeremy asks to review a code change / diff with Claude and Codex collaborating, OR when invoked automatically at the end of the plan-with-codex execute phase. Runs a silent two-model review (Claude diff review + Codex adversarial review), reconciles both, fixes correctness issues inline, and outputs ONE chat message. Triggers include "review this with codex", "review my changes with codex", "adversarial review", "cross-check the diff with codex", "review before I merge", or any near-equivalent. Do NOT run this for trivial diffs (single-file doc edits, obvious one-line fixes).
---

# Review with Codex (silent two-model review)

Claude and Codex review the diff in the background, reconcile their findings, fix correctness issues inline, and Jeremy sees ONE final message. Same two principles as plan-with-codex: silent debate, single final output.

**Fresh unbiased reviewers (the point of this skill).** Both reviews are run by agents that did NOT author the diff, so neither is defending its own work. The Claude review runs in a fresh subagent (clean context, not the session that wrote the code). The Codex review runs in its own fresh Codex thread, NOT a resume of the plan-debate session, so it never helped shape the plan it's now attacking. Both are still *informed*: each reads the plan file and the diff, so they can catch "the diff doesn't match the plan." Fresh means non-author, not blind. The main Claude session becomes the orchestrator here: it hands the diff + plan to the two fresh reviewers, then reconciles and applies fixes. On the disputed bucket it leans toward the reviewers, since it (or its executor) is the author.

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

## How to call Codex (the invocation contract)

Every Codex call in this skill goes through the hardened wrapper `tools/codex_exec.py`. Do NOT use the `/codex:*` slash commands: they are user-only (marked `disable-model-invocation`), so the model cannot run them, and calling them silently fails.

```
python tools/codex_exec.py --prompt-file <f> [--resume-last] --effort high [--write]
```

- Availability preflight: `python tools/codex_exec.py --pong` (prints PONG).
- Write the prompt to a file under `.tmp/` and pass `--prompt-file` (no argv-length limit; the wrapper pipes it via stdin).
- Review calls are read-only: never pass `--write` for a review.
- Run every round SYNCHRONOUSLY (foreground Bash, 10-min timeout). Background rounds fire a task-notification at Jeremy, which breaks the silent-loop contract.
- Exit codes: 0 ok, 3 usage-limit, 4 timeout, 5 auth, 6 codex CLI not found, 1 other failure (the wrapper already retried once if transient). Branch on the exit code, not on stderr keywords.

**Independence (the whole point).** This review ALWAYS opens its own fresh Codex thread for the adversarial review: omit `--resume-last` and include the project preamble below, EVEN IF plan-with-codex already ran Codex earlier in this session. A reviewer that helped write the plan is not a fresh reviewer. So: plan debate = Codex thread A; this review = a new Codex thread B. The review's OWN later rounds (rebuttal Step 3a, fix-verify Step 3b, the full re-scan) use `--resume-last` to resume thread B, so the reviewer keeps its own context across the review, just not the plan-debate context.

**The context preamble (mandatory on the fresh review call).** Codex starts with zero knowledge of the project. Lead the fresh prompt with:

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

### Step 1 — Fresh Claude subagent reviews the diff (silent)

Spawn a fresh reviewer via the Agent tool (strong model — Opus/Fable-class for code review). This subagent did NOT author the diff, so it has no stake in defending it. Do NOT review inline in the orchestrating session; that session is (or ran) the author.

Give the subagent, in its prompt, everything it needs but nothing about how the code was written:
- The task: adversarial pre-merge review of the working-tree diff against `main`. Return findings only, do not edit files.
- The diff: `git diff main` (paste it, or tell it to run that read-only command).
- The plan file path (if one exists) so it can cross-check every plan step against the diff: file-path mistakes, missed steps, UX issues, regressions, broken imports. If standalone, review for correctness, edge cases, regressions, and anything that contradicts the change's stated intent.
- Ask it to return structured findings (BLOCKING / RISKS / MISSING, with file:line refs) as its final message.

The orchestrating session captures the subagent's returned findings verbatim into `## Pre-merge review — Claude` (plan file or `.tmp/review-<slug>.md`). The subagent is the reviewer; the orchestrator only records and later reconciles.

### Step 2 — Fresh Codex adversarial review (silent)

Invoke `tools/codex_exec.py` on a FRESH thread (omit `--resume-last`, include the project preamble — see "Independence" above; `--effort high`, review-only — no `--write`) with an adversarial-review prompt over the diff against main: attack correctness, edge cases, failure modes, and anything the change's stated intent contradicts; return BLOCKING / RISKS / MISSING findings with file:line references. Reference the plan file (or the changed-file list).

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
2. **Full re-scan** — once all targeted fixes verify, re-run the Codex adversarial-review once (on thread B, `--resume-last`) to catch anything the fixes newly broke. If new blocking issues appear, fix and re-run.

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

`codex_exec.py` detects, classifies, and retries transient failures once. Branch on its exit code: 0 ok, 3 usage-limit (do NOT retry; surface), 4 timeout (already retried; surface), 5 auth (surface with `/codex:setup`), 6 codex CLI not found (surface install hint), 1 other (already retried; surface). The one self-handled case is malformed-but-exit-0 output (missing the BLOCKING/RISKS structure): retry ONCE with `--effort medium`, then surface.

**Surface message (the only chat message on failure):**

```
Codex unavailable during adversarial review.

Reason: <usage limit | auth expired | timeout | not found | error>
<one line of detail>

Options:
1. Continue Claude-only — I'll finish the review using just the fresh Claude reviewer.
   Single-perspective, so reduced confidence on edge cases.
2. Pause here — fix the Codex issue first.
   <If auth: "Run /codex:setup to re-authenticate.">
   <If usage limit: "Wait for the reset, or upgrade.">

Reply "claude only" to continue solo, or fix and reply "retry codex".

Preserved so far: <plan/audit file path + what completed>
```

**On "claude only":** the fresh Claude reviewer's findings carry the review; the final chat message flags it: `Review complete (Claude-only — Codex was unavailable). Reduced confidence in correctness/edge cases.`

**On "retry codex":** re-run the same Codex call. If it fails again, surface again.

### What Jeremy can check manually

- `/codex:setup` — auth status, whether Codex CLI is reachable.
- `/codex:status` / `/codex:result <jobId>` / `/codex:cancel <jobId>` — job status / output / kill.

## After the review

- Append any meaningful decisions Jeremy made on the judgment calls to `decisions/log.md`.
- Merge happens on Jeremy's say-so (his usual `git push` / PR flow). Never push or merge without an explicit green light.

## Reference: where each model's review strength helps

- **Codex catches:** unstated assumptions, missing failure modes, model-bug root causes, edge cases Claude papers over.
- **Claude catches:** repo-specific context, file-path correctness, UX/edge-case reasoning, long-context coherence, skill/MCP-tool awareness, whether the diff actually matches the plan.

The silent reconcile forces both models' review strengths onto the diff without leaking the messy middle to Jeremy.

## Changelog
- 2026-07-18: de-drifted to the canonical generic review-with-codex + fresh non-author reviewers. Codex is now invoked via the hardened `tools/codex_exec.py` wrapper (the old `/codex:adversarial-review` slash-command path could not be run by the model at all). The Claude review runs in a fresh subagent and the Codex review opens its own fresh thread (independence). Both stay informed (they read the plan + diff); fresh means non-author, not blind.
- 2026-06-01: created — split out of plan-with-codex so the review is a standalone skill, invokable on any diff. Step 3a disputed-findings rebuttal + Step 3b targeted fix-verification.
