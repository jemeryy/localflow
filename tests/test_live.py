"""M12 streaming commit rules (core/live.py). Pure logic - no model, no mic:
the whole reason commit_prefix/patch_plan live in core is that the "is this
word safe to type yet" decision is testable without a GPU.
"""

from __future__ import annotations

from localflow.core.live import append_tail, commit_prefix, patch_plan


def _w(text: str) -> list[str]:
    return text.split()


# ---- append_tail (content-anchored tail) --------------------------------


def test_append_tail_simple_growth():
    tail, boundary = append_tail(_w("hello there"), _w("hello there my"), 2)
    assert tail == ["my"] and boundary == "there"


def test_append_tail_empty_screen_types_everything():
    assert append_tail([], _w("hello there"), 0) == (_w("hello there"), None)


def test_append_tail_survives_an_inserted_word():
    # The newer decode grew a word BEHIND the screen's edge: positional
    # slicing (newer[4:]) would re-type "retire" (live 30 Jul "retire
    # retire"); content alignment finds the real edge.
    tail, boundary = append_tail(
        _w("amounts needed to retire."),
        _w("amounts needed to a retire to be"), 4)
    assert tail == ["to", "be"] and boundary == "retire"


def test_append_tail_survives_multi_word_drift():
    tail, boundary = append_tail(
        _w("we are coming up with"),
        _w("we are now really coming up with lessons"), 5)
    assert tail == ["lessons"] and boundary == "with"


def test_append_tail_does_not_skip_words_when_the_edge_shifts_back():
    # The newer decode MERGED two words before the edge: positional slicing
    # (newer[5:]) would silently drop "on" - the mirror bug of re-typing.
    tail, boundary = append_tail(
        _w("I want to go on"),
        _w("I wanna go on with it"), 5)
    assert tail == ["with", "it"] and boundary == "on"


def test_append_tail_falls_back_to_position_when_the_edge_is_rewritten():
    # Append-only: the rewritten edge word stays as typed, and the old
    # positional behaviour decides the tail.
    tail, boundary = append_tail(
        _w("i rode a bike"), _w("i wrote a book fast"), 4)
    assert tail == ["fast"] and boundary == "book"


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
