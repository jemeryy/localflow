# DeepFilterNet spike (M9, item 3) - results and verdict

**Date:** 6 July 2026. **Approved by Jeremy** ("we can try the deepfilternet").
**Verdict: NOT adopted into the live pipeline now. Keep the spectral gate as
default-safe.** DeepFilterNet builds and runs fully locally and shows a small,
consistent quality edge on heavy synthetic noise, but the evidence is thin
(synthetic, single clip) and CPU latency is real. Revisit only with a real-room
A/B on Jeremy's mic, and only via an OpenVINO GPU/NPU port if latency matters.

## What was tested

- **DeepFilterNet3** (`deepfilternet==0.5.6`, `deepfilterlib==0.5.6`) in an
  ISOLATED venv at `.tmp/dfspike` (torch 2.2.2+cpu, torchaudio 2.2.2+cpu,
  numpy 1.26.4). Kept out of the main venv on purpose - a first attempt to
  install it into `.venv` downgraded numpy to 1.26 and broke openvino/scipy;
  that was reverted (main venv restored to numpy 2.4.6, no DF packages).
- Build toolchain: DeepFilterNet has no prebuilt wheel for Python 3.12, so its
  Rust extension is compiled from source. Installed the **Rust GNU toolchain**
  (`rustup`, `stable-x86_64-pc-windows-gnu` 1.96.1) and linked against the
  existing **WinLibs mingw64 gcc** (no MSVC / Visual Studio on this machine).
- A/B: a clean TTS sentence ("a quick brown fox jumped over the lazy dog") mixed
  with seeded "fan/room" noise (low-passed rumble + broadband hiss) at 0, 5 and
  10 dB SNR. Each variant transcribed through the REAL OV Whisper engine on GPU;
  word-error-rate measured against the clean-audio transcript.
- Repro scripts persist at `.tmp/dfspike/df_make_noisy.py` (main venv),
  `df_enhance.py` (spike venv), `df_ab_transcribe.py` (main venv).

## Latency (CPU)

| clip | DF enhance (best of 3) | per 5s utterance | RTF |
|------|------------------------|------------------|-----|
| 6s @ 0dB  | 329 ms | 275 ms | 0.05 |
| 6s @ 5dB  | 317 ms | 264 ms | 0.05 |
| 6s @ 10dB | 320 ms | 266 ms | 0.05 |

`init_df()` (model load) is ~0.2-2.6s once. Steady-state DF is ~270ms per 5s of
audio on CPU (8 threads). That is **over the plan's optimistic <=150ms/utterance
gate**, but comfortably inside the overall ~1200ms release-to-paste budget for
short dictations (Whisper STT is ~450ms on GPU). It would add up on multi-minute
dictations and it burns CPU while the rest of the pipeline is on the iGPU.

## Quality (WER vs clean transcript, lower is better)

| SNR | noisy | spectral 0.6 | spectral 0.9 | DeepFilterNet |
|-----|-------|--------------|--------------|---------------|
| 0 dB  | 0.11 | 0.11 | 0.11 | **0.00** |
| 5 dB  | 0.11 | 0.11 | 0.11 | **0.00** |
| 10 dB | 0.00 | 0.00 | 0.00 | 0.00 |

Reading it honestly:
- Whisper is **very** noise-robust on this content. Even at 0 dB SNR the only
  error anywhere was the leading article ("The" heard for "A") - WER 0.11 is one
  word out of nine.
- The spectral gate (both gentle 0.6 and aggressive 0.9) **never fixed** that
  error - it was no better than feeding Whisper the raw noisy audio.
- DeepFilterNet fixed it at every noise level. So DF does clean the onset enough
  to recover a word the spectral gate can't - a real, repeatable edge, but a
  **small** one (one article, one synthetic clip, one synthetic-noise profile,
  one TTS voice).

## Why not adopt now

1. The quality signal is thin: a single-word improvement on synthetic noise.
   Whisper already handles moderate noise, so denoising has little accuracy
   headroom to add on the STT path.
2. Latency isn't free (~270ms/5s CPU), and adopting DF drags **torch (~800MB) +
   a Rust-built lib** into what is meant to be a lean local tool.
3. The plan's adopt gate ("latency <= ~150ms AND transcripts clearly better")
   is not clearly met on either axis.

## If we revisit

- **Real-room A/B first.** Record Jeremy's actual mic in his actual noisy
  condition (fan/keyboard on), then rerun the three scripts. If the spectral
  gate visibly underperforms there and DF clearly wins, reconsider.
- **Adopt as an opt-in, not the default.** It would become
  `denoise_backend = "spectral" | "deepfilter"` with spectral default-safe, and
  the model bundled at install time (never fetched at runtime - the DF weights
  were downloaded once to `%LOCALAPPDATA%\DeepFilterNet\...` during this spike).
- **Port to OpenVINO GPU/NPU** to erase the CPU latency before it goes on the
  live paste path - that is the only way it fits the ~1200ms budget on long
  dictations and keeps the CPU free.

## Housekeeping

- Main venv is clean (DF packages removed, numpy restored). The spike venv at
  `.tmp/dfspike` is disposable (gitignored) - delete it to reclaim the space.
- Pre-existing `torch 2.12 / torchaudio 2.11` remain in the MAIN venv from an
  earlier abandoned attempt; localflow does not import them. Safe to
  `pip uninstall torch torchaudio` from `.venv` to slim it (optional, not done
  here to avoid touching anything unasked).
