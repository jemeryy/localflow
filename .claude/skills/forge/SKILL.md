---
name: forge
description: Use when Jeremy wants to design or pressure-test a non-trivial technical build - an architecture, a refactor, a system design, a "how should I build X" decision with competing approaches or costly blind spots. Runs a multi-perspective, cross-model adversarial design pipeline: five engineering lenses (Architect, Shipper, Breaker, Maintainer, Integrator) in parallel PLUS an independent Codex design pass, then a contradiction/debate map, a synthesized design decision (chosen approach + rejected alternatives + failure modes + build plan), and an adversarial break-test before delivering. Triggers include "forge this", "design this build", "architect this", "how should I build X", "pressure-test this design", "design review this". This is the technical-design sibling of council (strategy) and storm-research (external research). It decides the DESIGN; it does not write production code or deploy. Skip it for trivial changes with an obvious approach - just build, or use plan-with-codex to lock a linear plan.
argument-hint: "[the build or design problem]"
---

# Forge

Turns one technical design problem into a decided, break-tested design + build plan, by running it past five engineering lenses AND a second model (Codex), mapping where they disagree, and adversarially trying to break the winner before delivering.

The core idea (borrowed from the STORM method): one angle on a design has blind spots. More independent perspectives that actively contradict each other produce a more robust design. Codex is a genuinely different model, so it kills blind spots a single-model panel shares. Run the whole pipeline; do not shortcut a phase. This is heavier than just designing it yourself; that is the point. It is also more expensive (~6-8 subagents + 1-2 Codex calls) - reserve it for genuinely hard or ambiguous design decisions.

**Where forge fits:** `council` = strategy / business decisions. `storm-research` = external research with citations. **forge** = internal technical design. Forge decides the design; hand the resulting build plan to the normal loop (lock with `plan-with-codex` if needed -> execute -> `review-with-codex`). Forge writes NO production code and never deploys.

## Phase 0: Scope the design problem

1. Restate in one line: the goal, and what "done and good" looks like (success criteria).
2. List the hard constraints that actually bind here - fully-local (no network for transcription), the pure `localflow/core` seam (no desktop / clipboard / keyboard / network imports), the Intel Arc GPU/NPU target (no CUDA), real-time latency, existing behaviour that can't break.
3. **Read the real code.** Open the files/modules this design touches. Ground every later step in the actual repo, not generic advice. Note the surrounding idiom so the design matches it.
4. State your assumptions in 1-2 lines. Only ask a clarifying question if the problem is genuinely ambiguous in a way that changes the design; otherwise proceed.

## Phase 1: Five engineering lenses (parallel agents)

Spawn **five `general-purpose` agents in a single message** so they run concurrently. Each gets the same problem framing + the relevant repo context + its lens. Each returns EXACTLY: 1) PREFERRED APPROACH in 2 sentences. 2) 3-5 concrete design points or tradeoffs specific to this repo. 3) THE ONE THING only this lens flags. 4) Its single biggest objection to the naive/obvious approach. Under ~350 words.

1. **THE ARCHITECT** - module boundaries, data flow, simplicity, one source of truth (no duplicated state that can drift). Minimize accidental complexity; make the shape easy to reason about.
2. **THE SHIPPER** - the smallest thing that delivers the value. YAGNI, cut scope, time-to-value. Actively challenge over-engineering; call out anything built for a future that may not come.
3. **THE BREAKER** - failure modes and edge cases. Concurrency and races (this repo has multiple live Claude sessions on one tree), partial failures, bad input, data loss, security. Name the worst realistic input/timing.
4. **THE MAINTAINER** - future-you in 6 months. Readability, testability, drift, migration and rollback, the cost to keep it alive. Does it match the surrounding code's conventions?
5. **THE INTEGRATOR / OPERATOR** - fit with existing code and deploy reality. Rollback path, smoke-testing before deploy, observability, and the greenlight/deploy-safety rules. How does this ship without breaking live?

When all five return, post a 2-3 line note in chat: where they converge, and the sharpest disagreement. Keep the raw briefs out of chat.

## Phase 1b: Independent Codex design pass (cross-model)

Invoke Codex the **same way `plan-with-codex` does** - fresh thread per Claude session with a context preamble, 10-min Bash timeout, `--resume-last` within the session (see `.claude/skills/plan-with-codex/SKILL.md` "Codex session hygiene" and "Failure handling"). Ask Codex to, independently and without seeing the five briefs first: propose its own preferred design for this problem, name the top failure mode it sees, and say where it would disagree with the obvious approach.

If Codex is unavailable (quota/auth/timeout), degrade to Claude-only and carry a **confidence-reduced** flag through to the output, per the plan-with-codex failure tree. Do not silently drop the cross-model check.

## Phase 2: Contradiction + debate map

Working from the five lenses + Codex (inline, no new agents unless a debate round is triggered):

1. **Direct conflicts** - name the specific clashing design choices, not just topics.
2. **Strongest vs weakest reasoning** - which position is best-grounded in the actual constraints, which is weakest, and why.
3. **The resolving question** - the single question that decides the biggest fork.
4. **Universal agreement** - the design decisions every lens (and Codex) share. These are load-bearing; treat them as settled.
5. **The blind spot** - what NO lens addressed (the missing 6th lens).

**Debate round (only if two or more approaches genuinely compete):** spawn two agents - or pit Claude against Codex - one arguing hardest FOR approach A, one FOR approach B, each rebutting the other's strongest point once. Converge on which wins under the stated constraints, or state the tradeoff crisply if it is a real judgment call for Jeremy. This is the point of forge over a single planning pass: let the strongest opposing designs actually fight before you pick.

## Phase 3: Synthesize the design decision

Produce, inline:

- **Chosen approach** + why, tied to the specific constraints from Phase 0.
- **Rejected alternatives** + why-not, explicitly (so the decision is auditable).
- **Key risks / failure modes** + a mitigation for each.
- **Build plan** - ordered steps, the files each step touches, the tests/verification for each, and the rollback path. Sequenced enough to hand straight to an executor.

## Phase 4: Adversarial break-test (do not skip)

The builder is not the judge. Spawn ONE hard skeptic - a `general-purpose` agent, and/or Codex - whose only job is: given the chosen design + build plan, find where it breaks. The worst edge case, the concurrency or deploy hazard, the hidden coupling, the thing that bites in 3 months. Then apply fixes to the design or add explicit caveats. A design delivered without this pass is not a forge design.

## Output

1. A design doc in markdown. If this is a real build that will get a row in `plans/`, write `plans/YYYY-MM-DD-<slug>.md` from `templates/plan.md` with `status: active`; otherwise a design note in the relevant project folder (or inline in chat for a small one). Include: the decision, rejected alternatives, risks + mitigations, the build plan, and any open judgment calls for Jeremy.
2. In chat, tight: the recommendation, the sharpest tradeoff, the top surviving risk, and any judgment call that needs Jeremy.
3. Hand-off line: forge decided the design; to build it, run the normal loop (`plan-with-codex` to lock the sequence if the plan is non-trivial -> execute -> `review-with-codex`).

## Notes & guardrails

- **No workflows.** Per CLAUDE.md, use the `Agent` tool for subagents; do not use the Workflow tool.
- **Ground everything in the real repo.** Read the code first; every design point must reference what is actually there, not textbook advice.
- **Forge proposes; Jeremy disposes.** It does not write production code and never pushes or deploys. Respect the multi-session git rules and the no-deploy-without-greenlight rule.
- **Cost discipline.** ~6-8 subagents + 1-2 Codex calls per run. For a trivial change with an obvious approach, skip forge - just build it, or use `plan-with-codex`.
- **Codex is the cross-model check, not decoration.** If it is skipped, say so and lower the stated confidence.
