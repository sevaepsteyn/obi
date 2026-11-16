"""Unit (feature 118): the ``dash_status`` chat handler.

The promises under test: the summary leads with the overall state and the board's age,
lists non-green items worst-first with detail and time-in-state, and stays bounded (item
cap with an honest "…and N more", worst case ≤ 1,600 chars); a green board is a one-line
all-clear; a board past the 240 s staleness idiom is disclosed as STALE/last-known; a
missing board answers "not running" (naming the state dir, the cron, and the config
reason when there is one) — never a raised error, never invented state; the ``signal``
drill-down lists a whole tile including green items; and open PAGED incidents from the
pager state render with their age (seeded/cleared entries never do).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from obi.core import dash as dash_core
from obi.core.audit import AuditTrail
from obi.core.config import DashSettings
from obi.core.dash import Board, Item, Tile
from obi.core.toolset import ToolContext, _dash_status, build_tools
from tests.fakes import chat_config


def _now_us() -> int:
    # Relative to REAL now at call time — a module-import stamp would drift in a full-suite
    # run and break the age/staleness assertions (the test_toolset_query_health lesson).
    return int(datetime.now(UTC).timestamp() * 1_000_000)


def _item(label: str, state: str, detail: str = "", *, since_ago_s: float | None = 720) -> Item:
    since = _now_us() - int(since_ago_s * 1_000_000) if since_ago_s is not None else None
    return Item(key=label, label=label, state=state, detail=detail, since_us=since)


def _green_tiles() -> list[Tile]:
    return [
        Tile(
            signal=signal,
            state="green",
            items=[_item(f"{signal}-a", "green", "up"), _item(f"{signal}-b", "green", "up")],
            cadence_s=60,
        )
        for signal in dash_core.SIGNALS
    ]


def _write_board(
    state_dir: Path,
    tiles: list[Tile],
    *,
    age_s: float = 40.0,
    notes: list[str] | None = None,
) -> None:
    board = Board(
        generated_at_us=_now_us() - int(age_s * 1_000_000),
        state=dash_core.worst([t.state for t in tiles]),
        tiles=tiles,
        notes=notes or [],
    )
    dash_core.write_board(state_dir, board)


def _write_pager(state_dir: Path, items: dict[str, dict[str, Any]]) -> None:
    dash_core.write_json_atomic(state_dir / dash_core.PAGER_STATE_FILE, {"items": items})


def _ctx(tmp_path: Path, *, dash: DashSettings | None) -> ToolContext:
    config = chat_config(tmp_path)
    config.dash = dash
    return ToolContext(
        config=config,
        driver=None,
        audit=AuditTrail(config.audit_path),
        question="what is the current status?",
    )


def _settings(tmp_path: Path, *, enabled: bool = True) -> DashSettings:
    return DashSettings(enabled=enabled, state_dir=tmp_path / "dash")


# --- US1: summary + drill-down ----------------------------------------------------------------


def test_green_board_is_a_one_line_all_clear_with_age(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _write_board(settings.state_dir, _green_tiles(), age_s=40)
    out = _dash_status(_ctx(tmp_path, dash=settings), {})
    assert "all green" in out
    assert "7 signals" in out and "14 items" in out  # 121 added mesh + lighting
    assert "40s old" in out
    assert "\n" not in out  # one line — no item roll on a green board
    assert "STALE" not in out


def test_mixed_board_lists_non_green_worst_first_with_detail_and_duration(
    tmp_path: Path,
) -> None:
    tiles = _green_tiles()
    tiles[0] = Tile(  # fleet: one red
        signal="fleet",
        state="red",
        items=[_item("noc-ptp-manzanita-60", "red", "down · timeout", since_ago_s=720)],
        cadence_s=60,
    )
    tiles[3] = Tile(  # internet: one yellow
        signal="internet",
        state="yellow",
        items=[_item("alder uplink", "yellow", "in 2.1 Mb/s (low)", since_ago_s=180)],
        cadence_s=60,
    )
    settings = _settings(tmp_path)
    _write_board(settings.state_dir, tiles)
    out = _dash_status(_ctx(tmp_path, dash=settings), {})
    assert out.startswith("Dashboard: RED")
    assert "fleet red" in out and "internet yellow" in out and "netwatch green" in out
    assert "down · timeout" in out and "red for 12m" in out
    assert "in 2.1 Mb/s (low)" in out and "yellow for 3m" in out
    assert out.index("noc-ptp-manzanita-60") < out.index("alder uplink")  # red before yellow


def test_item_without_since_carries_no_duration(tmp_path: Path) -> None:
    tiles = _green_tiles()
    tiles[0] = Tile(
        signal="fleet",
        state="red",
        items=[_item("fresh-gw", "red", "down", since_ago_s=None)],
        cadence_s=60,
    )
    settings = _settings(tmp_path)
    _write_board(settings.state_dir, tiles)
    out = _dash_status(_ctx(tmp_path, dash=settings), {})
    assert "fresh-gw" in out and "— red" in out
    assert "red for" not in out


def test_item_cap_is_honest_and_worst_case_fits_the_radio_budget(tmp_path: Path) -> None:
    """SC-004: the mass-outage payload stays inside the compact convention."""

    items = [_item(f"down-gw-{i:02d}", "red", "down · timeout") for i in range(77)]
    tiles = _green_tiles()
    tiles[0] = Tile(signal="fleet", state="red", items=items, cadence_s=60)
    settings = _settings(tmp_path)
    _write_board(settings.state_dir, tiles)
    out = _dash_status(_ctx(tmp_path, dash=settings), {})
    assert out.count("down-gw-") == 8  # the cap
    assert "69 more not shown" in out
    assert "signal=" in out  # points at the drill-down
    assert len(out) <= 1600


def test_notes_render_verbatim(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _write_board(settings.state_dir, _green_tiles(), notes=["netwatch source unreachable"])
    out = _dash_status(_ctx(tmp_path, dash=settings), {})
    assert "netwatch source unreachable" in out


def test_signal_drilldown_lists_green_items_and_tile_age(tmp_path: Path) -> None:
    tiles = _green_tiles()
    tiles[4] = Tile(
        signal="pipeline",
        state="green",
        items=[_item("logs-sync", "green", "last run 3m ago")],
        cadence_s=300,
        data_age_s=170.0,
    )
    settings = _settings(tmp_path)
    _write_board(settings.state_dir, tiles)
    out = _dash_status(_ctx(tmp_path, dash=settings), {"signal": "pipeline"})
    assert out.startswith("Signal pipeline: green")
    assert "logs-sync" in out and "last run 3m ago" in out  # green items DO list here
    assert "tile data 3m old" in out


def test_unknown_signal_names_the_valid_ones(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _write_board(settings.state_dir, _green_tiles())
    out = _dash_status(_ctx(tmp_path, dash=settings), {"signal": "bogus"})
    assert "'bogus'" in out
    for signal in dash_core.SIGNALS:
        assert signal in out


def test_dash_status_is_a_registered_read_only_tool() -> None:
    tools = {t.name: t for t in build_tools()}
    tool = tools["dash_status"]
    assert tool.risk_class == "read_only" and tool.mutating is False
    assert "FIRST tool" in tool.description  # the steering claim rides every surface
    assert tool.input_schema["required"] == []


# --- US2: staleness and absence are answers ---------------------------------------------------


def test_stale_board_is_disclosed_as_last_known(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _write_board(settings.state_dir, _green_tiles(), age_s=1200)
    out = _dash_status(_ctx(tmp_path, dash=settings), {})
    assert out.startswith("STALE")
    assert "20m ago" in out
    assert "LAST-KNOWN" in out
    assert "all green" in out  # content still shown, framed as last-known


def test_board_within_threshold_carries_no_stale_caveat(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _write_board(settings.state_dir, _green_tiles(), age_s=239)
    out = _dash_status(_ctx(tmp_path, dash=settings), {})
    assert "STALE" not in out and "4m old" in out


def test_missing_board_answers_not_running_with_dir_and_cron(tmp_path: Path) -> None:
    settings = _settings(tmp_path)  # state dir never written
    out = _dash_status(_ctx(tmp_path, dash=settings), {})
    assert "not running" in out
    assert str(settings.state_dir) in out
    assert "obi dash run" in out
    assert "[dash]" not in out  # configured and enabled — no config blame


def test_missing_board_names_the_config_gap(tmp_path: Path) -> None:
    out = _dash_status(_ctx(tmp_path, dash=None), {})
    assert "no [dash] section" in out

    disabled = _settings(tmp_path, enabled=False)
    out = _dash_status(_ctx(tmp_path, dash=disabled), {})
    assert "[dash] enabled = false" in out


def test_unreadable_board_behaves_as_absent(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.state_dir.mkdir(parents=True)
    (settings.state_dir / dash_core.BOARD_FILE).write_text("not json{", encoding="utf-8")
    out = _dash_status(_ctx(tmp_path, dash=settings), {})
    assert "not running" in out  # never a traceback, never invented state


# --- US3: open paged incidents ----------------------------------------------------------------


def _red_entry(label: str, *, ago_s: float = 900, **extra: Any) -> dict[str, Any]:
    now = _now_us()
    return {
        "signal": "fleet",
        "label": label,
        "detail": "down · timeout",
        "red_since_us": now - int(ago_s * 1_000_000),
        "observed_us": now,
        "paged_at_us": now - int(ago_s * 1_000_000),
        "seeded": False,
        **extra,
    }


def test_open_paged_item_renders_with_age(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _write_board(settings.state_dir, _green_tiles())
    _write_pager(settings.state_dir, {"fleet|sad-gw": _red_entry("sad-gw", ago_s=900)})
    out = _dash_status(_ctx(tmp_path, dash=settings), {})
    assert "Paged incidents open:" in out
    assert "sad-gw — paged, open 15m" in out


def test_link_incident_members_render_as_one_line(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _write_board(settings.state_dir, _green_tiles())
    opened = _now_us() - 720 * 1_000_000
    _write_pager(
        settings.state_dir,
        {
            "fleet|noc-ptp-manzanita-60": _red_entry(
                "noc-ptp-manzanita-60", incident_opened_us=opened
            ),
            "netwatch|manzanita-gw→noc": _red_entry("manzanita-gw→noc", incident_opened_us=opened),
        },
    )
    out = _dash_status(_ctx(tmp_path, dash=settings), {})
    assert out.count("paged, open") == 1  # ONE incident line, not two
    assert "manzanita-gw→noc + noc-ptp-manzanita-60" in out and "open 12m" in out


def test_seeded_cleared_and_absent_pager_entries_stay_silent(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _write_board(settings.state_dir, _green_tiles())

    no_file = _dash_status(_ctx(tmp_path, dash=settings), {})
    assert "Paged" not in no_file

    _write_pager(
        settings.state_dir,
        {
            # Seeded at first run, never paged — no observed transition, nothing to name.
            "fleet|old-red": {**_red_entry("old-red"), "paged_at_us": None, "seeded": True},
            # Paged once, since recovered — recovery already went out; not open.
            "fleet|healed": _red_entry("healed", cleared_at_us=_now_us()),
        },
    )
    out = _dash_status(_ctx(tmp_path, dash=settings), {})
    assert "Paged" not in out and "old-red" not in out and "healed" not in out


def test_incident_lines_are_capped_honestly(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _write_board(settings.state_dir, _green_tiles())
    _write_pager(
        settings.state_dir,
        {f"fleet|gw-{i}": _red_entry(f"gw-{i}", ago_s=600 + i) for i in range(6)},
    )
    out = _dash_status(_ctx(tmp_path, dash=settings), {})
    assert out.count("paged, open") == 4  # the cap
    assert "2 more open incidents" in out


# --- 135: maintenance is excluded from the verdict and summarised in one line ------------------

from dataclasses import replace as _replace  # noqa: E402

from obi.core.maintenance import MaintenanceSummary  # noqa: E402


def _maint_item(label: str, detail: str = "maintenance · down 33d") -> Item:
    return _replace(_item(label, "yellow", detail), maintenance="config: teardown")


def _maint_tiles() -> list[Tile]:
    tiles = _green_tiles()
    extra = [_maint_item("magnolia-gw"), _maint_item("manzanita-gw")]
    tiles[0] = _replace(tiles[0], items=tiles[0].items + extra)
    return tiles


def test_only_maintenance_non_green_is_still_all_green(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    board = Board(
        generated_at_us=_now_us() - 30_000_000,
        state="green",
        tiles=_maint_tiles(),
        maintenance=MaintenanceSummary(
            sites=["magnolia", "manzanita"],
            signals=["mesh"],
            reason="post-burn teardown",
            uisp_flagged=21,
        ),
    )
    dash_core.write_board(settings.state_dir, board)
    out = _dash_status(_ctx(tmp_path, dash=settings), {})
    first, *rest = out.splitlines()
    assert "all green" in first
    assert rest[0].startswith(
        "In maintenance: sites magnolia, manzanita; signals mesh; 21 UISP-flagged"
    )
    assert "post-burn teardown" in rest[0] and "2 board items" in rest[0]
    assert "magnolia-gw" not in out  # not listed as a problem


def test_mixed_board_lists_only_real_problems(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    tiles = _maint_tiles()
    tiles[0] = _replace(
        tiles[0],
        state="red",
        items=tiles[0].items + [_item("rowan-ptp-joshua-st-05", "red", "down 33d")],
    )
    _write_board(settings.state_dir, tiles)
    out = _dash_status(_ctx(tmp_path, dash=settings), {})
    assert "RED" in out.splitlines()[0]
    assert "rowan-ptp-joshua-st-05" in out
    assert "- [fleet] magnolia-gw" not in out
    assert "In maintenance: 2 board items" in out


def test_signal_view_marks_maintenance_items(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _write_board(settings.state_dir, _maint_tiles())
    out = _dash_status(_ctx(tmp_path, dash=settings), {"signal": "fleet"})
    assert "magnolia-gw: maintenance · down 33d — in maintenance" in out


def test_whole_signal_maintenance_shows_in_signals_line(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    tiles = _green_tiles()
    mesh = next(i for i, t in enumerate(tiles) if t.signal == "mesh")
    tiles[mesh] = _replace(
        tiles[mesh],
        state="yellow",
        maintenance="config",
        items=[
            _replace(
                _item("mesh radio", "yellow", "maintenance · no device"), maintenance="config"
            )
        ],
    )
    fleet = tiles[0]
    tiles[0] = _replace(fleet, state="red", items=fleet.items + [_item("x-gw", "red", "down 3m")])
    _write_board(settings.state_dir, tiles)
    out = _dash_status(_ctx(tmp_path, dash=settings), {})
    assert "mesh maintenance" in out
