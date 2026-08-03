"""Trailing-silence hallucination filter + language-token mapping (pure core).

The filter must kill Whisper's classic silence artifacts (a stray trailing
"you", YouTube sign-offs) WITHOUT ever touching genuine dictation — those two
properties are what the cases below pin down.
"""

from __future__ import annotations

from localflow.core import collapse_repeats, strip_hallucinations, to_language_token
from localflow.core.hallucination import drop_trailing_hallucinated_chunks


# -- whole-utterance hallucinations are gated to nothing --------------------

def test_bare_you_is_gated():
    assert strip_hallucinations("you") == ""
    assert strip_hallucinations("You.") == ""
    assert strip_hallucinations("  You  ") == ""


def test_youtube_signoffs_are_gated():
    assert strip_hallucinations("Thanks for watching!") == ""
    assert strip_hallucinations("thank you for watching") == ""
    assert strip_hallucinations("Please subscribe.") == ""
    assert strip_hallucinations("Subscribe to my channel") == ""


def test_empty_and_whitespace():
    assert strip_hallucinations("") == ""
    assert strip_hallucinations("   ") == ""
    assert strip_hallucinations(".") == ""


# -- a detached trailing "You." after real speech is peeled -----------------

def test_trailing_you_after_a_real_sentence_is_stripped():
    assert strip_hallucinations("Let me check the code. You.") == \
        "Let me check the code."
    assert strip_hallucinations("Okay! You") == "Okay!"


def test_repeated_trailing_you_is_fully_peeled():
    assert strip_hallucinations("Send me the file. You. You.") == \
        "Send me the file."


# -- genuine speech is never touched ----------------------------------------

def test_legitimate_you_is_kept():
    # "you" mid-clause (no sentence break before it) is real speech.
    assert strip_hallucinations("I'll send it to you.") == \
        "I'll send it to you."
    assert strip_hallucinations("How are you?") == "How are you?"
    assert strip_hallucinations("You know what I mean.") == \
        "You know what I mean."


def test_plain_thank_you_is_not_gated():
    # "thank you" alone is a plausible dictation, so it is deliberately kept.
    assert strip_hallucinations("Thank you.") == "Thank you."


# -- a detached trailing "Thank you." is peeled (live 13 Jul: appended to
# -- almost every dictation on the quiet-mic setup) --------------------------

def test_trailing_thank_you_sentence_is_peeled():
    assert strip_hallucinations(
        "the export should show and the step should be gone. Thank you."
    ) == "the export should show and the step should be gone."
    # Capitalized "Thank you" glued on with no sentence break before it.
    assert strip_hallucinations("give me the overview Thank you.") == \
        "give me the overview"
    assert strip_hallucinations(
        "split them into two worktrees? Thank you. Thank you."
    ) == "split them into two worktrees?"


def test_genuine_thank_you_is_kept():
    # Lowercase mid-sentence "thank you" is real speech.
    assert strip_hallucinations("I want to thank you.") == \
        "I want to thank you."
    assert strip_hallucinations("It adds extra words. Like thank you.") == \
        "It adds extra words. Like thank you."


# -- a doubled FINAL word collapses (live 13 Jul: "standardized
# -- standardized.", "one Task. Task.", "things things.") --------------------

def test_trailing_doubled_word_collapses():
    assert strip_hallucinations(
        "can you summarize and explain simply simply."
    ) == "can you summarize and explain simply."
    # The "full stop then the last word again as a new sentence" shape.
    assert strip_hallucinations("she only show one Task. Task.") == \
        "she only show one Task."
    # Combined with a trailing "Thank you." (peel first, then collapse).
    assert strip_hallucinations(
        "work on these two things things. Thank you. Thank you."
    ) == "work on these two things."


def test_mid_sentence_doubled_word_is_kept():
    # Only the very END is deduped - "had had" inside a sentence is English.
    text = "I had had enough of it by then."
    assert strip_hallucinations(text) == text


def test_normal_text_passes_through_unchanged():
    text = "The quick brown fox jumps over the lazy dog."
    assert strip_hallucinations(text) == text


def test_trailing_ellipsis_is_stripped():
    # Whisper trails off into dots when speech fades (live 30 Jul: "for
    # the......", "that is all or..."). Never dictated - drop the run.
    assert strip_hallucinations("i want the bounce for the......") == \
        "i want the bounce for the"
    assert strip_hallucinations("accurate that is all or...") == \
        "accurate that is all or"
    assert strip_hallucinations("wait…") == "wait"
    assert strip_hallucinations("...") == ""


def test_single_full_stop_is_kept():
    assert strip_hallucinations("Meeting at three.") == "Meeting at three."


def test_idempotent():
    once = strip_hallucinations("Meeting at three. You.")
    assert strip_hallucinations(once) == once == "Meeting at three."


# -- timestamped-chunk hallucination dropping -------------------------------

def test_chunk_filter_drops_slow_trailing_chunk_keeps_real_final_word():
    # Real chunks (from the actual model on the test WAV) + a bogus tail:
    # "Thank you" over ~30s (0.3 cps) starting past the 7.2s audio.
    chunks = [
        (" The quick brown fox jumps over the lazy dog.", 0.0, 3.36),
        (" Local Flow is a fully local dictation tool for", 3.52, 5.76),
        (" windows.", 6.0, 8.0),                 # real final word (kept)
        (" Thank you.", 8.04, 37.98),            # hallucination (dropped)
    ]
    out = drop_trailing_hallucinated_chunks(chunks, audio_duration_s=7.22)
    assert out.endswith("windows.")
    assert "Thank you" not in out


def test_chunk_filter_keeps_normal_speech():
    chunks = [
        (" Let me check the schedule", 0.0, 1.8),
        (" and get back to you tomorrow.", 1.8, 3.6),
    ]
    out = drop_trailing_hallucinated_chunks(chunks, audio_duration_s=4.1)
    assert out == "Let me check the schedule and get back to you tomorrow."


def test_chunk_filter_drops_chunk_starting_past_the_audio():
    chunks = [(" real speech here.", 0.0, 2.0),
              (" you", 3.5, 5.0)]               # starts past the 2.5s audio
    out = drop_trailing_hallucinated_chunks(chunks, audio_duration_s=2.5)
    assert out == "real speech here."


def test_chunk_filter_empty_and_all_dropped():
    assert drop_trailing_hallucinated_chunks([], 1.0) == ""
    assert drop_trailing_hallucinated_chunks(
        [(" you", 5.0, 40.0)], audio_duration_s=3.0) == ""


def test_chunk_filter_never_drops_a_long_real_sentence_on_bad_timestamps():
    # Seen live 11 Jul: openvino reported the 30s decode-window end for the
    # one chunk holding the whole dictation (12.15s of audio), the cps test
    # judged it "too slow" and the WHOLE transcript was eaten. Text with
    # many varied words is real speech regardless of its timestamps.
    text = (" sometimes when i record it still didn't show up as text"
            " nothing gets")
    out = drop_trailing_hallucinated_chunks([(text, 0.0, 30.0)],
                                            audio_duration_s=12.15)
    assert out == text.strip()


def test_chunk_filter_drops_a_stuttered_repetition_tail():
    # The repetition artifact ("you! you! you!...") can exceed the tiny-tail
    # word count but repeats one word - still droppable when its timing is
    # bogus (window-padded end over mostly-silent audio).
    chunks = [(" real words that vary considerably here.", 0.0, 3.0),
              (" you. you. you. you. you. you.", 3.4, 40.0)]
    out = drop_trailing_hallucinated_chunks(chunks, audio_duration_s=12.0)
    assert out == "real words that vary considerably here."


# -- decode-loop repetition collapse -----------------------------------------

def test_collapse_word_run_of_three_plus():
    # Seen live 11 Jul: "...button for for... for... for... for..."
    assert collapse_repeats(
        "what is the redact this page button for for... for... for... for..."
    ) == "what is the redact this page button for"
    assert collapse_repeats("you you you you. you. you.") == "you"


def test_collapse_doubled_phrase():
    # Seen live 11 Jul: "...before you do do anything do anything"
    assert collapse_repeats(
        "session handoff before you do anything do anything"
    ) == "session handoff before you do anything"
    assert collapse_repeats("Thank you. Thank you. Thank you.") == "Thank you."


def test_collapse_doubled_whole_sentence():
    # Live 30 Jul: Whisper decoded 11- and 16-word sentences twice back to
    # back; the old 6-word phrase cap let both through.
    assert collapse_repeats(
        "It's a very flat all the way at the FIH level. "
        "It's a very flat all the way at the FIH level."
    ) == "It's a very flat all the way at the FIH level."


def test_collapse_gapped_phrase_repeat():
    # Live 3 Aug: the preview hypothesis looped a 10-word phrase with one
    # stray joiner between the copies, so the adjacent rule missed it and
    # the loop reached the screen.
    assert collapse_repeats(
        "modeled out in excel so for example the fi calculators the needs "
        "analysis the excel so for example the fi calculators the needs "
        "analysis. What else"
    ) == ("modeled out in excel so for example the fi calculators the needs "
          "analysis. What else")


def test_collapse_gapped_needs_a_long_phrase():
    # Short phrases repeat with a word between in real English all the time.
    for text in ("I like this and I like that",
                 "we go up and we go down",
                 "the plan beats the plan we had"):
        assert collapse_repeats(text) == text


def test_collapse_keeps_genuine_english():
    # A single doubled word is real English - never collapsed.
    for text in ("I had had enough of that that day",
                 "it is very very important",
                 "The quick brown fox jumps over the lazy dog."):
        assert collapse_repeats(text) == text


def test_collapse_is_case_and_punctuation_insensitive():
    assert collapse_repeats("Stop! stop, STOP.") == "Stop!"


def test_collapsed_whole_utterance_run_is_then_gated():
    # collapse -> "you" -> the whole-utterance blocklist gates it to nothing,
    # matching the pipeline's collapse-then-strip order.
    assert strip_hallucinations(collapse_repeats(
        "you you you you. you. you. you.")) == ""


# -- language code -> Whisper token -----------------------------------------

def test_language_token_forces_a_language():
    assert to_language_token("en") == "<|en|>"
    assert to_language_token("FR") == "<|fr|>"
    assert to_language_token("zh") == "<|zh|>"


def test_language_token_auto_is_none():
    assert to_language_token("auto") is None
    assert to_language_token("") is None
    assert to_language_token(None) is None


def test_language_token_accepts_bracketed_form():
    assert to_language_token("<|de|>") == "<|de|>"
