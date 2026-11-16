"""Curated callsign fakes preserve every fuzzy-match decision (feature 137, T055). Synthetic."""

from __future__ import annotations

from obi.core.identity.callsign import _MATCH_THRESHOLD, check, ratio

STATION = ["obi", "obie", "obee", "o b", "opie", "hobi"]


def test_thresholds_are_the_radio_ones() -> None:
    assert _MATCH_THRESHOLD == 0.75


def test_same_shape_fakes_pass() -> None:
    # Two names one edit apart stay one edit apart; distant names stay distant.
    pairs = [("Marlow", "Harlow"), ("Marlowe", "Harlowe"), ("Zephyr", "Quokka")]
    assert check(pairs, STATION) == []


def test_a_fake_that_collapses_two_names_is_caught() -> None:
    pairs = [("Zephyr", "Kestrel"), ("Quokka", "Kestral")]  # real far apart, fakes near-twins
    rules = {v.rule for v in check(pairs)}
    assert "pair band changed" in rules


def test_a_fake_too_close_to_the_station_name_is_caught() -> None:
    pairs = [("Zephyr", "Obie")]
    assert any(v.rule == "station-name band changed" for v in check(pairs, STATION))


def test_fake_equal_to_real_is_caught() -> None:
    assert check([("Wren", "wren")])[0].rule == "fake equals real"


def test_ratio_squeezes_like_the_trigger() -> None:
    assert ratio("Hi-Fi", "hifi") == 1.0
