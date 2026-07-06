---
name: self-drive
description: Use when Jeremy says "self-drive <plan>", "self drive this", "drive the plan to done", "run self-drive on <plan>", or asks to autonomously execute a LOCKED plan end to end (build it, verify it the way a real user would, find bugs, fix them, repeat) until the plan's stated DEFINITION OF DONE is reached. It stops only for a real decision, a blocker, or a cost cap (a context checkpoint is a handoff, not a stop). NEVER ships, pushes, publishes, or deploys. Not for one-off single edits. Project-agnostic: it drives any project by using that project's own test and run commands.
---

# Self-Drive (autonomous build -> verify -> fix loop, to a plan's definition of done)

Point this at a LOCKED plan and it drives that plan to its stated outcome without
per-step prompting: re-ground from files, pick the next not-DONE step, build it,
verify it the way a real user would, fix the bugs it finds, commit to the working
branch, mark the step DONE, repeat, until the plan's **definition of done** is
reached. It STOPS at the first thing that genuinely needs Jeremy.

This skill is the goal-driver: the plan's **definition of done** IS the goal, and the
loop runs until that outcome is demonstrably met, not merely until the step list is
empty.

It is **project-agnostic**. It assumes no project-specific runner. It uses THIS
project's own commands, which you discover from the repo (see "Ground the project").
All durable state lives in files (the plan + a session handoff), so a compaction or a
`/loop` restart never loses a step count or a decision.

## The locked constraints (hold all of these)
1. **Stop BEFORE ship.** End at built + verified + committed to the working branch +
   handoff updated. NEVER push to a remote, tag a release, publish, or run any deploy
   command. See "The ship line" below.
2. **Simple judgment calls** go to the cleanest, lowest-future-risk fix (usually fix
   the root cause now, not a patch over it).
3. **Anything UX / UI / user-facing, or any real decision: STOP and surface.** Never
   guess a decision that is Jeremy's to make.
4. **Verify like a real user.** Do not mark a step done on "the code looks right."
   Actually exercise the changed behaviour with this project's own tools (run the
   tests/gate; if there is a UI, drive the changed flow and look at it; if it is a CLI
   or library, run it and check the output). Fix objective breakage; surface
   subjective design.
5. **Cost / irreversibility cap.** Do not spend real money or take any outward or
   hard-to-reverse action unless the plan explicitly authorizes it with a cap. If a
   verify needs a paid call, flag the projected spend BEFORE spending and stop at the
   cap (STOP-D). Default: $0, local-only.
6. **Recover from files, not memory.** All state is in the plan + the handoff. On
   resume, re-read them; never trust in-context counters (they do not survive a
   compaction).

## Ground the project (do this once at the start of a run)
Before building, read this repo's `CLAUDE.md` / `README` / build files
(`pyproject.toml`, `package.json`, `Makefile`, etc.) and record, in the handoff:
- **Test / gate command** — how this project proves a change is correct (e.g.
  `pytest`, `python -m pytest tests/`, `npm test`, a lint + typecheck gate).
- **Run command** — how to actually run the thing a step changes (the CLI entry,
  `python -m <pkg>`, a dev server + URL, a script). This is how you verify like a user
  (constraint 4).
- **The ship line for THIS repo** — the exact commands that would push / deploy /
  publish (e.g. `git push`, a deploy script, `railway up`, `npm publish`, an Apps
  Script deploy). You will NOT run these; list them so you recognise them.
- **Working branch** — never commit straight onto a branch the project treats as
  "live" / protected if a feature branch is expected; if unsure, STOP and ask.
If any of these cannot be determined and a step needs it, that is a STOP-C.

## Precondition: the plan must be execution-ready
Do not build unless the plan is LOCKED and declares:
- an overall **definition of done** (the desired outcome in concrete, checkable terms:
  what the finished thing does, and how you can tell it is finished), and
- a per-step **verify** note for every buildable step: the concrete check that proves
  that step works (a test command, a flow to drive + what to assert, or an expected
  output). A step whose done-check is subjective or not machine-checkable is marked
  `verify: human` and the loop STOPS on it (STOP-C), never builds it blind.
If the plan is not execution-ready, go to Phase 0.

## Phase 0 — planning (only when the plan is NOT execution-ready)
Do NOT build. Instead:
1. If this project has the `plan-with-codex` skill, run it to converge a LOCKED plan
   with (a) a definition of done and (b) a verify note per buildable step. Otherwise
   draft the plan yourself and lock it.
2. Surface the locked plan to Jeremy and STOP (**STOP-C**: plan approval is his). Do
   NOT flow planning straight into an unattended build.

## Phase 1 — the iteration cycle (repeat until a STOP condition)
1. **Re-ground from files.** Read the plan + the handoff. Confirm which steps are DONE
   (by their commit refs) and the working-tree state (`git status`). If the recorded
   state contradicts git in a way you cannot reconcile -> **STOP-B** (do not guess).
2. **Pick the step.** The lowest-numbered not-DONE step. If none remain AND the
   definition of done is demonstrably met -> **STOP-A** (report in plain language). If
   none remain but the outcome is NOT reached -> **STOP-C** (surface the gap).
3. **human-verify step?** If the step's verify is `human` (subjective / not
   machine-checkable) -> **STOP-C**. Do not build it blind.
4. **Build it.** The smallest clean change that fixes the root cause (constraint 2).
   Delegate a heavy build to a subagent that writes into the repo by absolute path;
   you keep the small result (done / commit ref / short report). Commit NEW files
   early by explicit path so they are not lost.
5. **Verify** (constraint 4), in order:
   a. Run the project's **test / gate command**. It must pass.
   b. **Exercise the change like a user** with the project's **run command**: drive
      the changed flow (a UI flow via a browser driver or a script; a CLI / module by
      running it) and check the real behaviour and output, not just that it launched.
      If the change is visual, capture and look at it; fix objective breakage, surface
      subjective design (STOP-C).
   c. If a verify would spend money or do anything outward / irreversible, apply
      constraint 5: flag first, stop at the cap (STOP-D). Default local + $0.
6. **On fail:** fix toward the cleanest solution and re-verify. Keep a round count in
   the handoff; if you hit the plan's (or a sensible default) round cap without green
   -> **STOP-B** (report what you tried). Fix objective breakage; STOP-C subjective
   design calls.
7. **On pass:** if this project has `review-with-codex`, run it on the diff and address
   anything BLOCKING. Then commit to the working branch by explicit path, record the
   step DONE with its commit ref in the plan / handoff, and refresh the handoff
   (branch, plan path, current step, last commit, status).
8. Continue at the next step.

## The ship line (constraint 1, never cross it)
Before running any command you are unsure about, ask: does this push, publish, tag,
release, or deploy? If yes, DO NOT run it. This includes (recognise the repo's own
form from "Ground the project"): `git push`, `git tag` / release, a deploy script,
`railway up`, `npm publish`, `docker push`, an Apps Script deploy, any "go live" step.
The loop ends at a committed feature branch + an updated handoff. Jeremy tests, ships,
and deploys.

## Context + handoff policy (lean by design, lossless by files)
You cannot read your own context %, and you cannot compact yourself. So keep the
orchestrator lean (offload heavy build / verify to subagents that return small
results) and make every stopping point a fresh **handoff**: at the start of a run,
after each step marked DONE, and before any long build, rewrite the session handoff so
it always points at exactly where the loop is (working branch, plan path, current
step, last commit, round count, the discovered test / run / ship commands, and any
pending decision). Because all state is in files, a compaction or a `/clear` +
re-invoke of `self-drive` resumes with nothing lost. Jeremy's standing "hand off near
~20% context" rule is satisfied by this continuous fresh-handoff, not by halting. (Use
the `session-handoff` skill if the project has it.)

## Jeremy can interject any time
He may stop, comment, or redirect at any point. Record his direction into the handoff
(so it survives a compaction), fold it into the current step, then continue toward the
same definition of done. Never lose his instruction across a compaction.

## STOP conditions (each: write the report into the handoff, then end the turn)
- **A — done:** steps exhausted AND the definition of done is demonstrably reached. End
  with a plain-language summary (simple words, no jargon): what got built, what it does
  for the user, and any decisions still left for Jeremy. The loop NEVER ships.
- **B — blocked:** stuck after the round cap, a contended file, or an unreconcilable
  state. Report in plain words what you tried.
- **C — needs a decision:** a real / UX / user-facing call, a `human`-verify step, or a
  "steps done but outcome not reached" gap. Surface it (with a screenshot if visual).
- **D — cost:** a paid or irreversible action is required and near / over the
  authorized cap. Flag the projected spend; do not exceed it.

## Notes
- If the project has a `decisions/log.md` (or similar) convention, append one line per
  MEANINGFUL decision (root-cause fix choice, fix-vs-surface call, each STOP and why,
  any paid spend with the amount). Routine "ran the tests, they passed" is not a
  decision.
- First runs are ATTENDED (Jeremy watches). Later unattended runs (via `/loop`
  re-invoking this skill) re-ground from files exactly like the compaction path.
- Not for trivial one-off edits: if the task is a single clear change, just make it.
