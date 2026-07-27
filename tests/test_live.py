"""M12 streaming commit rules (core/live.py). Pure logic - no model, no mic:
the whole reason commit_prefix/patch_plan live in core is that the "is this
word safe to type yet" decision is testable without a GPU.
"""

from __future__ import annotations

from localflow.core.live import commit_prefix, patch_plan


def _w(text: str) -> list[str]:
    return text.split()


# ---- commit_prefix (LocalAgreement-2) -----------------------------------


def test_first_decode_commits_nothing():
    # nothing to agree with yet - one decode is never evidence
    assert commit_prefix([], _w("hello there my"), 0) == 0


def test_agreeing_prefix_commits():
    assert commit_prefix(_w("hello there"), _w("hello there my friend"), 0) == 2


def test_divergence_stops_the_commit_there():
    # the second decode revised "wrote" -> "rode": only "I" is safe
    assert commit_prefix(_w("I wrote a"), _w("I rode a bike"), 0) == 1


def test_never_retreats_below_what_is_committed():
    # a shorter/disagreeing decode can't unsay words already typed (M13)
    assert commit_prefix(_w("one two three"), _w("one"), 3) == 3
    assert commit_prefix(_w("one two"), _w("totally different"), 2) == 2


def test_agreement_ignores_punctuation_and_case():
    # Whisper re-punctuates as the buffer grows; a strict match would never
    # agree and nothing would ever commit.
    assert commit_prefix(_w("hello there"), _w("Hello, there!"), 0) == 2


# ---- patch_plan (M13's final patch) -------------------------------------


def test_identical_text_needs_no_patch():
    assert patch_plan("hello there", "hello there") == (0, "")


def test_appended_words_are_pure_retype():
    back, retype = patch_plan("hello there", "hello there my friend")
    assert back == 0
    assert retype == " my friend"


def test_changed_suffix_backspaces_only_the_divergent_words():
    back, retype = patch_plan("I rode a bike", "I wrote a book")
    assert back == len(" rode a bike")         # "I" survives; the space is retyped
    assert retype == " wrote a book"


def test_full_rewrite_backspaces_everything():
    assert patch_plan("garbage", "hello") == (len("garbage"), "hello")


def test_trailing_space_is_trimmed():
    assert patch_plan("hello there ", "hello there") == (1, "")
