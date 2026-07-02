# LocalFlow

A personal, fully-local voice-to-text dictation tool for Windows (a Wispr Flow clone). Hold a hotkey, speak, release, and the text is typed into whatever app has focus. Transcription runs locally on the Intel Arc iGPU. No audio or text ever leaves the machine.

Built and owned by Jeremy. This is a **personal tool**, not a product for distribution.

## Build spec
The full v1 spec is `docs/PRD.md` (locked, ready to build). The research behind it is `docs/research-briefing.html`. Build to the PRD; the build order is milestones M1 to M5 in the PRD's section 9.

**Start with M1 first:** a throwaway script that proves OpenVINO Whisper runs on the Intel Arc 140V iGPU and prints which device it used, before building any UI or hotkey. That is the one load-bearing risk; everything after it is low-risk plumbing.

**Build status (2 July 2026): M1 and M2 DONE, fully signed off — do not redo either (`docs/m1-results.md`, `docs/m2-results.md`).** The real `localflow/core` + `localflow/desktop` packages exist (installed editable, `pip install -e .[dev]`), all 7 tests pass, GPU+NPU verified, FR-4 silence gate and real-mic dictation verified. The M1 GPU trailing-word issue is CLOSED as a TTS artifact (real trailed-off speech transcribes identically on GPU and NPU) — **GPU is the primary STT device**. The OpenVINO compile cache lives at `models/ov_cache` (NOT `.tmp/` — the old handoff doc predates this move). Next milestone: M3 (push-to-talk hotkey + clipboard injection, AT-1/2/3) — session handoff with the M3 plan: `plans/handoff-2026-07-02-m2-complete.md`.

## Hard constraints (from the PRD, do not violate)
- **Fully local.** No network calls for transcription. No cloud speech-to-text. No audio or text leaves the device.
- **The core seam.** `localflow/core/` is a pure Python package: audio in, text out. It must NOT import the desktop layer, touch the clipboard or keyboard, or make network calls. This is the seam that lets FARA and other projects reuse the exact same transcription code later.
- **The hardware.** Target machine is an Intel Core Ultra (Lunar Lake), Intel Arc 140V iGPU + NPU. No NVIDIA, no CUDA. Log which device (GPU / NPU / CPU) actually ran at startup.
- **Ollama cannot transcribe.** It is only ever the optional cleanup LLM (removes filler words, fixes punctuation). The transcriber is Whisper on OpenVINO.

## Scope
- v1 = personal Windows desktop tool (PRD sections 1 to 10).
- Phase 2 (FARA web integration) and Phase 3 (cloud) are DESIGNED FOR but NOT BUILT in v1. Keep the core seam clean; build nothing for them yet.

## Note on the parent folder
This repo sits under `C:\Users\jemer\code\`, which has a parent CLAUDE.md describing a "WAT framework". That does not apply here. LocalFlow is a standalone Python desktop app; this file takes precedence.
