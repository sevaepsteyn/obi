"""Unit (feature 127): the playa place lookup — loaders, resolution, rendering, fetch.

Fixture datasets in tmp paths; the city model is injected (dial-only, the mesh-test
shape); ``fetch_dataset`` runs against ``httpx.MockTransport``. No test touches the
network, and the resolver constants are the trigger.py/mesh.py mishearing policy.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from obi.core.brcaddr import CityModel
from obi.core.playa import (
    MAX_ANSWER_CHARS,
    PlayaLocator,
    fetch_dataset,
    load_places,
)
from tests.fixtures.brc.origin import ORIGIN

_DIAL_ONLY_MODEL = CityModel(
    origin=ORIGIN, az12_deg=45.0, orientation_source="plaza-fit", segments=(), anchors=()
)

# 6:15-ish, ~2,670 ft from the Man in the dial-only model (the mesh fixture point).
_ART_LAT, _ART_LON = 40.7788198, -114.8232908


def _write_art(tmp_path: Path, *entries: dict) -> Path:
    path = tmp_path / "art.json"
    path.write_text(json.dumps(list(entries)))
    return path


def _write_camps(tmp_path: Path, *entries: dict) -> Path:
    path = tmp_path / "camp.json"
    path.write_text(json.dumps(list(entries)))
    return path


def _camp(name: str, address: str | None = "G & 9:15", **extra: object) -> dict:
    entry: dict = {"name": name, "location": {"frontage": "G"}}
    if address is not None:
        entry["location_string"] = address
    entry.update(extra)
    return entry


def _art(name: str, lat: float | None = _ART_LAT, lon: float | None = _ART_LON) -> dict:
    entry: dict = {"name": name, "location": {}}
    if lat is not None:
        entry["location"] = {"gps_latitude": lat, "gps_longitude": lon}
    return entry


def _locator(tmp_path: Path, art: Path | None = None, camps: Path | None = None) -> PlayaLocator:
    return PlayaLocator(
        art_path=art or tmp_path / "absent-art.json",
        camp_path=camps or tmp_path / "absent-camp.json",
        model_loader=lambda: _DIAL_ONLY_MODEL,
    )


# --- loading -----------------------------------------------------------------------------


def test_load_places_merges_both_kinds_and_skips_junk(tmp_path: Path) -> None:
    art = _write_art(tmp_path, _art("Electric Dandelion"), {"noname": True}, "junk")
    camps = _write_camps(tmp_path, _camp("Snuggles", landmark="Glowing Tree Tree"))
    places = load_places(art, camps)
    assert [(p.kind, p.name) for p in places] == [
        ("art", "Electric Dandelion"),
        ("camp", "Snuggles"),
    ]
    assert places[1].landmark == "Glowing Tree Tree"
    assert places[0].latitude == _ART_LAT


def test_missing_or_corrupt_files_contribute_nothing(tmp_path: Path) -> None:
    bad = tmp_path / "art.json"
    bad.write_text("{ not json")
    assert load_places(bad, tmp_path / "absent.json") == ()


# --- resolution --------------------------------------------------------------------------


def test_exact_name_wins_case_and_punctuation_blind(tmp_path: Path) -> None:
    camps = _write_camps(tmp_path, _camp("Camp Soft Landing!"), _camp("Soft Serve"))
    locator = _locator(tmp_path, camps=camps)
    assert locator.locate("camp soft landing") == ("Camp Soft Landing! is at 9:15 & Golf.")


def test_unique_substring_resolves(tmp_path: Path) -> None:
    art = _write_art(tmp_path, _art("Electric Dandelion"), _art("Head Maze"))
    locator = _locator(tmp_path, art=art)
    assert locator.locate("dandelion").startswith("Electric Dandelion is at ")


def test_misheard_name_resolves_at_the_shared_threshold(tmp_path: Path) -> None:
    camps = _write_camps(tmp_path, _camp("Snuggles"), _camp("Distant Star"))
    locator = _locator(tmp_path, camps=camps)
    assert locator.locate("snugles") == "Snuggles is at 9:15 & Golf."


def test_near_tie_answers_every_candidates_location(tmp_path: Path) -> None:
    # Operator directive 2026-09-03: a tie lists each match's location outright —
    # never "which did you mean".
    art = _write_art(tmp_path, _art("Snuggle Field"))
    camps = _write_camps(tmp_path, _camp("Snuggle Fields"))
    locator = _locator(tmp_path, art=art, camps=camps)
    answer = locator.locate("snuggle fielб")
    assert "Snuggle Field (art) is at 6:15 and 2,670 feet from the Man" in answer
    assert "Snuggle Fields (camp) is at 9:15 & Golf." in answer
    assert "No confident match" not in answer


def test_duplicate_exact_names_answer_both_locations(tmp_path: Path) -> None:
    camps = _write_camps(
        tmp_path,
        _camp("Oasis", address="3:00 & C"),
        _camp("Oasis", address="9:00 & E"),
    )
    locator = _locator(tmp_path, camps=camps)
    assert locator.locate("oasis") == (
        "Oasis (camp) is at 3:00 & Charlie; Oasis (camp) is at 9:00 & Echo."
    )


def test_substring_tie_answers_locations_and_respects_the_budget(tmp_path: Path) -> None:
    camps = _write_camps(
        tmp_path,
        _camp("Arctica Outpost", address="3:00 & C"),
        _camp("Arctica Ice Sales", address="9:00 & E", landmark="x" * 700),
    )
    locator = _locator(tmp_path, camps=camps)
    answer = locator.locate("arctica")
    assert "Arctica Outpost (camp) is at 3:00 & Charlie" in answer
    assert "Arctica Ice Sales (camp) is at 9:00 & Echo" in answer
    assert "Look for" not in answer  # the combined form stays one breath
    assert len(answer) <= MAX_ANSWER_CHARS


def test_unknown_name_suggests_or_says_nothing(tmp_path: Path) -> None:
    camps = _write_camps(tmp_path, _camp("Snuggles"))
    locator = _locator(tmp_path, camps=camps)
    assert "Nothing named 'qzqzqzqz'" in locator.locate("qzqzqzqz")


# --- rendering ---------------------------------------------------------------------------


def test_camp_answer_carries_the_lookfor_line(tmp_path: Path) -> None:
    camps = _write_camps(tmp_path, _camp("Snuggles", landmark="Lit sign & glowing Tree Tree."))
    locator = _locator(tmp_path, camps=camps)
    assert locator.locate("snuggles") == (
        "Snuggles is at 9:15 & Golf.\n- Look for: Lit sign & glowing Tree Tree."
    )


def test_unplaced_camp_is_honest(tmp_path: Path) -> None:
    camps = _write_camps(tmp_path, _camp("Wanderers", address=None))
    locator = _locator(tmp_path, camps=camps)
    assert locator.locate("wanderers") == ("Wanderers has no published placement this year.")


def test_art_speaks_obis_address_for_its_gps(tmp_path: Path) -> None:
    art = _write_art(tmp_path, _art("Electric Dandelion"))
    locator = _locator(tmp_path, art=art)
    assert locator.locate("electric dandelion") == (
        "Electric Dandelion is at 6:15 and 2,670 feet from the Man."
    )


def test_gpsless_art_falls_back_to_the_published_string(tmp_path: Path) -> None:
    art = _write_art(
        tmp_path, {"name": "Mystery Piece", "location_string": "1:15 3900', Open Playa"}
    )
    locator = _locator(tmp_path, art=art)
    assert locator.locate("mystery piece") == ("Mystery Piece is at 1:15 3900', Open Playa.")


def test_budget_trims_the_lookfor_line_first(tmp_path: Path) -> None:
    camps = _write_camps(tmp_path, _camp("Snuggles", landmark="x" * 700))
    locator = _locator(tmp_path, camps=camps)
    answer = locator.locate("snuggles")
    assert answer == "Snuggles is at 9:15 & Golf."
    assert len(answer) <= MAX_ANSWER_CHARS


def test_no_data_answer_points_at_the_fetch_command(tmp_path: Path) -> None:
    locator = _locator(tmp_path)
    answer = locator.locate("anything")
    assert "No playa data downloaded yet" in answer and "obi playa fetch" in answer


def test_empty_name_asks_for_one(tmp_path: Path) -> None:
    locator = _locator(tmp_path)
    assert locator.locate("  ") == "Which camp or art piece? Give me a name."


# --- fetch -------------------------------------------------------------------------------


def test_fetch_dataset_hits_the_official_endpoints(tmp_path: Path) -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["key"] = request.headers.get("x-api-key", "")
        seen["url"] = str(request.url)
        return httpx.Response(200, json=[{"name": "A"}])

    out = tmp_path / "camp.json"
    count = fetch_dataset(
        "camp",
        year=2026,
        key="sekrit",
        out_path=out,
        base_url="https://api.example.net",
        transport=httpx.MockTransport(handler),
    )
    assert count == 1
    assert seen["key"] == "sekrit"
    assert seen["url"].startswith("https://api.example.net/")
    assert seen["url"].split("?")[0].endswith("/api/camp") and "year=2026" in seen["url"]
    assert json.loads(out.read_text()) == [{"name": "A"}]


def test_fetch_failure_leaves_the_existing_file_untouched(tmp_path: Path) -> None:
    out = tmp_path / "art.json"
    out.write_text('[{"name": "Kept"}]')

    def reject(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    with pytest.raises(httpx.HTTPStatusError):
        fetch_dataset(
            "art",
            year=2026,
            key="bad",
            out_path=out,
            base_url="https://api.example.net",
            transport=httpx.MockTransport(reject),
        )
    assert json.loads(out.read_text())[0]["name"] == "Kept"

    def not_a_list(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"nope": 1})

    with pytest.raises(ValueError, match="expected a list"):
        fetch_dataset(
            "art",
            year=2026,
            key="k",
            out_path=out,
            base_url="https://api.example.net",
            transport=httpx.MockTransport(not_a_list),
        )
    assert json.loads(out.read_text())[0]["name"] == "Kept"


def test_places_pick_up_a_refetch_without_restart(tmp_path: Path) -> None:
    import os as _os

    camps = _write_camps(tmp_path, _camp("First"))
    _os.utime(camps, (1.0, 100.0))
    art_absent = tmp_path / "absent.json"
    assert load_places(art_absent, camps)[0].name == "First"
    _write_camps(tmp_path, _camp("Second"))
    _os.utime(camps, (1.0, 200.0))
    assert load_places(art_absent, camps)[0].name == "Second"


# --- 127 data-driven refinements ---------------------------------------------------------


def test_the_temple_means_the_temple_of_convention(tmp_path: Path) -> None:
    # "Temple of …" is the honored build's naming convention (2026: Temple of the Moon);
    # camps and satellite pieces with "temple" in the name are never what this ask means.
    art = _write_art(tmp_path, _art("Temple of the Moon"), _art("Temple Braziers"))
    camps = _write_camps(tmp_path, _camp("Water Temple"), _camp("Temple Camp"))
    locator = _locator(tmp_path, art=art, camps=camps)
    assert locator.locate("the temple").startswith("Temple of the Moon is at ")
    assert locator.locate("Temple").startswith("Temple of the Moon is at ")


def test_far_art_prefers_the_published_string(tmp_path: Path) -> None:
    # ~8,000 ft out (the airport pieces) the published string carries the context obi's
    # clock arithmetic loses.
    far_lat = _ART_LAT - 0.022  # ≈ 8,000 ft further south
    art = _write_art(
        tmp_path,
        {
            "name": "Electric Dandelion",
            "location": {"gps_latitude": far_lat, "gps_longitude": _ART_LON},
            "location_string": "4:45 8000', Airport",
        },
    )
    locator = _locator(tmp_path, art=art)
    assert locator.locate("electric dandelion") == ("Electric Dandelion is at 4:45 8000', Airport.")


def test_placeholder_coordinates_render_as_no_location(tmp_path: Path) -> None:
    art = _write_art(
        tmp_path,
        {
            "name": "Spec Placeholder",
            "location": {"gps_latitude": 0.0, "gps_longitude": 0.0},
        },
    )
    locator = _locator(tmp_path, art=art)
    assert locator.locate("spec placeholder") == (
        "Spec Placeholder has no published location this year."
    )


def test_published_letters_speak_nato_phonetic_names(tmp_path: Path) -> None:
    # Operator decision 2026-09-03: ring letters are spoken NATO-phonetically.
    camps = _write_camps(
        tmp_path,
        _camp("Ice Nine", address="9:00 G Plaza @ 4:30"),
        _camp("Edge Camp", address="K & 10:00"),
    )
    locator = _locator(tmp_path, camps=camps)
    assert locator.locate("ice nine") == "Ice Nine is at 9:00 Golf Plaza @ 4:30."
    assert locator.locate("edge camp") == "Edge Camp is at 10:00 & Kilo."
