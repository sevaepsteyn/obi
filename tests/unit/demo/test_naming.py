"""Unit tests for the fictional naming scheme (feature 134, T008)."""

from __future__ import annotations

from obi.core.demo.naming import (
    TREES,
    is_dictionary_word,
    node_label,
    person_label,
    place_label,
    place_words,
    rewrite_words,
    within_one_edit,
)

WORDS = frozenset({"orchard", "hotel", "shower", "mountain", "ranch", "relay"})


def test_place_words_drop_roles_digits_and_short_runs() -> None:
    assert place_words("northfield-gw-01") == ["northfield"]
    assert place_words("core-gw-1") == []
    assert place_words("eastmoor-internet-gw") == ["eastmoor"]
    assert place_words("Mountain Relay - East") == ["mountain"]
    assert place_words("Hollis's Farm") == ["hollis", "farm"]
    assert place_words("Meadows - Kiosk7") == ["meadows", "kiosk"]


def test_rewrite_words_keeps_roles_separators_and_case() -> None:
    mapping = {"northfield": "aspen", "southgate": "birch"}
    lookup = mapping.get
    assert rewrite_words("northfield-gw-01", lookup) == "aspen-gw-01"
    assert rewrite_words("NORTHFIELD_AP", lookup) == "ASPEN_AP"
    assert rewrite_words("Southgate Relay - East", lookup) == "Birch Relay - East"
    assert rewrite_words("northfield-southgate-bh", lookup) == "aspen-birch-bh"


def test_vendor_colliding_words_never_offered() -> None:
    assert not {"juniper", "cisco", "arista"} & set(TREES)
    assert len(set(TREES)) == len(TREES)


def test_place_labels_overflow_into_numbered_trees() -> None:
    assert place_label(0) == TREES[0]
    assert place_label(len(TREES)) == f"{TREES[0]}2"


def test_person_and_node_sequences() -> None:
    assert [person_label(i) for i in range(3)] == ["admin", "user-1", "user-2"]
    assert node_label(0) == "node-01"


def test_dictionary_classification_handles_plurals() -> None:
    assert is_dictionary_word("showers", WORDS)
    assert is_dictionary_word("Orchard", WORDS)
    assert not is_dictionary_word("northfield", WORDS)


def test_within_one_edit() -> None:
    assert within_one_edit("eastmoor", "eastmor")
    assert within_one_edit("eastmoor", "eastmoro")
    assert within_one_edit("eastmoor", "eastmoar")
    assert not within_one_edit("eastmoor", "westmoor1")
