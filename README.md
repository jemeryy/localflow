# LocalFlow

A personal, fully-local voice-to-text dictation tool for Windows. Hold a hotkey, speak, release, and your words are typed into whatever app has focus. Transcription runs locally on the Intel Arc iGPU (OpenVINO Whisper). No audio or text ever leaves the machine.

The transcription logic lives in a standalone `localflow.core` package (audio in, text out), so FARA and future projects can reuse the same code.

## Status
Not yet built. The full spec is in [`docs/PRD.md`](docs/PRD.md). Build order is in the PRD, section 9. Start with milestone M1 (prove OpenVINO Whisper on the Intel Arc iGPU).

## Research
The design is grounded in a research briefing: [`docs/research-briefing.html`](docs/research-briefing.html).
