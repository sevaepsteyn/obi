"""Unit (feature 133): the garble judgement — contract G1.

Pure function, corpus-bound: every recorded incident is garbled with its expected
signature; every real transmission in the replay corpus and the fixture's negative list
is not. See specs/133-radio-garble-reopen/contracts/garble-reopen.md.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from obi.radio.garble import (
    FOREIGN_ORTHOGRAPHY,
    NON_LATIN,
    REPETITION,
    garble_signature,
    is_repetition_hallucination,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
INCIDENTS = json.loads((FIXTURES / "radio_garble" / "incidents.json").read_text())


# -- repetition (the rule carried since 088, unit widened to 12 chars) -------------------


def test_repetition_rule_still_catches_the_088_shapes() -> None:
    assert garble_signature("adoado" * 40) == REPETITION
    assert garble_signature("BeepBeepBeepBeepBeepBeep") == REPETITION
    assert garble_signature("asc asc asc") == REPETITION
    assert garble_signature("...!!!...") == REPETITION  # symbol soup is never traffic
    assert garble_signature("ок" * 223) == REPETITION


def test_ten_char_unit_loop_is_repetition() -> None:
    # rx #2040: a 10-char squeezed unit slipped past the old 1–6 range.
    assert is_repetition_hallucination("остbenig er " * 6)
    row_2040 = "остbenig er остbenig er остbenig er остbenig er остbenig"
    assert garble_signature(row_2040) == REPETITION


def test_moved_function_is_still_importable_from_runner() -> None:
    from obi.radio.runner import is_repetition_hallucination as via_runner

    assert via_runner is is_repetition_hallucination


def test_plain_english_is_not_garbled() -> None:
    assert garble_signature("what's the uptime of the noc gateway") is None
    assert garble_signature("radio check") is None
    assert garble_signature("papa") is None
    assert garble_signature("") is None
    assert garble_signature("   ") is None


# -- non-Latin script -------------------------------------------------------------------


def test_three_non_latin_letters_are_garble_two_are_tolerated() -> None:
    assert garble_signature("Within Earth, Within Earth,струструструст") == NON_LATIN
    assert garble_signature("статус") == NON_LATIN  # a real Russian word is still not this channel
    assert garble_signature("roger, ок") is None  # two Cyrillic letters: a transcriber slip
    assert garble_signature("status is окей fine") == NON_LATIN


# -- foreign orthography ----------------------------------------------------------------


def test_icelandic_looking_row_is_foreign_orthography() -> None:
    # rx #6095, 2026-09-07 21:00:51 PT — accepted as intelligible at the time.
    assert garble_signature("Ó, píða, og fór í hana.") == FOREIGN_ORTHOGRAPHY
    assert garble_signature("Er það er það?") == FOREIGN_ORTHOGRAPHY  # two word occurrences


def test_one_accented_word_is_tolerated_two_words_or_three_letters_are_not() -> None:
    assert garble_signature("Meet at the café by the tower.") is None
    assert garble_signature("Zoë is at the gate.") is None
    assert garble_signature("Zoë and Renée are at the gate.") == FOREIGN_ORTHOGRAPHY
    assert garble_signature("élévé") == FOREIGN_ORTHOGRAPHY  # three accented letters, one word
    # The same word twice counts twice: occurrences, not distinct words.
    assert garble_signature("Café, café.") == FOREIGN_ORTHOGRAPHY


def test_decomposed_input_is_normalized_before_judging() -> None:
    decomposed = "Ó, píða, og fór í hana."
    assert garble_signature(decomposed) == FOREIGN_ORTHOGRAPHY


# -- corpus guard (G1) ------------------------------------------------------------------


@pytest.mark.parametrize("incident", INCIDENTS["incidents"], ids=lambda i: i["source"][:40])
def test_every_recorded_incident_is_garbled(incident: dict[str, str]) -> None:
    assert garble_signature(incident["text"]) == incident["expect"], incident["source"]


@pytest.mark.parametrize("text", INCIDENTS["intelligible"])
def test_fixture_negatives_stay_intelligible(text: str) -> None:
    assert garble_signature(text) is None


def _replay_corpus_intelligible_texts() -> list[str]:
    texts: list[str] = []
    for path in sorted((FIXTURES / "radio_replay").glob("*.json")):
        data = json.loads(path.read_text())
        for tx in data.get("transmissions", []):
            if tx.get("intelligible", True) and isinstance(tx.get("text"), str) and tx["text"]:
                texts.append(tx["text"])
    return texts


def test_replay_corpus_has_traffic_to_guard() -> None:
    # The guard below is only meaningful while the corpus carries real traffic.
    assert len(_replay_corpus_intelligible_texts()) >= 20


@pytest.mark.parametrize("text", _replay_corpus_intelligible_texts())
def test_replay_corpus_traffic_is_never_judged_garbled(text: str) -> None:
    assert garble_signature(text) is None, text
