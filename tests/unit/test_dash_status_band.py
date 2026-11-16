"""Unit (feature 128): the status-band color derivation — pageable signals only.

The tower blinking red must mean "someone would be paged about this": the derivation
mirrors the pager's signal set, never the raw board state (a display-only mesh/lighting
red must NOT redden the tower — the D1 trap in research.md).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from obi.core.config import _DASH_PAGEABLE_SIGNALS, load_config
from obi.core.dash import (
    PAGEABLE_SIGNALS,
    Board,
    Item,
    LightingController,
    LightingHealth,
    Tile,
    desired_status,
    load_board,
    reconcile_status_band,
    run_cycle,
)
from obi.core.lighting import STATUS_LOOKS

NOW_US = 1_787_000_000_000_000


def _tile(signal: str, state: str, items: list[Item] | None = None) -> Tile:
    return Tile(signal=signal, state=state, items=items or [], cadence_s=60)


def _board(tiles: list[Tile]) -> Board:
    return Board(generated_at_us=0, state="green", tiles=tiles)


def _green_pageable() -> list[Tile]:
    return [
        _tile(signal, "green", [Item(key="k", label="k", state="green")])
        for signal in PAGEABLE_SIGNALS
    ]


def test_pageable_tuple_mirrors_config() -> None:
    # config inlines the tuple to avoid a config→dash import cycle; keep them identical.
    assert PAGEABLE_SIGNALS == _DASH_PAGEABLE_SIGNALS


def test_all_clear_is_green() -> None:
    assert desired_status(_board(_green_pageable())) == "green"


def test_pageable_red_item_is_red() -> None:
    tiles = _green_pageable()
    tiles[0] = _tile(
        "fleet", "red", [Item(key="gw", label="core-gw", state="red", detail="down 3m")]
    )
    assert desired_status(_board(tiles)) == "red"


def test_display_only_red_never_reddens_the_tower() -> None:
    # The D1 trap: mesh/lighting can go red but nobody is paged — the band stays green.
    tiles = _green_pageable() + [
        _tile("mesh", "red", [Item(key="rak", label="station radio", state="red")]),
        _tile("lighting", "red", [Item(key="portal", label="portal", state="red")]),
    ]
    assert desired_status(_board(tiles)) == "green"


def test_pageable_yellow_is_yellow() -> None:
    tiles = _green_pageable()
    tiles[1] = _tile(
        "netwatch", "yellow", [Item(key="pair", label="pair", state="yellow")]
    )
    assert desired_status(_board(tiles)) == "yellow"


def test_pageable_stale_is_yellow() -> None:
    # Degraded observability is a degraded state — you cannot read "all good" off missing data.
    tiles = _green_pageable()
    tiles[2] = _tile("ping", "stale", [])
    assert desired_status(_board(tiles)) == "yellow"


def test_red_outranks_yellow() -> None:
    tiles = _green_pageable()
    tiles[0] = _tile("fleet", "red", [Item(key="gw", label="gw", state="red")])
    tiles[1] = _tile("netwatch", "yellow", [Item(key="p", label="p", state="yellow")])
    assert desired_status(_board(tiles)) == "red"


def test_maintenance_yellow_item_never_reds() -> None:
    # A maintenance-flagged down item is yellow-ized at eval time (fleet already, netwatch
    # via T015) and excluded from the red set — the band shows at most yellow for it.
    tiles = _green_pageable()
    tiles[0] = _tile(
        "fleet",
        "green",  # 118: maintenance-yellows ride a green tile (excluded from rollup)
        [Item(key="wave", label="tamarack-ptp", state="yellow", detail="maintenance · down 2h")],
    )
    assert desired_status(_board(tiles)) == "green"


def test_no_pageable_tiles_is_none() -> None:
    assert desired_status(_board([])) is None
    assert desired_status(_board([_tile("mesh", "green")])) is None


# --- the reconcile step (edge-triggered, drift-healing, fail-quiet) ---------------------------


def _load(tmp_path: Path, *, enabled: bool = True) -> Any:
    path = tmp_path / "config.toml"
    path.write_text(
        f'[logs]\ndb_path = "{tmp_path}/logs.db"\n'
        f'[dash]\nenabled = true\nstate_dir = "{tmp_path}/dash"\n'
        f"[dash.status_band]\nenabled = {str(enabled).lower()}\n"
        '[wled.devices]\nportal = "https://portal.test"\n'
        "[wled.segments.portal]\nstatus = [0, 2, 4]\nmain = [1, 3, 5]\n"
    )
    path.chmod(0o600)
    return load_config(path)


def _fixture_raw() -> dict[str, Any]:
    fixtures = Path(__file__).parent.parent / "fixtures" / "wled"
    return json.loads((fixtures / "state_6seg.json").read_text())


def _matching_raw(look: str) -> dict[str, Any]:
    raw = _fixture_raw()
    vocab = STATUS_LOOKS[look]
    for seg in raw["seg"]:
        if seg["id"] in (0, 2, 4):
            if look == "off":
                seg["on"] = False
                continue
            seg["col"] = [list(c) for c in vocab["col"]] + seg["col"][len(vocab["col"]) :]
            seg["fx"] = 1 if look == "red" else 0
    return raw


class _FakeEngine:
    def __init__(self, *, boom: bool = False) -> None:
        self.calls: list[tuple[str, str]] = []
        self.boom = boom

    def set_status_band(self, device: str, *, look: str, reason: str, actor: str) -> str:
        if self.boom:
            raise RuntimeError("engine exploded")
        self.calls.append((device, look))
        return f"{device} status bands are now {look}."


def _health(raw: dict[str, Any] | None) -> LightingHealth:
    return LightingHealth(
        controllers=[LightingController("portal", raw is not None, raw=raw)]
    )


def test_reconcile_writes_on_drift(tmp_path: Path) -> None:
    config = _load(tmp_path)
    engine = _FakeEngine()
    # Fixture bands are pure green — NOT vocabulary emerald ⇒ drift ⇒ one write.
    reconcile_status_band(config, _board(_green_pageable()), _health(_fixture_raw()),
                          engine=engine)
    assert engine.calls == [("portal", "green")]


def test_reconcile_steady_state_writes_nothing(tmp_path: Path) -> None:
    config = _load(tmp_path)
    engine = _FakeEngine()
    reconcile_status_band(config, _board(_green_pageable()), _health(_matching_raw("green")),
                          engine=engine)
    assert engine.calls == []


def test_reconcile_red_incident(tmp_path: Path) -> None:
    config = _load(tmp_path)
    engine = _FakeEngine()
    tiles = _green_pageable()
    tiles[0] = _tile("fleet", "red", [Item(key="gw", label="gw", state="red")])
    reconcile_status_band(config, _board(tiles), _health(_matching_raw("green")), engine=engine)
    assert engine.calls == [("portal", "red")]


def test_reconcile_disabled_never_writes(tmp_path: Path) -> None:
    config = _load(tmp_path, enabled=False)
    engine = _FakeEngine()
    reconcile_status_band(config, _board(_green_pageable()), _health(_fixture_raw()),
                          engine=engine)
    assert engine.calls == []


def test_reconcile_unreadable_device_never_writes(tmp_path: Path) -> None:
    config = _load(tmp_path)
    engine = _FakeEngine()
    reconcile_status_band(config, _board(_green_pageable()), _health(None), engine=engine)
    reconcile_status_band(config, _board(_green_pageable()), None, engine=engine)
    assert engine.calls == []


def test_reconcile_missing_status_segments_never_writes(tmp_path: Path) -> None:
    config = _load(tmp_path)
    engine = _FakeEngine()
    raw = _fixture_raw()
    raw["seg"] = [seg for seg in raw["seg"] if seg["id"] not in (0, 2, 4)]
    reconcile_status_band(config, _board(_green_pageable()), _health(raw), engine=engine)
    assert engine.calls == []


def test_reconcile_unevaluable_board_never_writes(tmp_path: Path) -> None:
    config = _load(tmp_path)
    engine = _FakeEngine()
    reconcile_status_band(config, _board([]), _health(_fixture_raw()), engine=engine)
    assert engine.calls == []


def test_cycle_contains_a_reconcile_explosion(tmp_path: Path, monkeypatch: Any) -> None:
    # FR-008/SC-005: a lighting failure never costs the board write or the cycle.
    from obi.core.drivers import wled as wled_driver_mod

    config = _load(tmp_path)
    fixture = _fixture_raw()

    def fake_state(self: Any, base_url: str) -> Any:
        from obi.core.drivers.wled import WledResult, _parse_state

        return WledResult(ok=True, value=_parse_state(fixture))

    monkeypatch.setattr(wled_driver_mod.WledDriver, "state", fake_state)
    board = run_cycle(config, now_us=NOW_US, lighting_engine=_FakeEngine(boom=True))
    assert board is not None
    assert config.dash is not None
    assert load_board(config.dash.state_dir) == board  # board landed despite the explosion


def test_cycle_reconciles_through_the_injected_engine(tmp_path: Path, monkeypatch: Any) -> None:
    from obi.core.drivers import wled as wled_driver_mod

    config = _load(tmp_path)
    fixture = _fixture_raw()

    def fake_state(self: Any, base_url: str) -> Any:
        from obi.core.drivers.wled import WledResult, _parse_state

        return WledResult(ok=True, value=_parse_state(fixture))

    monkeypatch.setattr(wled_driver_mod.WledDriver, "state", fake_state)
    engine = _FakeEngine()
    run_cycle(config, now_us=NOW_US, lighting_engine=engine)
    # No routeros/inventory in this config: every pageable tile is stale/yellow ⇒ desired
    # yellow; the fixture's pure-green bands mismatch ⇒ exactly one heal write.
    assert engine.calls == [("portal", "yellow")]


def test_135_config_maintenance_red_down_item_never_reds_the_band() -> None:
    tiles = _green_pageable()
    tiles[0] = _tile(
        "fleet",
        "green",
        [
            Item(key="k", label="k", state="green"),
            Item(key="magnolia-gw", label="magnolia-gw", state="red", maintenance="config"),
        ],
    )
    assert desired_status(_board(tiles)) == "green"
