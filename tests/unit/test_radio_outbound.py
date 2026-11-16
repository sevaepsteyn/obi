"""Unit tests for the outbound go-ahead detector (feature 124, contract O8 + shapes)."""

from __future__ import annotations

import pytest

from obi.radio.outbound import (
    GoAheadDecision,
    detect_go_ahead,
    is_decline,
    is_standby,
    with_over_sign_off,
)

VARIANTS = ["obi", "obie", "obee", "o b", "opie"]


def go(text: str, callee: str = "Dana", aliases: dict[str, str] | None = None) -> GoAheadDecision:
    return detect_go_ahead(text, callee=callee, name_variants=VARIANTS, aliases=aliases)


# -- accepted shapes ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "shape"),
    [
        ("Go.", "bare_go"),
        ("go", "bare_go"),
        ("Go, over", "bare_go"),
        ("Go ahead", "go_ahead"),
        ("go ahead, what do you have", "go_ahead"),
        ("Go for Dana", "go_for_callee"),
        ("go for dana, over", "go_for_callee"),
        ("Go for obi", "go_for_obi"),
        ("go for obie", "go_for_obi"),
        ("Dana, go for obi", "callee_go_for_obi"),
        ("dana go for obi over", "callee_go_for_obi"),
        ("Dana, go ahead", "callee_go_for_obi"),
        ("Dana, go", "callee_go_for_obi"),
        ("Obi, go for Dana", "obi_go_for_callee"),
        ("obi go for dana", "obi_go_for_callee"),
    ],
)
def test_accepted_shapes(text: str, shape: str) -> None:
    decision = go(text)
    assert decision.matched, text
    assert decision.shape == shape


def test_filler_token_tolerated() -> None:
    assert go("uh, go for Dana").matched
    assert go("yeah go ahead").matched


def test_fuzzy_callee_match() -> None:
    # Whisper near-miss on the callee name clears the trigger's own 0.75 bar.
    assert go("go for Dena").matched
    assert go("go for Danna").matched


def test_caller_alias_reaches_callee() -> None:
    # Curated mis-hearings apply in name position, the trigger rule.
    decision = go("go for seven", callee="Mira", aliases={"seven": "Mira"})
    assert decision.matched
    assert decision.shape == "go_for_callee"


def test_multiword_callee() -> None:
    assert go("go for net control", callee="Net Control").matched


# -- rejections --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Go for Mike",  # names a different station — never ours (FR-007)
        "go for it",  # the English idiom; 'it' is neither callee nor obi
        "we'll go for the generator swap at six",  # not opening the transmission
        "I say go for Dana",  # 'say' is no filler — a sentence, not a go-ahead
        "going ahead with the swap",  # 'going' is not 'go'
        "copy that",
        "",
        "over",  # stripping the trailing 'over' must not leave a phantom match
    ],
)
def test_rejections(text: str) -> None:
    assert not go(text).matched, text


def test_empty_callee_matches_nothing() -> None:
    assert not detect_go_ahead("go for dana", callee="  ", name_variants=VARIANTS).matched


def test_go_for_callee_requires_the_right_callee() -> None:
    assert not go("go for dana", callee="Mike").matched


# -- standby / decline -------------------------------------------------------------------


def test_standby_shapes() -> None:
    assert is_standby("standby")
    assert is_standby("Stand by one, obi")
    assert is_standby("uh, wait one")
    assert not is_standby("I can't stand by that")
    assert not is_standby("go for dana")


def test_decline_shapes() -> None:
    assert is_decline("Negative, busy right now")
    assert is_decline("negatory")
    assert is_decline("no copy on that")
    assert not is_decline("that's a negative copy of the config")


# -- delivery framing --------------------------------------------------------------------


def test_with_over_sign_off_appends() -> None:
    assert with_over_sign_off("the link is back up") == "the link is back up, over"


@pytest.mark.parametrize(
    "text",
    [
        "the link is back up, over",
        "the link is back up, over.",
        "the link is back up, out",
        "the link is back up, over and out",
    ],
)
def test_with_over_sign_off_idempotent(text: str) -> None:
    assert with_over_sign_off(text) == text


def test_with_over_sign_off_empty() -> None:
    assert with_over_sign_off("") == ""


def test_hail_back_is_a_go_ahead() -> None:
    # The callee answering our hail by hailing back: permission by protocol.
    assert go("Obi, obi, Dana").shape == "hail_back"
    assert go("obi dana").shape == "hail_back"
    assert go("obi obi dana go ahead").matched  # hail-back carrying the go


def test_bare_callee_name_alone_is_not_a_go_ahead() -> None:
    # Third parties say names too — the obi-led prefix is what makes it a response.
    assert not go("Dana").matched
    assert not go("dana dana").matched
