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


def test_case_only_difference_does_not_rewrite_the_line():
    """Live 27 Jul: the final pass said "i mean" where the preview typed
    "I mean" and the whole line got backspaced over one letter."""
    back, retype = patch_plan("I mean like since we have",
                              "i mean like since we have Hello?")
    assert back == 0
    assert retype == " Hello?"


def test_punctuation_still_counts_as_a_difference():
    back, retype = patch_plan("hello there", "hello there.")
    assert back == len(" there")
    assert retype == " there."


# ---- loose mode (live revisions + the final patch over live-typed text) --


def test_loose_ignores_punctuation_and_case_drift():
    """Whisper re-punctuates typed words every decode ("hello" -> "Hello,");
    honouring that would rewrite the line once a second."""
    assert patch_plan("hello there my", "Hello, there my", loose=True) == (0, "")


def test_loose_still_fixes_the_trailing_words_cheaply():
    """Trailing form fixes sit at the cursor, so they ARE applied."""
    back, retype = patch_plan("hello there my friend",
                              "Hello, there my friend.", loose=True)
    assert back == len(" friend")
    assert retype == " friend."


def test_loose_patches_real_word_changes_from_where_they_start():
    back, retype = patch_plan("i rode a bike", "I wrote a book", loose=True)
    assert back == len(" rode a bike")
    assert retype == " wrote a book"


def test_loose_appends_new_words_without_touching_the_rest():
    back, retype = patch_plan("I mean like since we have",
                              "i mean like since we have Hello?", loose=True)
    assert back == 0
    assert retype == " Hello?"
