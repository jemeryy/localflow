"""M12 (live typing): which words are safe to type yet, and how to patch what
was already typed once the final text is known.

Pure logic only - no model, no injection, no threads (the core seam, PRD §3).
The desktop LiveTyper feeds successive decodes of the same growing buffer in;
this decides what has stabilised. Keeping it pure is what makes the commit
rule testable without a mic or a GPU.
"""

from __future__ import annotations

import difflib
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
                  committed_len: int, edge_guard: int = 0) -> int:
    """LocalAgreement-2: how many leading words of `new_words` are final.

    Two consecutive decodes of the same growing audio agreeing on a prefix is
    the evidence that the prefix has stabilised (whisper_streaming's rule; see
    docs/stt-streaming-research.md). The result never drops below
    `committed_len` - a committed word has already been typed, so a commit can
    only grow.

    `edge_guard` > 0 relaxes the rule for AGED words (10 Aug): the model only
    wavers on the words nearest the live edge of the audio, so everything
    except the last `edge_guard` words of `new_words` commits after ONE
    decode. The newest `edge_guard` words still need two agreeing looks.
    0 keeps pure LocalAgreement-2.
    """
    agreed = 0
    for a, b in zip(prev_words, new_words):
        if _norm(a) != _norm(b):
            break
        agreed += 1
    aged = len(new_words) - edge_guard if edge_guard else 0
    return max(agreed, aged, committed_len)


def append_tail(screen: list[str], newer: list[str],
                fallback: int) -> tuple[list[str], str | None]:
    """Which words of `newer` still need typing after `screen`, by CONTENT.

    `screen` is what the current window already typed (final - append-only);
    `newer` is a later text for the same audio (an agreed hypothesis prefix,
    or the authoritative segment text). Positional slicing (newer[count:])
    breaks the moment the newer decode merges, splits or inserts a word
    behind the screen's edge - every later index shifts, and the slice
    re-types words already on screen ("retire retire", live 30 Jul) or
    silently skips real ones. Aligning normalised words finds where the
    screen's text actually ends inside `newer`; everything after that is
    the tail. Also returns the boundary - the newer surface of the screen's
    last word - for the punctuation rule.

    When the screen's edge cannot be found in `newer` (the decode rewrote
    that word; append-only means it stays as typed), `fallback` - the
    positional count - restores the old slice behaviour.
    """
    if not screen:
        return list(newer), None
    a = [_norm(w) for w in screen]
    b = [_norm(w) for w in newer]
    end = None
    for a0, b0, size in difflib.SequenceMatcher(
            a=a, b=b, autojunk=False).get_matching_blocks():
        if size and a0 + size == len(a):    # this block covers the edge
            end = b0 + size
    if end is None:
        end = min(fallback, len(newer))
    boundary = newer[end - 1] if end else None
    return list(newer[end:]), boundary


def patch_plan(typed: str, final: str, loose: bool = False) -> tuple[int, str]:
    """(backspaces, text to retype) that turns `typed` into `final`.

    Word-aligned: keep the longest common WORD prefix, delete the rest of
    `typed`, retype the rest of `final`. Aligning on words stops a late
    one-word revision from rewriting a whole paragraph.

    The prefix match ignores CASE. Whisper capitalises the same word
    differently depending on how much audio it saw ("I mean" from the preview,
    "i mean" from the final pass), and treating that as a divergence rewrote
    the whole line over one letter (live 27 Jul: 25 backspaces to change "I" to
    "i").

    `loose=True` (live revisions and the final patch over live-typed text)
    also ignores PUNCTUATION in the kept prefix — Whisper re-punctuates a word
    every time the buffer around it grows ("hello" -> "Hello,"), and honouring
    that would rewrite the whole line once a second. The kept prefix keeps the
    TYPED form; then the keep point walks back over any words whose typed form
    still differs (case aside), so trailing punctuation ("friend" ->
    "friend.") is fixed cheaply at the cursor while a stale mid-text comma is
    left alone. ponytail: mid-text punctuation-only fixes are skipped — fixing
    them via keystrokes means retyping everything after, the exact churn this
    mode exists to stop.
    """
    if typed == final:
        return 0, ""
    t = [m.span() for m in _WORD.finditer(typed)]
    f = [m.span() for m in _WORD.finditer(final)]
    keep = 0
    for (a0, a1), (b0, b1) in zip(t, f):
        a, b = typed[a0:a1], final[b0:b1]
        if (_norm(a) != _norm(b)) if loose else (a.casefold() != b.casefold()):
            break
        keep += 1
    if loose:
        # Fix trailing loose-matched words whose typed form is still wrong
        # ("friend" vs "friend.") — they sit at the cursor, so it's cheap.
        while keep and typed[slice(*t[keep - 1])].casefold() != \
                final[slice(*f[keep - 1])].casefold():
            keep -= 1
    t_end = t[keep - 1][1] if keep else 0
    f_end = f[keep - 1][1] if keep else 0
    return len(typed) - t_end, final[f_end:]
