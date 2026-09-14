---
name: self-drive
description: "Use when Jeremy says \"self-drive plan\", \"self drive this\", \"drive the plan to done\", \"run self-drive on plan\", or asks to autonomously execute a LOCKED plan end to end (build it, verify it the way a real user would, find bugs, fix them, repeat) until the plan's stated DEFINITION OF DONE is reached. It stops only for a real decision, a blocker, or a cost cap (a context checkpoint is a handoff, not a stop). NEVER ships, pushes, publishes, or deploys. Not for one-off single edits. Project-agnostic: it drives any project by using that project's own test and run commands."
---

# Shared project skill adapter

Read [the canonical SKILL.md](../../../.claude/skills/self-drive/SKILL.md) completely before acting. Follow its workflow, gates and completion criteria. Resolve all relative references from `.claude/skills/self-drive/`, not this adapter directory, and read the additional resources it requires.

Apply this project's Claude and Codex compatibility rules. Map generic tool and agent roles to the available runtime; keep explicit Claude CLI, Claude-hosted services and cross-provider steps service-specific unless the canonical skill permits an equivalent. Keep the workflow in `.claude`; this file exists only for Codex discovery.
