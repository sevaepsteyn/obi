"""Unit (feature 121): the MESH dash tile — the signal that would have caught 2026-08-31.

The mesh radio went dark for 29 hours and nobody noticed, because the failure only ever wrote
log lines. These tests drive the pure evaluator with synthesized health, plus the collector
against fakes. The collector's defining property — it can never transmit — is pinned in
tests/contract/test_dash_no_alerting.py alongside the no-paging guarantees.
"""

from __future__ import annotations

from obi.core.config import DashMeshSettings, DashSettings
from obi.core.dash import (
    DashInputs,
    MeshHealth,
    TrackedNode,
    _eval_mesh,
    evaluate,
)

NOW_US = 1_787_000_000_000_000


def _settings(**kwargs: object) -> DashSettings:
    return DashSettings(**kwargs)  # type: ignore[arg-type]


def _healthy(**kwargs: object) -> MeshHealth:
    base: dict[str, object] = {
        "reachable": True,
        "detail": "attached on /dev/cu.usbmodem111301",
        "nodes": [
            TrackedNode(node_id="!91378e3a", label="Tooma", last_heard_age_s=60, snr_db=13.0)
        ],
        "newest_fix_age_s": 120.0,
    }
    base.update(kwargs)
    return MeshHealth(**base)  # type: ignore[arg-type]


def test_attached_radio_and_recently_heard_node_is_green() -> None:
    tile = _eval_mesh(_healthy(), _settings())
    assert tile.signal == "mesh"
    assert tile.state == "green"
    assert "last heard just now" in tile.items[1].detail
    assert "SNR 13" in tile.items[1].detail


def test_unreachable_radio_is_red_and_names_the_reason() -> None:
    """The 2026-08-31 shape: discovery found only the walkie-talkie."""

    health = MeshHealth(
        reachable=False,
        detail="/dev/cu.usbserial-FBC3793547DF belongs to the radio station",
    )
    tile = _eval_mesh(health, _settings())
    assert tile.state == "red"
    assert "radio station" in tile.items[0].detail


def test_quiet_node_goes_yellow_then_red() -> None:
    settings = _settings(mesh=DashMeshSettings(heard_yellow_after="2h", heard_red_after="12h"))
    fresh = _eval_mesh(_healthy(nodes=[TrackedNode("!a", "Tooma", last_heard_age_s=600)]), settings)
    yellow = _eval_mesh(
        _healthy(nodes=[TrackedNode("!a", "Tooma", last_heard_age_s=3 * 3600)]), settings
    )
    red = _eval_mesh(
        _healthy(nodes=[TrackedNode("!a", "Tooma", last_heard_age_s=13 * 3600)]), settings
    )
    assert (fresh.state, yellow.state, red.state) == ("green", "yellow", "red")


def test_node_absent_from_the_table_is_yellow_not_forgotten() -> None:
    """The 117 eviction case. A missing node must not let the board go green by omission."""

    tile = _eval_mesh(
        _healthy(nodes=[TrackedNode("!91378e3a", "Tooma", last_heard_age_s=None)]), _settings()
    )
    assert tile.state == "yellow"
    assert "not in the radio's node table" in tile.items[1].detail


def test_stale_recorded_fix_goes_yellow() -> None:
    settings = _settings(mesh=DashMeshSettings(fix_yellow_after="6h"))
    tile = _eval_mesh(_healthy(newest_fix_age_s=7 * 3600), settings)
    assert tile.state == "yellow"
    assert tile.items[-1].label == "newest fix"


def test_no_recorded_fixes_is_green_not_a_fault() -> None:
    """An empty history is a new install, not a problem to chase."""

    tile = _eval_mesh(_healthy(newest_fix_age_s=None), _settings())
    assert tile.state == "green"
    assert "none recorded" in tile.items[-1].detail


def test_observation_dated_fix_is_marked() -> None:
    """Mirrors `obi mesh track`'s "~": the device stamped no fix time, so this is when WE
    saw it, not when the tracker says it was there."""

    tile = _eval_mesh(
        _healthy(newest_fix_age_s=300.0, newest_fix_dated_by_observation=True), _settings()
    )
    assert tile.items[-1].detail.startswith("~")


def test_unconfigured_mesh_emits_no_tile() -> None:
    """Contract B1: absent, not stale. A machine with no mesh radio has no mesh gap."""

    board = evaluate(DashInputs(mesh=None), _settings(), now_us=NOW_US).board
    assert [t.signal for t in board.tiles if t.signal == "mesh"] == []


def test_configured_mesh_appears_after_the_core_signals() -> None:
    board = evaluate(DashInputs(mesh=_healthy()), _settings(), now_us=NOW_US).board
    signals = [t.signal for t in board.tiles]
    assert signals[-1] == "mesh"
    assert board.tile("mesh") is not None


def test_red_mesh_tile_taints_the_board_rollup() -> None:
    """Display-only means it does not PAGE — it still colours the wall."""

    board = evaluate(
        DashInputs(mesh=MeshHealth(reachable=False, detail="no Meshtastic USB device found")),
        _settings(),
        now_us=NOW_US,
    ).board
    assert board.tile("mesh").state == "red"
    assert board.state == "red"


# --- feature 123: daemon states on the tile ----------------------------------------------


def test_standby_radio_is_yellow_on_loan_not_green() -> None:
    """123/O7: a loaned-out radio must never present as listening."""

    tile = _eval_mesh(
        _healthy(standby=True, detail="radio on loan to cli:mesh until 12:40"),
        _settings(),
    )
    assert tile.state == "yellow"
    assert tile.items[0].state == "yellow"
    assert "on loan" in tile.items[0].detail


def test_daemon_down_is_red_and_names_the_daemon() -> None:
    """123/FR-012: "service down" and "radio silent" must read differently."""

    health = MeshHealth(
        reachable=False,
        detail="the mesh daemon isn't reachable at /tmp/daemon.sock (connection refused)",
    )
    tile = _eval_mesh(health, _settings())
    assert tile.state == "red"
    assert "daemon" in tile.items[0].detail


# --- 135: the mesh signal in maintenance is shown but never decides the board ---------------------

from obi.core.config import MaintenanceSettings  # noqa: E402
from obi.core.dash import worst  # noqa: E402
from obi.core.maintenance import Maintenance  # noqa: E402


def test_mesh_in_maintenance_is_labeled_and_out_of_board_state() -> None:
    board = evaluate(
        DashInputs(
            mesh=MeshHealth(reachable=False, detail="no Meshtastic USB device found"),
            maintenance=Maintenance.build(MaintenanceSettings(signals=("mesh",), reason="off")),
        ),
        _settings(),
        now_us=NOW_US,
    ).board
    mesh = board.tile("mesh")
    assert mesh is not None and mesh.maintenance == "config: off"
    assert all(i.maintenance == "config: off" for i in mesh.items)
    assert mesh.state == "yellow"  # visible, never red
    others = [t.state for t in board.tiles if t.signal != "mesh"]
    assert board.state == worst(others)  # decided without the mesh tile


def test_mesh_without_maintenance_still_reds_the_board() -> None:
    board = evaluate(
        DashInputs(mesh=MeshHealth(reachable=False, detail="no Meshtastic USB device found")),
        _settings(),
        now_us=NOW_US,
    ).board
    assert board.tile("mesh").state == "red"  # type: ignore[union-attr]
    assert board.state == "red"
