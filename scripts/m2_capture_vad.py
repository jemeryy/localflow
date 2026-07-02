"""M2 acceptance: mic -> 16kHz mono -> Silero VAD trim -> Whisper on the iGPU.

Modes:
  (default)          record N seconds from the mic, transcribe, print the result
  --wav PATH         skip the mic and run the core pipeline on a WAV file.
                     This path never imports localflow.desktop (AT-7 shape).
  --expect-silence   exit 1 if any text was produced (FR-4 silence-gate check)

Examples:
  python scripts/m2_capture_vad.py --seconds 6 --save .tmp/m2_take1.wav
  python scripts/m2_capture_vad.py --wav .tmp/m2_take1.wav --device NPU
  python scripts/m2_capture_vad.py --seconds 4 --expect-silence   # stay quiet
  python scripts/m2_capture_vad.py --list-devices
"""

from __future__ import annotations

import argparse
import logging
import sys


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seconds", type=float, default=6.0,
                    help="mic recording length (default 6)")
    ap.add_argument("--model", default="models/whisper-turbo-ov")
    ap.add_argument("--device", default="GPU", help="GPU | NPU | CPU")
    ap.add_argument("--fallback", default="CPU")
    ap.add_argument("--cache", default="models/ov_cache",
                    help="OpenVINO compile cache dir")
    ap.add_argument("--wav", default=None,
                    help="transcribe this WAV instead of recording")
    ap.add_argument("--save", default=None,
                    help="save the raw mic capture to this WAV path")
    ap.add_argument("--input-device", default=None,
                    help="sounddevice input device index or name substring")
    ap.add_argument("--no-vad", action="store_true",
                    help="skip the VAD gate (debugging only)")
    ap.add_argument("--expect-silence", action="store_true",
                    help="exit 1 if any text was produced")
    ap.add_argument("--list-devices", action="store_true",
                    help="list audio input devices and exit")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)-7s %(name)s: %(message)s")

    if args.list_devices:
        import sounddevice as sd

        print(sd.query_devices())
        return 0

    from localflow.core import SttEngine, VadGate, ensure_16k_mono, transcribe_audio

    engine = SttEngine(args.model, device=args.device,
                       fallback_device=args.fallback, cache_dir=args.cache)
    vad = None if args.no_vad else VadGate()

    if args.wav:
        import soundfile as sf

        audio, rate = sf.read(args.wav, dtype="float32")
        print(f"\nInput: {args.wav} ({len(audio) / rate:.2f}s @ {rate}Hz)")
    else:
        # Imported lazily so --wav runs stay pure-core (AT-7).
        from localflow.desktop import record_audio

        input_device: int | str | None = args.input_device
        if input_device is not None and input_device.isdigit():
            input_device = int(input_device)
        print(f"\n>>> Recording for {args.seconds:.0f}s — speak now <<<")
        audio, rate = record_audio(args.seconds, input_device=input_device)
        peak = float(abs(audio).max()) if len(audio) else 0.0
        print(f"Captured {len(audio) / rate:.2f}s @ {rate}Hz (peak {peak:.3f})")
        if args.save:
            import soundfile as sf

            sf.write(args.save, audio, rate, subtype="FLOAT")
            print(f"Saved raw capture -> {args.save}")

    audio16 = ensure_16k_mono(audio, rate)
    result = transcribe_audio(audio16, engine, vad=vad)

    print("\nTranscriptResult")
    print(f"  raw_text   : {result.raw_text!r}")
    print(f"  clean_text : {result.clean_text!r}")
    print(f"  duration_s : {result.duration_s}")
    print(f"  stt_ms     : {result.stt_ms}")
    print(f"  cleanup_ms : {result.cleanup_ms}")
    print(f"  device     : {result.device}")
    print(f"  model      : {result.model}")

    if not result.raw_text:
        print("\nSilence gate: no speech detected -> no text produced (FR-4).")

    if args.expect_silence:
        if result.raw_text:
            print("EXPECT-SILENCE: FAIL — text was produced from non-speech audio.")
            return 1
        print("EXPECT-SILENCE: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
