---
name: review-with-codex
description: "Use when Jeremy asks to review a code change / diff with Claude and Codex collaborating, OR when invoked automatically at the end of the plan-with-codex execute phase. Runs a silent two-model review (Claude diff review + Codex adversarial review), reconciles both, fixes correctness issues inline, and outputs ONE chat message. Triggers include \"review this with codex\", \"review my changes with codex\", \"adversarial review\", \"cross-check the diff with codex\", \"review before I merge\", or any near-equivalent. Do NOT run this for trivial diffs (single-file doc edits, obvious one-line fixes)."
---

# Shared project skill adapter

Read [the canonical SKILL.md](../../../.claude/skills/review-with-codex/SKILL.md) completely before acting. Follow its workflow, gates and completion criteria. Resolve all relative references from `.claude/skills/review-with-codex/`, not this adapter directory, and read the additional resources it requires.

Apply this project's Claude and Codex compatibility rules. Map generic tool and agent roles to the available runtime; keep explicit Claude CLI, Claude-hosted services and cross-provider steps service-specific unless the canonical skill permits an equivalent. Keep the workflow in `.claude`; this file exists only for Codex discovery.
