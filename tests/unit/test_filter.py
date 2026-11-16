"""T016: the vendor-neutral device-name filter (feature 003, FR-016).

Also covers ``name_excludes`` — the exclude companion added by feature 035 (FR-001/FR-002).
"""

from __future__ import annotations

from obi.core.filter import name_excludes, name_matches


def test_unset_filter_matches_everything() -> None:
    assert name_matches("anything", None)
    assert name_matches(None, None)
    assert name_matches("anything", "")


def test_case_insensitive_substring_match() -> None:
    assert name_matches("ptp-ridge-east", "ptp")
    assert name_matches("PTP-Ridge-East", "ptp")
    assert name_matches("uplink-PtP-7", "ptp")
    assert not name_matches("core-sw-01", "ptp")


def test_device_with_no_name_excluded_when_filtering() -> None:
    assert not name_matches(None, "ptp")
    assert not name_matches("", "ptp")


def test_name_excludes_no_patterns_excludes_nothing() -> None:
    assert not name_excludes("aspen-gw", [])
    assert not name_excludes("aspen-gw", ())


def test_name_excludes_case_insensitive_substring() -> None:
    assert name_excludes("aspen-gw", ["aspen"])
    assert name_excludes("SF - ASPEN", ["aspen"])  # site name, uppercase
    assert name_excludes("aspen-gw ↔ aspen-ptp-60", ["aspen"])  # link label
    assert not name_excludes("chestnut-gw", ["aspen"])


def test_name_excludes_matches_any_pattern() -> None:
    assert name_excludes("chestnut-gw", ["aspen", "chestnut"])
    assert not name_excludes("core-gw", ["aspen", "chestnut"])


def test_name_excludes_none_or_empty_name_never_excluded() -> None:
    assert not name_excludes(None, ["aspen"])
    assert not name_excludes("", ["aspen"])


def test_name_excludes_ignores_empty_pattern_strings() -> None:
    # An empty pattern must not match everything (guards against a stray "" in the list).
    assert not name_excludes("chestnut-gw", [""])
    assert name_excludes("chestnut-gw", ["", "chestnut"])
