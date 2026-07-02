"""M1 throwaway proof: OpenVINO Whisper on the Intel Arc 140V iGPU.

Loads an exported OpenVINO Whisper model, transcribes a WAV file, and prints
which OpenVINO device actually ran the model plus timing. No UI, no hotkey,
no clipboard — this exists only to retire risk R1 from the PRD.

Usage:
    python scripts/m1_prove_igpu.py [--model models/whisper-turbo-ov]
                                    [--wav .tmp/test_utterance.wav]
                                    [--device GPU]
"""

import argparse
import sys
import time

import numpy as np
import openvino as ov
import openvino_genai as ov_genai
import soundfile as sf


def list_devices(core: ov.Core) -> None:
    print(f"OpenVINO {ov.get_version()}")
    print("Available devices:")
    for dev in core.available_devices:
        name = core.get_property(dev, "FULL_DEVICE_NAME")
        print(f"  {dev}: {name}")
    print()


def load_wav_16k_mono(path: str) -> tuple[np.ndarray, float]:
    audio, rate = sf.read(path, dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if rate != 16000:
        sys.exit(f"Expected 16kHz WAV, got {rate}Hz: {path}")
    return audio, len(audio) / rate


def try_pipeline(model_dir: str, device: str) -> tuple[ov_genai.WhisperPipeline | None, float]:
    t0 = time.perf_counter()
    try:
        pipe = ov_genai.WhisperPipeline(model_dir, device)
        return pipe, time.perf_counter() - t0
    except Exception as e:
        print(f"[!] Pipeline init on {device} FAILED: {type(e).__name__}: {e}\n")
        return None, time.perf_counter() - t0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="models/whisper-turbo-ov")
    ap.add_argument("--wav", default=".tmp/test_utterance.wav")
    ap.add_argument("--device", default="GPU")
    args = ap.parse_args()

    core = ov.Core()
    list_devices(core)

    audio, duration_s = load_wav_16k_mono(args.wav)
    print(f"Audio: {args.wav} ({duration_s:.1f}s @ 16kHz mono)\n")

    device_used = args.device
    pipe, load_s = try_pipeline(args.model, args.device)
    if pipe is None and args.device != "CPU":
        print("[!] Falling back to CPU...\n")
        device_used = "CPU"
        pipe, load_s = try_pipeline(args.model, "CPU")
    if pipe is None:
        sys.exit("FAIL: pipeline would not initialise on any device.")

    print(f"Pipeline loaded+compiled on {device_used} in {load_s:.1f}s")

    config = pipe.get_generation_config()
    config.language = "<|en|>"
    config.task = "transcribe"

    # Run 1 includes first-inference warmup; run 2 is representative latency.
    for run in (1, 2):
        t0 = time.perf_counter()
        result = pipe.generate(audio, config)
        stt_ms = int((time.perf_counter() - t0) * 1000)
        label = "warmup" if run == 1 else "steady"
        print(f"Run {run} ({label}): {stt_ms}ms for {duration_s:.1f}s audio")

    text = str(result).strip()
    print(f"\nDEVICE USED : {device_used}")
    print(f"TRANSCRIPT  : {text}")
    print("\nM1 PROOF: " + ("PASS (ran on iGPU)" if device_used == "GPU" else
                            f"PARTIAL (ran on {device_used}, not GPU)"))


if __name__ == "__main__":
    main()
