"""CleanupEngine — optional local LLM cleanup (PRD S1). Scheduled for M5 (v1.1).

The class exists so the §4 contract is complete and `transcribe_audio()` can
already accept a cleanup stage; constructing one raises until M5 lands.
"""

from __future__ import annotations


class CleanupEngine:
    def __init__(self, model_dir: str, device: str = "GPU"):
        raise NotImplementedError(
            "CleanupEngine is M5 (v1.1) and is not built yet — PRD S1 / FR-8."
        )

    def clean(self, text: str) -> str:
        raise NotImplementedError
