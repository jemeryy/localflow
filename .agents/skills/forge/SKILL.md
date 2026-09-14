---
name: forge
description: "Use when Jeremy wants to design or pressure-test a non-trivial technical build - an architecture, a refactor, a system design, a \"how should I build X\" decision with competing approaches or costly blind spots. Runs a multi-perspective, cross-model adversarial design pipeline: five engineering lenses (Architect, Shipper, Breaker, Maintainer, Integrator) in parallel PLUS an independent Codex design pass, then a contradiction/debate map, a synthesized design decision (chosen approach + rejected alternatives + failure modes + build plan), and an adversarial break-test before delivering. Triggers include \"forge this\", \"design this build\", \"architect this\", \"how should I build X\", \"pressure-test this design\", \"design review this\". This is the technical-design sibling of council (strategy) and storm-research (external research). It decides the DESIGN; it does not write production code or deploy. Skip it for trivial changes with an obvious approach - just build, or use plan-with-codex to lock a linear plan."
---

# Shared project skill adapter

Read [the canonical SKILL.md](../../../.claude/skills/forge/SKILL.md) completely before acting. Follow its workflow, gates and completion criteria. Resolve all relative references from `.claude/skills/forge/`, not this adapter directory, and read the additional resources it requires.

Apply this project's Claude and Codex compatibility rules. Map generic tool and agent roles to the available runtime; keep explicit Claude CLI, Claude-hosted services and cross-provider steps service-specific unless the canonical skill permits an equivalent. Keep the workflow in `.claude`; this file exists only for Codex discovery.
