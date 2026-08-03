"""Strip Whisper's trailing-silence hallucinations (FR-4 companion).

Whisper (turbo included) tends to emit a stock phrase — most often a bare
"you", or a YouTube sign-off like "Thanks for watching" — when it decodes a
stretch of near silence (the VAD trailing pad, or a hold with almost no real
speech). This module removes those artifacts:

- the WHOLE transcript when it is nothing but a stock phrase, and
- a trailing standalone "You." sentence appended after real speech.

Deliberately HIGH-PRECISION: it only ever removes a fragment that matches the
narrow blocklist, so genuine dictation is never touched. A legitimate ending
like "...send it to you." keeps its "you" (no sentence break before the word),
and a lowercase mid-sentence "thank you" ("I want to thank you.") survives.
What IS dropped: a detached trailing "You." or "Thank you." sentence (13 Jul:
the quiet-mic setup appended "Thank you." to almost every dictation, so the
old keep-thank-you stance flipped — a genuine detached closing "Thank you."
is the accepted loss), and a doubled FINAL word ("standardized standardized.",
"one Task. Task." — Whisper's end-of-utterance stutter).
"""

from __future__ import annotations

import re

# Normalized (lowercased, punctuation-stripped) phrases Whisper hallucinates on
# silence that a dictation user effectively never speaks as a whole utterance.
# Plain "thank you"/"thanks"/"bye" are DELIBERATELY excluded — those are things
# someone might genuinely dictate, and eating them would be worse than the
# occasional stray artifact.
_WHOLE_UTTERANCE = frozenset({
    "you",
    "thanks for watching",
    "thank you for watching",
    "thanks for watching everyone",
    "please subscribe",
    "subscribe to my channel",
    "like and subscribe",
    "thank you for watching this video",
})

# A trailing transcript CHUNK is a hallucination when it carries almost no
# text for the time it spans (Whisper decoding silence, e.g. "Thank you" over
# 30s -> 0.3 chars/sec), or when it begins after the real audio has already
# ended (its timestamp is invented). Real speech runs well above these floors.
_HALLUC_MIN_DURATION_S = 2.5   # only judge chunks at least this long
_HALLUC_MAX_CPS = 2.0          # chars/second below this over that span = silence
_HALLUC_START_MARGIN_S = 0.2   # a chunk starting this far past the audio is fake
_HALLUC_MAX_WORDS = 4          # a droppable tail is TINY ("you", "Thank you.")


def _droppable_text(stripped: str) -> bool:
    """Whether a trailing chunk's TEXT even looks like a silence artifact:
    a few words, or one phrase stuttered ("you! you! you!"). Timing tests
    only apply to these — a long, varied sentence is real dictation and is
    NEVER dropped, however broken its timestamps are (openvino sometimes
    reports the 30s decode-window end instead of the speech end, which made
    the cps test eat a whole real dictation; seen live 11 Jul)."""
    words = [w.strip(".,!?…").lower() for w in stripped.split()]
    words = [w for w in words if w]
    return len(words) <= _HALLUC_MAX_WORDS or len(set(words)) <= 2


def drop_trailing_hallucinated_chunks(
    chunks: list[tuple[str, float, float]], audio_duration_s: float
) -> str:
    """Rebuild the transcript from Whisper's timestamped chunks, dropping any
    TRAILING chunk whose timing marks it as a silence hallucination.

    `chunks` is (text, start_ts, end_ts) per chunk (openvino_genai's
    return_timestamps output). Walks from the end and drops chunks that begin
    past the real audio, or span a long time with too little text; stops at the
    first genuine chunk so mid-transcript content is never touched. Empty chunk
    list (or everything dropped) returns "".
    """
    kept = [(str(t), float(s), float(e)) for t, s, e in chunks]
    while kept:
        text, start_ts, end_ts = kept[-1]
        stripped = text.strip()
        dur = end_ts - start_ts
        cps = len(stripped) / dur if dur > 0 else 999.0
        started_past_audio = start_ts >= audio_duration_s - _HALLUC_START_MARGIN_S
        too_slow = dur >= _HALLUC_MIN_DURATION_S and cps < _HALLUC_MAX_CPS
        if not stripped or (_droppable_text(stripped)
                            and (started_past_audio or too_slow)):
            kept.pop()
        else:
            break
    return "".join(t for t, _s, _e in kept).strip()


# Whisper's OTHER decode-loop artifact: a word or short phrase stuck on
# repeat mid-transcript ("for... for... for...", "do anything do anything",
# seen live 11 Jul). Genuine dictation repeats a single word twice ("had
# had", "that that"), so a lone double is kept; three-plus of one word, or
# an immediate verbatim repeat of a phrase, is the model looping. The cap
# covers a whole doubled SENTENCE: live 30 Jul Whisper decoded 11- and
# 16-word sentences twice back to back and the old cap of 6 let both through.
_REPEAT_MAX_PHRASE_WORDS = 24
_REPEAT_MIN_WORD_RUN = 3
_REPEAT_STRIP = ".,!?;:…\"'"
# The loop is not always verbatim-adjacent: live 3 Aug a hypothesis repeated
# a 10-word phrase with one stray joiner between the copies ("...needs
# analysis THE excel so for..."). A long phrase repeated across a 1-2 token
# gap is still the model looping - no dictation says 4+ identical words
# twice with one word between. The joiner is loop junk and is dropped too.
_REPEAT_MIN_GAPPED_PHRASE = 4
_REPEAT_MAX_GAP = 2
# Fastest plausible dictation rate. When the caller supplies the speech
# duration, a big phrase run keeps as many copies as actually FIT the audio
# instead of collapsing to one - "I like it" said 3x in 2.9s decoded as x33
# (live 4 Aug): 33 copies cannot fit, 3 can.
_REPEAT_MAX_WPS = 3.5


def _min_period(phrase: list[str]) -> int:
    """Length of the smallest repeating unit ("i like it i like it" -> 3)."""
    for p in range(1, len(phrase)):
        if all(phrase[k] == phrase[k - p] for k in range(p, len(phrase))):
            return p
    return len(phrase)


def collapse_repeats(text: str, gapped: bool = False,
                     speech_s: float | None = None) -> str:
    """Collapse repetitions to ONE instance: a single word run of 3+
    ("you you you you"), a phrase up to a whole sentence repeated back to
    back ("do anything do anything"), and - only with `gapped=True` - a 4+
    word phrase repeated across a 1-2 token gap (the gap is dropped with
    the copy). Comparison ignores case and edge punctuation; the first
    occurrence's text is kept. A single doubled word is deliberately left
    alone - "had had" is real English, and the LLM cleanup already fixes
    genuine stumbles when it is on.

    `gapped` is OPT-IN because it can eat a deliberate near-repeat ("I
    want it done, and I want it done"). Only the live preview enables it -
    there a committed loop is permanent (append-only, no correction pass)
    and Jeremy ranked duplicates as the worse failure. The final text that
    feeds paste mode, history and "Copy last dictation" never uses it, so
    the genuine words always survive somewhere recoverable.

    `speech_s` (the speech duration) turns collapse-to-one into
    reconstruction for a BIG phrase run (4+ reps, certainly the model
    looping): keep as many copies as fit the audio at a fast speaking
    rate, words outside the run claiming time first. Text alone cannot
    tell "said it 3x" from "said it once, model looped" - the clock can."""
    tokens = text.split()
    if len(tokens) < 2:
        return text

    def norm(token: str) -> str:
        return token.strip(_REPEAT_STRIP).lower()

    out: list[str] = []
    i = 0
    n = len(tokens)
    while i < n:
        matched = False
        for length in range(_REPEAT_MAX_PHRASE_WORDS, 0, -1):
            if i + 2 * length > n:
                continue
            phrase = [norm(t) for t in tokens[i:i + length]]
            if not all(phrase):          # pure-punctuation token: no phrase
                continue
            if length > 1 and _min_period(phrase) < length:
                continue    # the phrase is itself periodic ("i like it i
                            # like it"): let the smallest unit's iteration
                            # collapse the run fully - matching the big
                            # block first left 11 of 33 copies (live 4 Aug)
            reps = 1
            while (i + (reps + 1) * length <= n
                   and [norm(t) for t in
                        tokens[i + reps * length:i + (reps + 1) * length]]
                   == phrase):
                reps += 1
            needed = _REPEAT_MIN_WORD_RUN if length == 1 else 2
            if reps >= needed:
                keep = 1
                if speech_s is not None and length > 1 and reps >= 4:
                    # Certainly a loop, but the true count is not 1 either:
                    # keep the copies that FIT the speech time.
                    other = n - reps * length
                    budget = int(speech_s * _REPEAT_MAX_WPS) - other
                    keep = max(1, min(reps, budget // length))
                out.extend(tokens[i:i + keep * length])
                i += reps * length
                matched = True
                break
        if not matched and gapped:
            # Near-adjacent repeat: a 4+ word phrase again after a tiny gap.
            for length in range(_REPEAT_MAX_PHRASE_WORDS,
                                _REPEAT_MIN_GAPPED_PHRASE - 1, -1):
                for gap in range(1, _REPEAT_MAX_GAP + 1):
                    j = i + length + gap
                    if j + length > n:
                        continue
                    phrase = [norm(t) for t in tokens[i:i + length]]
                    if not all(phrase) or _min_period(phrase) < length:
                        continue
                    if [norm(t) for t in tokens[j:j + length]] == phrase:
                        # Keep the SECOND copy - its last token carries the
                        # boundary punctuation; copy one + the gap are junk.
                        out.extend(tokens[j:j + length])
                        i = j + length
                        matched = True
                        break
                if matched:
                    break
        if not matched:
            out.append(tokens[i])
            i += 1
    if len(out) == len(tokens):          # nothing collapsed: exact original
        return text
    return " ".join(out)


# A trailing stock sentence Whisper appends to silence: "You." or "Thank you."
# after a sentence break ("code. You.", "gone. Thank you."). Matches "okay!
# You" but NOT "...send it to you." (no break before "you") or "you know what"
# (more words follow). Group 1 keeps the real sentence's punctuation.
_TRAILING_STOCK = re.compile(
    r"([.!?])\s*(?:thank\s+you|you)\b[\s.!?]*$", re.IGNORECASE)
# The same "Thank you." glued on with NO break at all ("the overview Thank
# you." — the capital T is Whisper starting a sentence the speaker never
# said). Case-sensitive on purpose: lowercase is real speech ("Like thank
# you."), and the \s+ means a bare whole-utterance "Thank you." is untouched.
_DETACHED_THANKS = re.compile(r"\s+Thank\s+you\b[\s.!?]*$")
# Whisper trails off into an ellipsis when speech fades mid-thought ("that is
# all or...", "for the......"). Never dictated speech — drop a trailing run.
_TRAILING_ELLIPSIS = re.compile(r"\s*(?:\.{2,}|…+)$")


def _drop_trailing_double(text: str) -> str:
    """Whisper's end-of-utterance stutter: the FINAL word doubled, often as a
    fake new sentence ("make it standardized standardized.", "show one Task.
    Task."). Collapse to one, keeping the last token — it carries the closing
    punctuation. Only the very end is touched; a doubled word mid-sentence
    ("had had a plan") is real English and stays."""
    tokens = text.split()
    if len(tokens) < 2:
        return text
    a = tokens[-2].strip(_REPEAT_STRIP).lower()
    b = tokens[-1].strip(_REPEAT_STRIP).lower()
    if a and a == b:
        del tokens[-2]
        return " ".join(tokens)
    return text


def _normalize(text: str) -> str:
    """Lowercase and strip surrounding punctuation/whitespace for a
    whole-utterance blocklist compare ("You." -> "you")."""
    lowered = text.strip().lower()
    lowered = re.sub(r"^[\s\W_]+|[\s\W_]+$", "", lowered)
    return re.sub(r"\s+", " ", lowered)


def strip_hallucinations(text: str) -> str:
    """Remove trailing-silence hallucinations from a raw STT transcript.

    Returns "" when the whole thing is a stock phrase (caller pastes nothing,
    same contract as the VAD silence gate); otherwise returns the text with any
    detached trailing "You."/"Thank you." sentence removed and a doubled final
    word collapsed. Idempotent.
    """
    if not text or not text.strip():
        return ""
    result = text.strip()
    # Peel detached trailing stock sentences (a hallucination can repeat).
    while True:
        peeled = _TRAILING_STOCK.sub(lambda m: m.group(1), result).strip()
        peeled = _DETACHED_THANKS.sub("", peeled).strip()
        if peeled == result:
            break
        result = peeled
    result = _TRAILING_ELLIPSIS.sub("", result).strip()
    result = _drop_trailing_double(result)
    norm = _normalize(result)
    if not norm or norm in _WHOLE_UTTERANCE:   # only-punctuation counts as empty
        return ""
    return result
