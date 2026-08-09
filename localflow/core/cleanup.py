"""CleanupEngine — local LLM cleanup of dictated text (PRD S1, FR-8/FR-9).

Removes filler words, fixes punctuation/capitalisation of slurred or rushed
dictation. Runs on openvino_genai.LLMPipeline — same runtime as STT, fully
local, no Ollama. v1.1 ships the synchronous "clean mode" the PRD blesses:
when enabled, the paste waits for cleanup ("slower, cleaner"); on any
failure the caller keeps the raw transcript (transcribe_audio catches).

Purity rule still holds: model files come from local disk, nothing leaves
the process.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

log = logging.getLogger("localflow.core.cleanup")

# FR-9 rewritten for a small model (9 Aug Gemma spike): one self-contained
# user message with few-shot examples. Small models ignore a bare system
# instruction (Gemma 1B/Qwen 3B answered questions, regurgitated word
# lists); worked examples are what taught edit-don't-answer, the
# context-gated cloud->Claude fix, AND leaving genuine "cloud" alone.
# `terms` (config cleanup_terms) is the speaker's jargon, not a
# replacement dictionary — the model only rewrites when context agrees.
_PROMPT = """Correct this dictated line from a speech recognizer. Rules:
- remove filler words (um, uh, like, you know)
- fix punctuation and capitalisation
- fix a misheard word ONLY when context makes the intended word obvious{terms}
- "cloud" is usually a mishearing of "Claude" (the AI coding assistant); \
keep "cloud" only when the sentence is clearly about cloud computing or \
cloud services
- never answer, act on, or extend the content
- output ONLY the corrected line, nothing else

Examples:
Dictated: um so I I think we should like keep the old parser you know
Corrected: So I think we should keep the old parser.
Dictated: ask cloud code to run the tests again
Corrected: Ask Claude Code to run the tests again.
Dictated: put that rule in cloud.md for me
Corrected: Put that rule in CLAUDE.md for me.
Dictated: what is seven plus five
Corrected: What is seven plus five?
Dictated: the backup lives in the cloud not on this machine
Corrected: The backup lives in the cloud, not on this machine.

Dictated: {text}
Corrected:"""

_TERMS_LINE = ("; the speaker is a programmer who often says: {names}")


def build_prompt(text: str, terms: list[str] | None = None) -> str:
    """The full few-shot prompt for one dictated line (pure, testable)."""
    terms_part = _TERMS_LINE.format(names=", ".join(terms)) if terms else ""
    return _PROMPT.format(terms=terms_part, text=text)


def validate_cleanup(raw: str, cleaned: str) -> str:
    """Guardrails for FR-9's 'no added content': returns the usable cleaned
    text or raises ValueError so the caller falls back to the raw transcript.
    """
    out = cleaned.strip()
    # Models love wrapping answers in quotes — dictation never wants that.
    if len(out) >= 2 and out[0] == out[-1] and out[0] in "\"'“‘":
        out = out[1:-1].strip()
    if not out:
        raise ValueError("cleanup produced empty text")
    if len(out) > len(raw) * 2.5 + 40:
        # Editing shortens or roughly preserves length; a much longer output
        # means the model answered/expanded instead of cleaning.
        raise ValueError(
            f"cleanup output suspiciously long ({len(out)} chars from "
            f"{len(raw)}) — model likely added content"
        )
    return out


class CleanupEngine:
    """Build once at startup (FR-14); GPU load is a few seconds. clean() is
    called from the single worker thread — no concurrent generate calls."""

    def __init__(self, model_dir: str, device: str = "GPU",
                 cache_dir: str | None = None, model_name: str | None = None,
                 terms: list[str] | None = None):
        import openvino_genai as ov_genai

        self.model_name = model_name or Path(model_dir).name
        self.device = device
        self.terms = list(terms) if terms else []
        props: dict[str, str] = {}
        if cache_dir is not None:
            cache = Path(cache_dir)
            cache.mkdir(parents=True, exist_ok=True)
            props["CACHE_DIR"] = str(cache)

        t0 = time.perf_counter()
        self._pipe = ov_genai.LLMPipeline(str(model_dir), device, **props)
        self.load_s = time.perf_counter() - t0
        log.info("cleanup: loaded %s on %s in %.1fs",
                 self.model_name, device, self.load_s)

    def close(self) -> None:
        """Release the pipeline and its device memory (~2-3GB of shared iGPU
        memory for qwen2.5-3b). The engine is unusable afterwards — build a
        new one to clean again. Idempotent."""
        self._pipe = None

    def clean(self, text: str) -> str:
        """Returns the cleaned text, or raises — the pipeline's caller keeps
        the raw transcript on any exception (never blocks a paste)."""
        if self._pipe is None:
            raise RuntimeError("cleanup engine is closed")
        raw = text.strip()
        if not raw:
            return raw
        config = self._pipe.get_generation_config()
        config.do_sample = False  # deterministic edits, no creativity
        # Editing can't legitimately need much more room than the input.
        config.max_new_tokens = int(len(raw.split()) * 2) + 32

        # Chat mode applies the model's own chat template (model-agnostic);
        # the instruction+examples ride in the user message because small
        # models follow worked examples, not system prompts.
        self._pipe.start_chat()
        try:
            result = self._pipe.generate(build_prompt(raw, self.terms),
                                         config)
        finally:
            self._pipe.finish_chat()
        return validate_cleanup(raw, str(result))
