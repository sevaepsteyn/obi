"""Unit (feature 095): the pure evaluator — frame honesty, fleet rules, wan thresholds.

Every test drives :func:`obi.core.dash.evaluate` (or a per-signal evaluator) with synthesized
inputs and a fixed ``now`` — no filesystem, no network. The frame's contract under test: a
``None`` input renders that tile stale-with-hint (never green, FR-009/FR-016), the rollup is
worst-of with stale between green and yellow, and cycle notes surface verbatim.
"""

from __future__ import annotations

from obi.core import dash
from obi.core.config import DashInternetLink, DashSettings
from obi.core.dash import DashInputs, UplinkReading, evaluate
from obi.core.icmpstore import DeviceState

NOW_US = 1_787_000_000_000_000
MIN_US = 60_000_000


def _settings(**kwargs: object) -> DashSettings:
    return DashSettings(**kwargs)  # type: ignore[arg-type]


def _device(
    name: str,
    *,
    up: bool = True,
    changed_ago_s: float = 3600,
    age_s: float = 5,
    rtt_us: int | None = 2100,
) -> DeviceState:
    return DeviceState(
        device=name,
        address=f"172.18.84.{len(name)}",
        up=up,
        consecutive_misses=0 if up else 2,
        last_seen_us=NOW_US if up else None,
        last_change_us=NOW_US - round(changed_ago_s * 1_000_000),
        updated_us=NOW_US - round(age_s * 1_000_000),
        last_rtt_us=rtt_us if up else None,
    )


# --- the frame ---------------------------------------------------------------------------------


def test_empty_inputs_render_all_stale_tiles_with_hints() -> None:
    result = evaluate(DashInputs(), _settings(), now_us=NOW_US)
    board = result.board
    assert [t.signal for t in board.tiles] == [
        s for s in dash.SIGNALS if s not in dash.OPTIONAL_SIGNALS
    ]  # 121: mesh/lighting are absent, not stale, when unconfigured
    for tile in board.tiles:
        if tile.signal == "internet":
            continue  # zero configured uplinks is a choice, not a data gap
        assert tile.state == "stale"
        assert tile.items[0].detail  # every placeholder names the fixing command
    assert board.state == "stale"  # never green off missing data


def test_unconfigured_internet_is_green_not_stale() -> None:
    result = evaluate(DashInputs(), _settings(), now_us=NOW_US)
    internet = result.board.tile("internet")
    assert internet is not None and internet.state == "green"
    assert "no internet_links configured" in internet.items[0].detail


def test_rollup_is_worst_of_tiles_and_notes_propagate() -> None:
    inputs = DashInputs(fleet=[_device("core-gw-1", up=False)], notes=["uisp snapshot absent"])
    result = evaluate(inputs, _settings(), now_us=NOW_US)
    assert result.board.state == "red"  # fleet red beats the stale placeholders
    assert result.board.notes == ["uisp snapshot absent"]


def test_generated_at_matches_now() -> None:
    result = evaluate(DashInputs(), _settings(), now_us=NOW_US)
    assert result.board.generated_at_us == NOW_US


def test_carried_state_survives_a_skipped_poll() -> None:
    carried = {"netwatch": {"gw|8.8.8.8": {"done_tests": 9}}, "ping": {"8.8.8.8": {}}}
    result = evaluate(DashInputs(carried=carried), _settings(), now_us=NOW_US)
    assert result.state["netwatch"] == carried["netwatch"]  # memory not lost on a no-data cycle
    assert result.state["ping"] == carried["ping"]


# --- fleet -------------------------------------------------------------------------------------


def test_fleet_green_up_with_rtt_detail() -> None:
    tile = dash._eval_fleet([_device("core-gw-1")], _settings(), now_us=NOW_US)
    assert tile.state == "green"
    item = tile.items[0]
    assert item.state == "green"
    assert "rtt 2.1 ms" in item.detail
    assert item.metrics["rtt_ms"] == 2.1


def test_fleet_red_when_down_with_since() -> None:
    tile = dash._eval_fleet(
        [_device("tamarack-gw", up=False, changed_ago_s=840)], _settings(), now_us=NOW_US
    )
    assert tile.state == "red"
    item = tile.items[0]
    assert item.state == "red"
    assert "down 14m" in item.detail
    assert item.since_us == NOW_US - 840 * 1_000_000


def test_fleet_yellow_within_flap_window() -> None:
    tile = dash._eval_fleet([_device("chestnut-gw", changed_ago_s=120)], _settings(), now_us=NOW_US)
    assert tile.state == "yellow"
    assert "recovered 2m ago" in tile.items[0].detail


def test_fleet_flap_window_is_configurable() -> None:
    settings = _settings(flap_window="1m")
    tile = dash._eval_fleet([_device("chestnut-gw", changed_ago_s=120)], settings, now_us=NOW_US)
    assert tile.state == "green"  # 2m ago is outside a 1m window


def test_fleet_stale_item_when_probe_aged_out() -> None:
    # An individual row older than 3×60s+60s reads stale, and taints the tile.
    tile = dash._eval_fleet(
        [_device("core-gw-1"), _device("lost-sw", age_s=600)], _settings(), now_us=NOW_US
    )
    stale_items = [i for i in tile.items if i.state == "stale"]
    assert [i.key for i in stale_items] == ["lost-sw"]
    assert "no probe for 10m" in stale_items[0].detail
    assert tile.state == "stale"


def test_fleet_stale_never_hides_red() -> None:
    tile = dash._eval_fleet(
        [_device("dead-gw", up=False), _device("lost", age_s=600)], _settings(), now_us=NOW_US
    )
    assert tile.state == "red"  # old bad news beats false comfort, stale beats green


def test_fleet_green_without_rtt_still_green() -> None:
    tile = dash._eval_fleet([_device("core-gw-1", rtt_us=None)], _settings(), now_us=NOW_US)
    assert tile.state in ("green", "yellow")
    assert tile.items[0].detail.startswith("up")


def test_fleet_maintenance_shows_yellow_but_never_taints_the_rollup() -> None:
    # The operator's ask verbatim: maintenance devices show yellow; fleet stays green when
    # they are the only non-green items.
    tile = dash._eval_fleet(
        [_device("core-gw-1"), _device("yew-ptp-basswood-05", up=False, changed_ago_s=840)],
        _settings(),
        now_us=NOW_US,
        maintenance={"yew-ptp-basswood-05"},
    )
    assert tile.state == "green"  # the down device is planned work, not an incident
    flagged = next(i for i in tile.items if i.key == "yew-ptp-basswood-05")
    assert flagged.state == "yellow"
    assert flagged.detail.startswith("maintenance · down 14m")
    assert flagged.metrics["maintenance"] == 1


def test_fleet_maintenance_up_device_is_also_yellow() -> None:
    tile = dash._eval_fleet(
        [_device("yew-ptp-basswood-05")],
        _settings(),
        now_us=NOW_US,
        maintenance={"yew-ptp-basswood-05"},
    )
    assert tile.items[0].state == "yellow"  # visibly flagged even while up
    assert tile.state == "green"  # and still excluded from the rollup


def test_fleet_maintenance_never_hides_a_real_red() -> None:
    tile = dash._eval_fleet(
        [
            _device("dead-gw", up=False),
            _device("yew-ptp-basswood-05", up=False),
        ],
        _settings(),
        now_us=NOW_US,
        maintenance={"yew-ptp-basswood-05"},
    )
    assert tile.state == "red"  # the non-maintenance red still owns the tile


def test_fleet_maintenance_probe_gap_still_reads_stale() -> None:
    # A data gap is about OUR probing, not the device: stale wins the item, rollup still clean.
    tile = dash._eval_fleet(
        [_device("core-gw-1"), _device("yew-ptp-basswood-05", age_s=600)],
        _settings(),
        now_us=NOW_US,
        maintenance={"yew-ptp-basswood-05"},
    )
    flagged = next(i for i in tile.items if i.key == "yew-ptp-basswood-05")
    assert flagged.state == "stale"
    assert tile.state == "green"


# --- internet (uplink utilization; renamed from wan) -------------------------------------------

_LINK = DashInternetLink(
    device="tamarack-internet-gw",
    interface="sfp-sfpplus5-sycamore",
    capacity_mbps=2500,
    label="sycamore",
)


def _reading(
    *,
    in_bps: float | None = 100e6,
    out_bps: float | None = 50e6,
    age: float | None = 200.0,
    error: str | None = None,
) -> UplinkReading:
    return UplinkReading(link=_LINK, in_bps=in_bps, out_bps=out_bps, sample_age_s=age, error=error)


def test_internet_green_shows_both_directions() -> None:
    tile = dash._eval_internet([_reading()], _settings(internet_links=[_LINK]), now_us=NOW_US)
    assert tile.state == "green"
    item = tile.items[0]
    assert item.label == "sycamore"
    # Both directions against the provisioned capacity, always (operator ask 2026-08-26).
    assert "in 100/2500 Mb/s (4%)" in item.detail
    assert "out 50/2500 Mb/s (2%)" in item.detail
    assert item.metrics["in_pct"] == 4.0
    assert item.metrics["out_pct"] == 2.0
    assert item.metrics["capacity_mbps"] == 2500


def test_internet_yellow_at_warn_red_at_down_on_either_direction() -> None:
    settings = _settings(internet_links=[_LINK])
    yellow = dash._eval_internet([_reading(in_bps=1800e6)], settings, now_us=NOW_US)
    assert yellow.state == "yellow"  # in at 72%
    red = dash._eval_internet([_reading(out_bps=2300e6)], settings, now_us=NOW_US)
    assert red.state == "red"  # out at 92% — the busier direction judges
    assert "out 2300/2500 Mb/s (92%)" in red.items[0].detail


def test_internet_stale_sample_and_missing_rate() -> None:
    settings = _settings(internet_links=[_LINK])
    old = dash._eval_internet([_reading(age=2000.0)], settings, now_us=NOW_US)
    assert old.state == "stale"  # a 5-minute feed older than 15m+60s is a gap, not a judgment
    absent = dash._eval_internet(
        [_reading(in_bps=None, out_bps=None, age=None)], settings, now_us=NOW_US
    )
    assert absent.state == "stale"
    assert "no rate sample" in absent.items[0].detail


def test_internet_error_reading_is_stale_with_detail() -> None:
    tile = dash._eval_internet(
        [_reading(error="metrics store locked")], _settings(internet_links=[_LINK]), now_us=NOW_US
    )
    assert tile.state == "stale"
    assert tile.items[0].detail == "metrics store locked"


# --- 135: config ∪ UISP maintenance on the fleet tile -------------------------------------------

from obi.core.config import MaintenanceSettings  # noqa: E402
from obi.core.maintenance import Maintenance  # noqa: E402

_TEARDOWN = Maintenance.build(
    MaintenanceSettings(
        sites=("wingnut", "manzanita", "noc", "magnolia"), reason="post-burn teardown"
    ),
    ["yew-ptp-basswood-05"],
)


def test_config_site_devices_are_labeled_and_out_of_the_rollup() -> None:
    board = evaluate(
        DashInputs(
            fleet=[
                _device("core-gw-1"),
                _device("magnolia-gw", up=False, changed_ago_s=33 * 86400),
                _device("noc-ap-birch", up=False, changed_ago_s=33 * 86400),
                _device("yew-ptp-basswood-05", up=False, changed_ago_s=50 * 86400),
            ],
            maintenance=_TEARDOWN,
        ),
        _settings(),
        now_us=NOW_US,
    ).board
    fleet = board.tile("fleet")
    assert fleet is not None and fleet.state == "green"
    by_key = {i.key: i for i in fleet.items}
    assert by_key["magnolia-gw"].state == "yellow"
    assert by_key["magnolia-gw"].maintenance == "config: post-burn teardown"
    assert by_key["magnolia-gw"].detail.startswith("maintenance · down")
    assert by_key["magnolia-gw"].metrics["maintenance"] == 1
    assert by_key["yew-ptp-basswood-05"].maintenance == "uisp"
    assert by_key["core-gw-1"].maintenance is None


def test_unmatched_real_outage_still_reds_the_tile() -> None:
    # M1: joshua is genuinely down and in no declaration — byte-identical to pre-135.
    board = evaluate(
        DashInputs(
            fleet=[
                _device("magnolia-gw", up=False, changed_ago_s=33 * 86400),
                _device("rowan-ptp-joshua-st-05", up=False, changed_ago_s=33 * 86400),
            ],
            maintenance=_TEARDOWN,
        ),
        _settings(),
        now_us=NOW_US,
    ).board
    fleet = board.tile("fleet")
    assert fleet is not None and fleet.state == "red"
    trailer = next(i for i in fleet.items if i.key == "rowan-ptp-joshua-st-05")
    assert trailer.state == "red" and trailer.maintenance is None


def test_every_item_in_maintenance_reads_green() -> None:
    board = evaluate(
        DashInputs(
            fleet=[_device("magnolia-gw", up=False), _device("manzanita-gw", up=False)],
            maintenance=_TEARDOWN,
        ),
        _settings(),
        now_us=NOW_US,
    ).board
    fleet = board.tile("fleet")
    assert fleet is not None and fleet.state == "green"


def test_maintenance_matcher_supersedes_bare_flag_set() -> None:
    board = evaluate(
        DashInputs(
            fleet=[_device("magnolia-gw", up=False)],
            fleet_maintenance={"something-else"},
            maintenance=_TEARDOWN,
        ),
        _settings(),
        now_us=NOW_US,
    ).board
    fleet = board.tile("fleet")
    assert fleet is not None and fleet.items[0].maintenance is not None
