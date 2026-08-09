"""core.numbers: spoken numbers -> digits (9 Aug, Parakeet spells them out)."""

import pytest

from localflow.core.numbers import spelled_to_digits


@pytest.mark.parametrize("spoken,expected", [
    # the live misses that motivated this (history 4-8 Aug)
    ("I think forty seven is too high.", "I think 47 is too high."),
    ("How do you get twenty six point two seven for it?",
     "How do you get 26.27 for it?"),
    ("Okay, forty into slice two.", "Okay, 40 into slice 2."),
    # compounds, scales, "and" inside a number
    ("twenty-six", "26"),
    ("one hundred and five", "105"),
    ("one hundred and twenty three thousand", "123000"),
    ("two thousand and twenty six", "2026"),
    ("nineteen hundred", "1900"),
    ("three million", "3000000"),
    ("zero", "0"),
    ("zero point five", "0.5"),
    ("one point five", "1.5"),
    # punctuation and casing survive around the number
    ("Forty seven?", "47?"),
    ("(seven, eight)", "(7, 8)"),
    # phrase boundaries: adjacent numbers stay separate
    ("five, six", "5, 6"),
    ("two and three make five", "2 and 3 make 5"),
])
def test_converts(spoken, expected):
    assert spelled_to_digits(spoken) == expected


@pytest.mark.parametrize("text", [
    # bare "one" is a pronoun too often to touch
    "no one likes it",
    "one of them",
    "The point is simple.",           # "point" outside a number
    "thousand thanks",                # a scale can't start a number
    "a hundred percent",              # "a hundred" stays spelled (documented)
    "in their twenties",              # plurals are not numbers
    "don't",
    "",
])
def test_leaves_alone(text):
    assert spelled_to_digits(text) == text


def test_scale_after_decimal_stays_a_word():
    # "1.5 million" reads better than 1500000 (documented limit)
    assert spelled_to_digits("one point five million") == "1.5 million"


def test_spacing_is_preserved_exactly():
    assert spelled_to_digits("say  forty seven \n now") == "say  47 \n now"
