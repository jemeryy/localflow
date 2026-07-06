"""Personal dictionary (M10): deterministic post-STT text replacement.

Pure text-in/text-out - no I/O, no config access - so it lives in the core
seam. This is the SAFE way to fix jargon Whisper mishears: unlike hot_words
(initial_prompt biasing degraded general accuracy live on 3 Jul), a
replacement applied after transcription can never change what the model
hears; worst case it rewrites one phrase wrongly and the rule gets deleted.

Also home to the correction miner: a dumb, transparent word-level diff
counter over (original, corrected) pairs from the history panel. No ML -
a substitution seen enough times becomes a SUGGESTED rule, never an
auto-applied one.
"""

from __future__ import annotations

import re
from collections import Counter
from difflib import SequenceMatcher

_WORD_RE = re.compile(r"[\w']+", re.UNICODE)


def _adapt_case(replacement: str, matched: str) -> str:
    """Give an all-lowercase replacement the matched text's sentence casing.
    A replacement that carries its own capitals ("Codex", "GPU") is used
    verbatim - that casing is the point of the rule."""
    if replacement != replacement.lower():
        return replacement
    if matched.isupper() and len(matched) > 1:
        return replacement.upper()
    if matched[:1].isupper():
        return replacement[:1].upper() + replacement[1:]
    return replacement


def apply_replacements(text: str, replacements: dict[str, str]) -> str:
    """Apply the dictionary to a transcript: case-insensitive, whole-word,
    multi-word capable, longest source first (so "code x api" wins over
    "code x" when both are rules). Deterministic and idempotent-ish by
    construction: replacements happen in one pass per rule, longest first,
    and later (shorter) rules see the already-rewritten text."""
    if not text or not replacements:
        return text
    for source in sorted(replacements, key=len, reverse=True):
        target = replacements[source]
        if not source.strip():
            continue
        # \b anchors break next to punctuation like "`"; lookarounds on
        # word characters handle multi-word sources and edge punctuation.
        pattern = re.compile(
            r"(?<!\w)" + re.escape(source.strip()) + r"(?!\w)",
            re.IGNORECASE)
        text = pattern.sub(lambda m, t=target: _adapt_case(t, m.group(0)),
                           text)
    return text


def mine_substitutions(pairs: list[tuple[str, str]],
                       min_count: int = 2,
                       max_words: int = 3) -> list[tuple[str, str, int]]:
    """Count word-level substitutions across (original, corrected) pairs.

    A substitution is a replace-opcode of at most ``max_words`` words on
    each side from a SequenceMatcher over the word sequences. Sources are
    counted case-insensitively; the target keeps the casing of its most
    recent occurrence. Returns [(source, target, count)] with
    count >= min_count, most frequent first. Dumb and transparent on
    purpose - the UI shows these as suggestions, never auto-applies."""
    counts: Counter[tuple[str, str]] = Counter()
    latest_target: dict[tuple[str, str], str] = {}
    for original, corrected in pairs:
        if not original or not corrected or original == corrected:
            continue
        a = _WORD_RE.findall(original)
        b = _WORD_RE.findall(corrected)
        matcher = SequenceMatcher(a=[w.lower() for w in a],
                                  b=[w.lower() for w in b], autojunk=False)
        for op, a0, a1, b0, b1 in matcher.get_opcodes():
            if op != "replace":
                continue
            if not (1 <= a1 - a0 <= max_words and 1 <= b1 - b0 <= max_words):
                continue
            source = " ".join(w.lower() for w in a[a0:a1])
            target = " ".join(b[b0:b1])
            if source == target.lower():
                continue                     # case-only edits are not rules
            key = (source, target.lower())
            counts[key] += 1
            latest_target[key] = target
    out = [(source, latest_target[(source, tgt)], n)
           for (source, tgt), n in counts.items() if n >= min_count]
    out.sort(key=lambda item: (-item[2], item[0]))
    return out
