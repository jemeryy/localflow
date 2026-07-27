---
name: session-handoff
description: Use when the user says "session handoff", "s/h", "wrap up session", "hand off", "handoff summary", or wants a structured end-of-session summary before clearing context. Writes a handoff markdown file (in this project's handoffs folder) covering decisions, shipped changes, key files, running state, verification steps, deferrals, and open questions so a fresh agent can continue seamlessly. Project-agnostic: it discovers this project's own paths.
---

# Session Handoff

The session handoff is read by the NEXT session, after compacting/clearing context.

Save what this session did (discussions, decisions, code changes, plans) into a
handoff markdown file inside this project's handoffs folder, so the user can
`/compact` or `/clear`, start a fresh agent, read the handoff, and pick up where the
previous session left off without losing continuity. The next agent should be able to
continue from this summary alone, and it should be prompted to the next steps.

This is a **context-handoff artifact**, not a status report. The audience is a future
instance of you, not a stakeholder.

## When to invoke

User says: "session handoff", "s/h" (the shorthand), "wrap up session", "hand off",
"handoff summary", "let's wrap up", "summarize before I clear", or any near-equivalent. Also invoke proactively
if the user says they are about to `/clear` without having run it yet.

## Where to write it (discover this project's convention)

Write to this project's handoffs folder. Discover it, in this order:
1. If the repo already has a handoffs location (a `handoffs/` folder, a `docs/` handoff
   convention, or a path named in this project's `CLAUDE.md`), use that.
2. Otherwise create and use a `handoffs/` folder at the repo root.

One file per session, named `handoff-<YYYY-MM-DD>-<short-slug>.md`. Use absolute paths
INSIDE the file (the next agent may have a different working directory).

## How to produce the summary

1. **Review the full conversation**, not just the last few turns. Handoffs miss things
   when they only summarize recent context.
2. **Pull state from these sources (in order):**
   - Plan files referenced this session (this project's `plans/` folder, if it has one).
   - TodoWrite state — any in-progress or pending tasks.
   - Background processes you started with `run_in_background` — shell IDs are
     load-bearing for the next agent.
   - Files created or modified this session — you know what you touched; don't grep to
     re-discover.
   - Memory files written or updated (this project's persistent-memory directory, if it
     uses one, e.g. `~/.claude/projects/<this-project-slug>/memory/`).
   - Unresolved questions — things you asked the user that never got a clear answer, or
     things the user asked that got deflected.
3. **Do NOT audit the filesystem.** This is synthesis of what happened in THIS session.
   No `git log`, no broad `Glob` sweeps. If you didn't touch it this session, it doesn't
   belong here.
4. **Produce the output in a markdown file.** Write a file. Do not update memory.

## Write the NEXT.md resume pointer

After the handoff doc is written, overwrite `NEXT.md` in the SAME folder you wrote the
handoff doc into (`handoffs/NEXT.md` or `docs/NEXT.md`, whichever this project uses),
with exactly this content and nothing else:

```
# NEXT (auto-written by session-handoff, read by the SessionStart hook)
Updated: <YYYY-MM-DD HH:MM> by session handoff.
Latest handoff: <ABSOLUTE path to the handoff doc just written>
Next step: <same next-step line as below, in shorthand>
```

NEXT.md is gitignored on purpose: do NOT commit it. It is a per-checkout convenience
pointer, regenerated every handoff, and this project's SessionStart hook injects it into
each fresh session so you no longer have to hand-type "read <handoff path>" to resume.

## End with the next step (one prose line, NOT a code block)

After the handoff doc and NEXT.md are written, end your chat message with a single
**next-step line in plain prose** — do NOT wrap it in a fenced code block. Format it as
`Next step: read <handoff-doc absolute path>, then <next action in shorthand>`. The same
line is saved as the final "Next step" section inside the handoff doc, so it survives a
`/clear`.

## Output template — use exactly this structure, every time

```
# Session Handoff — <one-line title of what this session was about>

## Where it started
<2-3 sentences: what the user asked for, key framing or constraints that emerged>

## Important discoveries and decisions
- What you learnt from research or discussion with the user

## Decisions locked + what shipped
- <decision or change> — <why, and where it lives (absolute path if a file)>
- ...

## Key files for next session
- `<absolute path>` — <why the next agent should read this first>
- Plan file: `<path>` (if a plan drove the session)
- Memory files touched: `<paths>` (if any)

## Running state
- Background processes: <shell IDs + what they are + how to kill> — or "none"
- Dev servers / ports: <url + port> — or "none"
- Open worktrees / branches: <paths> — or "none"

## Verification — how to confirm things still work
- `<command>` — <expected outcome>
- ...

## Deferred / pending tasks + open questions
- Deferred/pending: <item> — <why pushed to later>
- Open: <question needing the user's input> — <context>

## Plan
- If there was a plan created tell me the plan's file name is
- Include the overarching plan for the work, or point to the plan's file name

## Next steps
<the most likely next tasks for a fresh agent, likely following the plan>

## Next step (for the next session after /clear)
Next step: read <ABSOLUTE path to THIS handoff doc>, then <next action in shorthand>.
```

## Hard rules

1. **Write to file.** Do not write the handoff inline. Write the handoff to a file and
   tell the path. Never update memory from this skill.
2. **Never invent state.** If a section has nothing to report, write "none" — do not
   omit the section. Structure stability is the whole point.
3. **Absolute paths always.** The next agent may have a different working directory.
4. **If a plan file drove the session, name it first** in "Key files" so the next agent
   reads it before anything else.
5. **No emojis, no hype, no "great job" summaries.** Terse and concrete — paths,
   commands, shell IDs, decisions. Match the tone of a seasoned engineer handing off at
   end-of-shift.
6. **Background process IDs are critical.** If you started any `run_in_background`
   shells, their IDs must appear in "Running state" with the kill command — the next
   agent cannot find them otherwise.
7. **Write NEXT.md and end with the next step.** Overwrite the gitignored `NEXT.md` in
   the handoffs folder with the resume pointer, and end your chat message with a single
   next-step prose line (never a fenced code block).

## Anti-patterns — do not do these

- Summarizing the last 3 turns and calling it a handoff.
- Listing files by relative path.
- Skipping the "Running state" section because "nothing is running" — write "none"
  instead.
- Adding a "what went well / what went poorly" retrospective. This isn't a retro.
