"""M12 (live typing): which words are safe to type yet, and how to patch what
was already typed once the final text is known.

Pure logic only - no model, no injection, no threads (the core seam, PRD §3).
The desktop LiveTyper feeds successive decodes of the same growing buffer in;
this decides what has stabilised. Keeping it pure is what makes the commit
rule testable without a mic or a GPU.
"""

from __future__ import annotations

import re

_WORD = re.compile(r"\S+")
_PUNCT = ".,!?;:\"'`()[]{}-…"


def _norm(word: str) -> str:
    """Compare words by their letters alone. Whisper re-punctuates and
    re-capitalises a word as the buffer around it grows ("hello" -> "Hello,"),
    so a strict match would almost never agree and nothing would ever commit.
    """
    return word.strip(_PUNCT).lower()


def commit_prefix(prev_words: list[str], new_words: list[str],
                  committed_len: int) -> int:
    """LocalAgreement-2: how many leading words of `new_words` are final.

    Two consecutive decodes of the same growing audio agreeing on a prefix is
    the evidence that the prefix has stabilised (whisper_streaming's rule; see
    docs/stt-streaming-research.md). The result never drops below
    `committed_len` - a committed word has already been typed, so a commit can
    only grow.
    """
    agreed = 0
    for a, b in zip(prev_words, new_words):
        if _norm(a) != _norm(b):
            break
        agreed += 1
    return max(agreed, committed_len)


def patch_plan(typed: str, final: str) -> tuple[int, str]:
    """(backspaces, text to retype) that turns `typed` into `final`.

    Word-aligned: keep the longest common WORD prefix (exact match - this is
    the final text, punctuation and all), delete the rest of `typed`, retype
    the rest of `final`. Aligning on words stops a late one-word revision from
    rewriting a whole paragraph.
    """
    if typed == final:
        return 0, ""
    t = [m.span() for m in _WORD.finditer(typed)]
    f = [m.span() for m in _WORD.finditer(final)]
    keep = 0
    for (a0, a1), (b0, b1) in zip(t, f):
        if typed[a0:a1] != final[b0:b1]:
            break
        keep += 1
    t_end = t[keep - 1][1] if keep else 0
    f_end = f[keep - 1][1] if keep else 0
    return len(typed) - t_end, final[f_end:]
