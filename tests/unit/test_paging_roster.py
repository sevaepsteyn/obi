"""Unit (feature 102): the paging roster — CSV round-trip, search, STRICT resolution."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from obi.paging.roster import (
    ResolutionError,
    Resolved,
    RosterEntry,
    load_roster,
    parse_live,
    resolve,
    save_roster,
    search,
    split_tokens,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "paging"

ROSTER = [
    RosterEntry(type="group", id="201", name="Weather Watch"),
    RosterEntry(type="group", id="202", name="Night Ops"),
    RosterEntry(type="person", id="101", name="Sandpiper", fullname="Sandpiper (Alex Doe)"),
    RosterEntry(type="person", id="102", name="Dusty", fullname="Dusty (Sam Roe)"),
    RosterEntry(type="person", id="103", name="Towers On Call", fullname="floater"),
    RosterEntry(type="person", id="104", name="Dust Devil", fullname="Dust Devil (Kit Fog)"),
]


# --- CSV round-trip ----------------------------------------------------------------------------


def test_save_and_load_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "paging" / "roster.csv"  # parent dir is created
    save_roster(path, ROSTER)
    assert load_roster(path) == ROSTER


def test_load_missing_returns_none_not_empty(tmp_path: Path) -> None:
    assert load_roster(tmp_path / "nope.csv") is None


def test_save_keeps_previous_snapshot_as_bak(tmp_path: Path) -> None:
    path = tmp_path / "roster.csv"
    save_roster(path, ROSTER[:2])
    save_roster(path, ROSTER)
    assert load_roster(path) == ROSTER
    assert load_roster(tmp_path / "roster.csv.bak") == ROSTER[:2]


# --- search ------------------------------------------------------------------------------------


def test_search_substring_fullname_and_id() -> None:
    assert search(ROSTER, "weather") == [ROSTER[0]]
    assert search(ROSTER, "alex") == [ROSTER[2]]  # fullname matches too
    assert search(ROSTER, "102") == [ROSTER[3]]  # exact ID
    assert search(ROSTER, "dust") == [ROSTER[3], ROSTER[5]]


def test_search_type_filters() -> None:
    assert search(ROSTER, people_only=True) == ROSTER[2:]
    assert search(ROSTER, groups_only=True) == ROSTER[:2]


# --- resolution --------------------------------------------------------------------------------


def test_numeric_tokens_pass_through_without_roster() -> None:
    assert resolve(["101", "102"], roster=None) == Resolved(people=["101", "102"], groups=[])


def test_numeric_force_group_buckets_as_group() -> None:
    assert resolve(["201"], ROSTER, force_type="group") == Resolved(people=[], groups=["201"])


def test_unique_name_resolves_by_roster_type() -> None:
    resolved = resolve(["sandpiper", "weather"], ROSTER)
    assert resolved == Resolved(people=["101"], groups=["201"])


def test_ambiguous_name_refuses_listing_candidates() -> None:
    with pytest.raises(ResolutionError) as excinfo:
        resolve(["dust"], ROSTER)
    assert excinfo.value.kind == "ambiguous"
    assert [c.id for c in excinfo.value.candidates] == ["102", "104"]


def test_unknown_name_refuses() -> None:
    with pytest.raises(ResolutionError) as excinfo:
        resolve(["nobody"], ROSTER)
    assert excinfo.value.kind == "unknown"


def test_name_without_roster_refuses_with_guidance() -> None:
    with pytest.raises(ResolutionError) as excinfo:
        resolve(["sandpiper"], roster=None)
    assert excinfo.value.kind == "no-roster"
    assert "obi page refresh" in str(excinfo.value)


def test_force_type_narrows_the_pool() -> None:
    # "night" as a person: the group must not be considered — unknown, not a group hit.
    with pytest.raises(ResolutionError) as excinfo:
        resolve(["night"], ROSTER, force_type="person")
    assert excinfo.value.kind == "unknown"


def test_resolution_dedupes_preserving_order() -> None:
    resolved = resolve(["101", "sandpiper", "102"], ROSTER)
    assert resolved.people == ["101", "102"]


def test_split_tokens() -> None:
    assert split_tokens(" 101, sandpiper ,,102 ") == ["101", "sandpiper", "102"]


# --- refresh parsing (live capture shapes) -----------------------------------------------------


def test_parse_live_from_captured_shapes() -> None:
    people_body = json.loads((FIXTURES / "listpeople.json").read_text())
    groups_body = json.loads((FIXTURES / "listgroups.json").read_text())
    entries = parse_live(people_body, groups_body)
    assert entries[0] == RosterEntry(type="group", id="201", name="Weather Watch")
    assert RosterEntry(
        type="person", id="103", name="Towers On Call", fullname="floater"
    ) in entries
    assert len(entries) == 6


def test_parse_live_rejects_unexpected_shapes() -> None:
    with pytest.raises(ValueError, match="unexpected list response shape"):
        parse_live({"nope": []}, {"groups": []})
    with pytest.raises(ValueError, match="unexpected group entry"):
        parse_live({"people": []}, {"groups": [{"id": "1"}]})
