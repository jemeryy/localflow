---
name: session-handoff
description: "Use when the user says \"session handoff\", \"s/h\", \"wrap up session\", \"hand off\", \"handoff summary\", or wants a structured end-of-session summary before clearing context. Writes a handoff markdown file (in this project's handoffs folder) covering decisions, shipped changes, key files, running state, verification steps, deferrals, and open questions so a fresh agent can continue seamlessly. Project-agnostic: it discovers this project's own paths."
---

# Shared project skill adapter

Read [the canonical SKILL.md](../../../.claude/skills/session-handoff/SKILL.md) completely before acting. Follow its workflow, gates and completion criteria. Resolve all relative references from `.claude/skills/session-handoff/`, not this adapter directory, and read the additional resources it requires.

Apply this project's Claude and Codex compatibility rules. Map generic tool and agent roles to the available runtime; keep explicit Claude CLI, Claude-hosted services and cross-provider steps service-specific unless the canonical skill permits an equivalent. Keep the workflow in `.claude`; this file exists only for Codex discovery.
