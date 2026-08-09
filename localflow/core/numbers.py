"""Spoken-number normalisation: "forty seven" -> "47".

The small slice of inverse text normalisation dictation needs, pure
text-in/text-out (core seam). Parakeet TDT spells numbers out ("twenty six
point two seven"); this rewrites them as digits. Applied identically to
live-preview decodes, segment texts and the final joined text (the app's
post_stt transform) so every path agrees on the same surface.

Deliberate limits (ponytail: each is a rare miss, extend if one bites):
- bare "one" is never converted ("no one", "one of them" would break);
- ordinals ("first"), "a hundred", and year pairs ("twenty twenty six")
  stay spelled;
- a scale word after a decimal is left alone ("one point five million"
  -> "1.5 million", not 1500000).
"""

from __future__ import annotations

import re

_UNITS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
          "six": 6, "seven": 7, "eight": 8, "nine": 9}
_TEENS = {"ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
          "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
          "eighteen": 18, "nineteen": 19}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
         "seventy": 70, "eighty": 80, "ninety": 90}
_SCALES = {"hundred": 100, "thousand": 1_000, "million": 10**6,
           "billion": 10**9}

_TOKEN = re.compile(r"^(\W*)([\w-]+?)(\W*)$", re.UNICODE)


def _words_of(token: str) -> tuple[str, list[str], str] | None:
    """(lead punct, lowercased words, trail punct) — hyphens split
    ("twenty-six" -> ["twenty", "six"]); None if the token has no core."""
    m = _TOKEN.match(token)
    if not m:
        return None
    lead, core, trail = m.groups()
    return lead, [w for w in core.lower().split("-") if w], trail


def _kind(word: str) -> str | None:
    if word in _UNITS:
        return "unit"
    if word in _TEENS:
        return "teen"
    if word in _TENS:
        return "ten"
    if word in _SCALES:
        return "scale"
    return None


class _Phrase:
    """Accumulates one spoken number word by word, refusing words that
    cannot extend it (the refusal is what keeps "twenty twenty" as two
    numbers and "the point is" untouched)."""

    def __init__(self):
        self.total = 0          # completed thousand/million groups
        self.current = 0        # the group being built
        self.last: str | None = None    # kind of the last accepted word
        self.frac: str | None = None    # digits after "point"
        self.n_words = 0
        self.only_one = False   # phrase is exactly the word "one"

    def feed(self, word: str, next_word: str | None) -> bool:
        """Try to extend with `word` (`next_word` peeks past connectives).
        False = the phrase ends BEFORE this word."""
        if self.frac is not None:
            if _kind(word) in ("unit", "teen", "ten"):
                # decimals read digit-wise: "point two seven" -> ".27"
                self.frac += str((_UNITS | _TEENS | _TENS)[word])
                self.n_words += 1
                self.only_one = False
                return True
            return False
        if word == "and":
            # connective only INSIDE a number: "hundred and five" — never
            # "two and three" (that's a conjunction, not 5)
            return (self.last == "scale" and next_word is not None
                    and _kind(next_word) in ("unit", "teen", "ten"))
        if word == "point":
            if self.n_words and next_word is not None \
                    and _kind(next_word) in ("unit", "teen", "ten"):
                self.frac = ""
                return True
            return False
        k = _kind(word)
        if k is None or self.last == "done":
            return False
        if k == "scale":
            v = _SCALES[word]
            if self.current < 1:
                return False            # "thousand" can't start a number
            if v == 100:
                if self.current >= 100:
                    return False        # no "hundred hundred"
                self.current *= 100
            else:
                self.total += self.current * v
                self.current = 0
            self.last = "scale"
        else:
            v = (_UNITS | _TEENS | _TENS)[word]
            extends = self.last == "ten" and k == "unit" and v > 0
            if self.last not in (None, "scale") and not extends:
                return False            # "twenty twenty" = two numbers
            self.current += v
            # zero never extends ("twenty zero" is not 20); it only stands
            # alone or opens a decimal ("zero point five")
            self.last = "done" if word == "zero" else k
        self.only_one = self.n_words == 0 and word == "one"
        self.n_words += 1
        return True

    def render(self) -> str:
        out = str(self.total + self.current)
        if self.frac is not None:
            out += "." + self.frac
        return out


def spelled_to_digits(text: str) -> str:
    """Rewrite spoken numbers in `text` as digits, leaving everything else
    (spacing, punctuation, casing of other words) byte-identical."""
    if not text:
        return text
    parts = re.split(r"(\s+)", text)
    out: list[str] = []
    i = 0
    while i < len(parts):
        token = parts[i]
        if not token or token.isspace():
            out.append(token)
            i += 1
            continue
        converted = _try_phrase(parts, i)
        if converted is None:
            out.append(token)
            i += 1
        else:
            rendered, i = converted
            out.append(rendered)
    return "".join(out)


def _peek_first_word(parts: list[str], i: int) -> str | None:
    while i < len(parts):
        if parts[i] and not parts[i].isspace():
            split = _words_of(parts[i])
            if split is None or split[0]:
                return None       # leading punct blocks joining the phrase
            return split[1][0] if split[1] else None
        i += 1
    return None


def _try_phrase(parts: list[str], start: int) -> tuple[str, int] | None:
    """Parse a number phrase starting at parts[start]. Returns (rendered
    text incl. the first token's leading and last token's trailing
    punctuation, index just past the phrase), or None."""
    phrase = _Phrase()
    lead = trail = ""
    i = start
    end = start                   # just past the last consumed token, so the
    consumed = 0                  # whitespace after the phrase is preserved
    while i < len(parts):
        token = parts[i]
        if not token or token.isspace():
            i += 1
            continue
        split = _words_of(token)
        if split is None:
            break
        t_lead, words, t_trail = split
        if not words:
            break                 # a bare "-" must stay on screen
        if consumed == 0:
            lead = t_lead
        elif t_lead:
            break                 # punctuation glued to the front ends it
        saved = (phrase.total, phrase.current, phrase.last, phrase.frac,
                 phrase.n_words, phrase.only_one)
        accepted = 0
        for j, w in enumerate(words):
            nxt = words[j + 1] if j + 1 < len(words) else \
                _peek_first_word(parts, i + 1)
            if not phrase.feed(w, nxt):
                break
            accepted += 1
        if accepted < len(words):
            # token only partly a number ("twenty-first"): leave it whole,
            # and undo the half-fed words so they aren't counted twice
            (phrase.total, phrase.current, phrase.last, phrase.frac,
             phrase.n_words, phrase.only_one) = saved
            break
        consumed += 1
        i += 1
        end = i
        if t_trail:
            trail = t_trail
            break                 # ". , ?" glued to the back ends the phrase
    if phrase.n_words == 0 or phrase.only_one:
        return None
    return lead + phrase.render() + trail, end
