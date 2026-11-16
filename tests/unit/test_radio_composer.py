"""Unit (feature 109, US4): the expects-reply marker contract.

contracts/composer-contract.md: the model may end an answer with ``[expects-reply]``;
the glue parses and STRIPS it, case-insensitively, at the tail only. Anything else —
absent, misspelled, mid-text — parses as False, and the marker text never survives to
what the machine (and TTS) see. Advisory by construction: a wrong flag degrades to the
pre-109 clearing rules, never to a protocol fault.
"""

from __future__ import annotations

from obi.radio.runner import ComposedAnswer, split_expects_reply


def test_tail_marker_parses_and_strips() -> None:
    text, expects = split_expects_reply(
        "Need an animated effect for that — want one? over [expects-reply]"
    )
    assert expects is True
    assert text == "Need an animated effect for that — want one? over"


def test_tail_marker_is_case_insensitive_and_whitespace_tolerant() -> None:
    text, expects = split_expects_reply("say again the site name, over   [Expects-Reply]  ")
    assert expects is True
    assert "expects" not in text.lower()


def test_absent_marker_is_false() -> None:
    text, expects = split_expects_reply("Portal set to purple, over.")
    assert expects is False
    assert text == "Portal set to purple, over."


def test_mid_text_marker_is_false_but_still_scrubbed() -> None:
    text, expects = split_expects_reply("Link is up [expects-reply] and clean, over.")
    assert expects is False
    assert "expects-reply" not in text
    assert "Link is up" in text and "and clean, over." in text


def test_misspelled_marker_is_false_and_left_alone() -> None:
    text, expects = split_expects_reply("All good, over. [expect-reply]")
    assert expects is False


def test_composed_answer_shape() -> None:
    answer = ComposedAnswer(text="ok, over", expects_reply=True)
    assert answer.text == "ok, over" and answer.expects_reply is True
