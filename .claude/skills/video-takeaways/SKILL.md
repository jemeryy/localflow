---
name: video-takeaways
description: Use when Jeremy shares a YouTube link and asks to analyse it, e.g. "analyse this video", "key takeaways from this youtube video", "what can we implement from this video", or any near-equivalent. Pulls the transcript, extracts the key ideas in plain language, and maps each one to localflow (build / adapt / skip) without building anything.
---

# Video Takeaways

Jeremy sends a YouTube URL and wants two things: what the video actually says, and what (if anything) localflow should do about it.

## Steps

1. **Get the content.** Load the youtube MCP tools via ToolSearch, then:
   - `getVideoDetails` for title, channel, length (so the summary can cite the source properly).
   - `getTranscripts` for the transcript. If no transcript exists, say so plainly and fall back to the video description plus a web search of the title; mark the analysis as lower-confidence.
2. **Extract the takeaways.** 5-10 key points max, in plain language per `.claude/rules/communication-style.md`. Summarize what the SPEAKER claims, not what you agree with. Skip filler, sponsor reads, and generic advice.
3. **Map to localflow.** For each takeaway that could apply, one line each:
   - **Already have it** - name the existing feature/module/skill or memory entry it duplicates.
   - **Worth adapting** - what it would look like for localflow specifically (a personal, fully-local Windows voice-to-text dictation tool; OpenVINO Whisper on Intel Arc; no cloud), and a rough effort guess (hours / days).
   - **Skip** - why it does not fit (breaks the fully-local constraint, wrong scale, not this tool's job).
   Check `plans/` and memory before calling something new. Be critical: most video advice will not survive contact with localflow's constraints (fully local, personal tool), and saying so is the job.
4. **End with a recommendation.** At most 1-2 items worth acting on, or "nothing here worth building". Do NOT start building or create plan files unless Jeremy says so.

## Hard rules

- Diagnosis only: never edit code, create plans, or spawn build agents from this skill.
- Plain language throughout.
- If the video is long (>30 min) and the transcript is huge, digest it in a subagent and keep only the synthesis in the main context.
