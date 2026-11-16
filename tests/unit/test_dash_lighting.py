"""Unit (feature 121): the LIGHTING dash tile — can obi still reach the portal controllers?

The tile answers reachability, not taste. A controller that is reachable but switched off is
green: the portal being dark is a decision someone made, and a dashboard that nags about it
would train the operator to ignore the wall.
"""

from __future__ import annotations

from obi.core.config import DashSettings
from obi.core.dash import (
    DashInputs,
    LightingController,
    LightingHealth,
    _eval_lighting,
    evaluate,
)

NOW_US = 1_787_000_000_000_000


def _settings(**kwargs: object) -> DashSettings:
    return DashSettings(**kwargs)  # type: ignore[arg-type]


def test_reachable_controllers_are_green() -> None:
    tile = _eval_lighting(
        LightingHealth(
            controllers=[LightingController("portal", reachable=True, on=True, brightness=128)]
        )
    )
    assert tile.signal == "lighting"
    assert tile.state == "green"
    assert "on" in tile.items[0].detail
    assert "brightness 128" in tile.items[0].detail


def test_reachable_but_off_is_green_with_detail() -> None:
    """Contract B5. Off is a decision, not a fault."""

    tile = _eval_lighting(
        LightingHealth(controllers=[LightingController("portal", reachable=True, on=False)])
    )
    assert tile.state == "green"
    assert tile.items[0].detail == "off"


def test_unreachable_controller_is_yellow_not_red() -> None:
    """Operator decision 2026-09-01, the day this shipped: the board rollup feeds
    dash_status, the primary "how's the network" answer on radio and Slack. A decorative
    LED controller returning 502 must not make obi tell a caller the network is red."""

    tile = _eval_lighting(
        LightingHealth(
            controllers=[LightingController("portal", reachable=False, detail="connect timeout")]
        )
    )
    assert tile.state == "yellow"
    assert "connect timeout" in tile.items[0].detail


def test_worst_of_rollup_across_controllers() -> None:
    tile = _eval_lighting(
        LightingHealth(
            controllers=[
                LightingController("portal", reachable=True, on=True, brightness=200),
                LightingController("spare", reachable=False, detail="no route to host"),
            ]
        )
    )
    assert tile.state == "yellow"
    assert [item.state for item in tile.items] == ["green", "yellow"]


def test_no_controllers_is_green_not_empty() -> None:
    tile = _eval_lighting(LightingHealth(controllers=[]))
    assert tile.state == "green"
    assert tile.items[0].detail == "none configured"


def test_unconfigured_lighting_emits_no_tile() -> None:
    """Contract B1: no [wled] section ⇒ the tile is absent, not stale."""

    board = evaluate(DashInputs(lighting=None), _settings(), now_us=NOW_US).board
    assert board.tile("lighting") is None


def test_configured_lighting_appears_on_the_board() -> None:
    board = evaluate(
        DashInputs(
            lighting=LightingHealth(
                controllers=[LightingController("portal", True, on=True)]
            )
        ),
        _settings(),
        now_us=NOW_US,
    ).board
    assert board.tile("lighting") is not None
    assert board.tile("lighting").state == "green"


def test_lighting_in_maintenance_does_not_yellow_the_board() -> None:
    # 135: the portal is off for the season — the tile stays visible, labeled.
    from obi.core.config import MaintenanceSettings
    from obi.core.dash import worst as dash_worst
    from obi.core.maintenance import Maintenance

    inputs = DashInputs(
        lighting=LightingHealth(controllers=[LightingController("portal", False, detail="down")]),
        maintenance=Maintenance.build(MaintenanceSettings(signals=("lighting",))),
    )
    board = evaluate(inputs, _settings(), now_us=NOW_US).board
    tile = board.tile("lighting")
    assert tile is not None and tile.maintenance == "config"
    assert tile.items[0].maintenance == "config"
    others = [t.state for t in board.tiles if t.signal != "lighting"]
    assert board.state == dash_worst(others)  # decided without the lighting tile
