---
name: plan-with-codex
description: "Use when Jeremy asks to plan a non-trivial project, feature, or refactor with Claude and Codex collaborating. Runs a silent back-and-forth between Claude and Codex until BOTH genuinely agree, then outputs ONLY the final plan. Triggers include \"plan this with codex\", \"let's plan with codex\", \"plan and cross-check with codex\", \"bounce this off codex first\", or any near-equivalent. Do NOT run this for trivial changes (single-file edits, bug fixes with a clear root cause)."
---

# Shared project skill adapter

Read [the canonical SKILL.md](../../../.claude/skills/plan-with-codex/SKILL.md) completely before acting. Follow its workflow, gates and completion criteria. Resolve all relative references from `.claude/skills/plan-with-codex/`, not this adapter directory, and read the additional resources it requires.

Apply this project's Claude and Codex compatibility rules. Map generic tool and agent roles to the available runtime; keep explicit Claude CLI, Claude-hosted services and cross-provider steps service-specific unless the canonical skill permits an equivalent. Keep the workflow in `.claude`; this file exists only for Codex discovery.
