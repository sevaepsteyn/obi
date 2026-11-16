"""Unit (feature 095): the dashboard's board model — state ordering, rollup, JSON round-trip.

The load-bearing rule under test: ``stale`` outranks ``green`` (a data gap never reads "all
good") but never reaches ``yellow``/``red``. Plus the atomic state-file IO's totality: absent
and corrupt files are a state, never an exception.
"""

from __future__ import annotations

from pathlib import Path

from obi.core import dash
from obi.core.dash import Board, Item, Tile, board_from_json, worst

NOW_US = 1_787_000_000_000_000  # 2026-08-13T02:13:20Z


def test_state_ordering_is_green_stale_yellow_red() -> None:
    assert dash.STATES == ("green", "stale", "yellow", "red")


def test_worst_prefers_higher_rank() -> None:
    assert worst([]) == "green"
    assert worst(["green", "green"]) == "green"
    assert worst(["green", "stale"]) == "stale"
    assert worst(["stale", "yellow"]) == "yellow"
    assert worst(["yellow", "red", "green"]) == "red"
    assert worst(["bogus"]) == "green"  # unknown states never outrank known ones


def test_worst_stale_taints_green_but_never_beats_red() -> None:
    # The FR-001 rule in one line: green < stale < yellow < red.
    assert worst(["green", "stale"]) == "stale"
    assert worst(["red", "stale"]) == "red"


def _board() -> Board:
    tile = Tile(
        signal="fleet",
        state="red",
        items=[
            Item(
                key="tamarack-gw",
                label="tamarack-gw",
                state="red",
                detail="down 14m (172.18.89.1)",
                metrics={"rtt_ms": None, "age_s": 4.0},
                since_us=NOW_US - 840_000_000,
            )
        ],
        cadence_s=60,
        data_age_s=4.0,
    )
    return Board(generated_at_us=NOW_US, state="red", tiles=[tile], notes=["a note"])


def test_board_json_round_trip(tmp_path: Path) -> None:
    board = _board()
    dash.write_board(tmp_path, board)
    loaded = dash.load_board(tmp_path)
    assert loaded == board
    assert loaded is not None and loaded.generated_at.startswith("2026-")  # ISO derived from µs


def test_board_from_json_ignores_unknown_keys() -> None:
    payload = _board().to_json()
    payload["someday"] = {"new": "field"}
    payload["tiles"][0]["future"] = 1
    payload["tiles"][0]["items"][0]["shiny"] = True
    board = board_from_json(payload)
    assert board.tile("fleet") is not None
    assert board.tile("fleet").items[0].key == "tamarack-gw"  # type: ignore[union-attr]


def test_load_board_absent_and_corrupt_are_none(tmp_path: Path) -> None:
    assert dash.load_board(tmp_path) is None
    (tmp_path / dash.BOARD_FILE).write_text("{not json", encoding="utf-8")
    assert dash.load_board(tmp_path) is None


def test_carried_state_round_trip_and_absent(tmp_path: Path) -> None:
    assert dash.load_carried_state(tmp_path) == {}
    dash.write_carried_state(tmp_path, {"netwatch": {"gw|8.8.8.8": {"done_tests": 5}}})
    assert dash.load_carried_state(tmp_path)["netwatch"]["gw|8.8.8.8"]["done_tests"] == 5


def test_write_is_atomic_no_tmp_left_behind(tmp_path: Path) -> None:
    dash.write_board(tmp_path, _board())
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_fmt_duration_single_unit() -> None:
    assert dash._fmt_duration(42) == "42s"
    assert dash._fmt_duration(840) == "14m"
    assert dash._fmt_duration(3 * 3600) == "3h"
    assert dash._fmt_duration(2 * 86400) == "2d"


def test_maintenance_fields_round_trip_and_old_boards_read() -> None:
    # 135: optional fields — absent in pre-135 boards, preserved when present.
    from obi.core.maintenance import MaintenanceSummary

    summary = MaintenanceSummary(sites=["magnolia"], signals=["mesh"], reason="r", uisp_flagged=2)
    board = Board(
        generated_at_us=1,
        state="green",
        tiles=[
            Tile(
                signal="mesh",
                state="yellow",
                items=[Item(key="radio", label="mesh radio", state="yellow", maintenance="config")],
                cadence_s=300,
                maintenance="config",
            )
        ],
        maintenance=summary,
    )
    back = board_from_json(board.to_json())
    assert back == board
    old = board.to_json()
    old.pop("maintenance")
    for tile in old["tiles"]:
        tile.pop("maintenance")
        for item in tile["items"]:
            item.pop("maintenance")
    legacy = board_from_json(old)
    assert legacy.maintenance is None
    assert legacy.tiles[0].maintenance is None and legacy.tiles[0].items[0].maintenance is None
