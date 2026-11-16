"""Unit (feature 095): the netwatch mapper (recorded-fixture) + evaluator (counters, windows).

The mapper tests run from tests/fixtures/netwatch-tamarack-internet-gw.json — a live-recorded
``/tool/netwatch`` print (read-only) — pinning the type=simple wire shape and the row-to-row
key inconsistency. The evaluator tests drive the counter-delta loss window (analysis finding
U2: minimum sample count, reset-on-decrease) and the pair semantics with synthesized targets.
"""

from __future__ import annotations

import json
from pathlib import Path

from obi.core import dash
from obi.core.config import DashSettings
from obi.core.dash import NetwatchTarget, RouterNetwatch, map_netwatch

FIXTURE = Path(__file__).parent.parent / "fixtures" / "netwatch-tamarack-internet-gw.json"
NOW_US = 1_787_000_000_000_000
MIN_US = 60_000_000


def _rows() -> list[dict]:
    return json.loads(FIXTURE.read_text())["netwatch"]


# --- map_netwatch over the recorded fixture ----------------------------------------------------


def test_mapper_pins_the_simple_type_wire_shape() -> None:
    targets = [map_netwatch(row) for row in _rows()]
    google = next(t for t in targets if t.host == "8.8.8.8")
    assert google.name == "tamarack-internet-google"
    assert google.up is True
    assert google.probe_type == "simple"
    assert google.done_tests == 323
    assert google.failed_tests == 0
    # The load-bearing D2 finding: simple probes measure NO latency/loss.
    assert google.rtt_avg_ms is None
    assert google.loss_pct is None


def test_mapper_tolerates_absent_optional_keys() -> None:
    # Row 1 of the fixture had disabled/script keys stripped, as observed live on tamarack-gw.
    stripped = _rows()[1]
    assert "disabled" not in stripped
    target = map_netwatch(stripped)
    assert target.disabled is False  # absent ⇒ enabled
    assert target.up is True


def test_mapper_total_over_an_empty_record() -> None:
    target = map_netwatch({})
    assert target.up is False  # no status ⇒ not "up" — never a false green
    assert target.host == "" and target.name == ""
    assert target.done_tests is None


def test_mapper_captures_icmp_type_stats_opportunistically() -> None:
    target = map_netwatch(
        {
            "name": "x", "host": "172.25.110.1", "type": "icmp", "status": "up",
            "rtt-avg": "2ms320us", "loss-percent": "12%",
            "done-tests": 5, "failed-tests": 1,
        }
    )
    assert target.probe_type == "icmp"
    assert target.rtt_avg_ms == 2.32
    assert target.loss_pct == 12.0


def test_opportunistic_ms_shapes() -> None:
    assert dash._opportunistic_ms(3) == 3.0  # numeric ⇒ ms as-is
    assert dash._opportunistic_ms("15ms") == 15.0
    assert dash._opportunistic_ms("1s20ms") == 1020.0
    assert dash._opportunistic_ms("450us") == 0.45
    assert dash._opportunistic_ms("weird") is None
    assert dash._opportunistic_ms(None) is None
    assert dash._opportunistic_ms(True) is None  # bools are not measurements


# --- _eval_netwatch: pairs, counters, windows --------------------------------------------------


def _target(
    *, host: str = "8.8.8.8", name: str = "gw-google", up: bool = True,
    done: int | None = None, failed: int | None = None, disabled: bool = False,
    rtt: float | None = None, loss: float | None = None, probe_type: str = "simple",
) -> NetwatchTarget:
    return NetwatchTarget(
        name=name, host=host, up=up, probe_type=probe_type, since="2026-08-25 07:00:00",
        disabled=disabled, done_tests=done, failed_tests=failed,
        rtt_avg_ms=rtt, loss_pct=loss,
    )


def _eval(routers: list[RouterNetwatch], prev: dict | None = None, *, now_us: int = NOW_US):
    return dash._eval_netwatch(routers, DashSettings(), now_us=now_us, prev=prev or {})


def test_down_target_is_red_and_disabled_is_dropped() -> None:
    routers = [
        RouterNetwatch(
            router="tamarack-gw",
            targets=[
                _target(host="172.18.83.1", name="tamarack-basswood", up=False),
                _target(host="1.1.1.1", name="noc-cloudflare", disabled=True),
            ],
        )
    ]
    tile, _state = _eval(routers)
    assert tile.state == "red"
    assert [i.key for i in tile.items] == ["tamarack-gw|172.18.83.1"]  # disabled row never renders
    assert tile.items[0].label == "tamarack-gw→tamarack-basswood"
    assert "down" in tile.items[0].detail


def test_router_error_is_stale_not_red() -> None:
    tile, _state = _eval([RouterNetwatch(router="dead-gw", error="connect timeout")])
    assert tile.state == "stale"
    assert "poll failed" in tile.items[0].detail


def test_both_ends_watching_each_other_stay_distinct_items() -> None:
    routers = [
        RouterNetwatch(router="tamarack-gw", targets=[_target(host="172.18.83.2", name="to-mtn")]),
        RouterNetwatch(router="basswood-gw", targets=[_target(host="172.18.89.2", name="to-shw")]),
    ]
    tile, state = _eval(routers)
    assert {i.key for i in tile.items} == {"tamarack-gw|172.18.83.2", "basswood-gw|172.18.89.2"}
    assert set(state) == {"tamarack-gw|172.18.83.2", "basswood-gw|172.18.89.2"}


def test_loss_window_accumulates_and_judges_after_min_tests() -> None:
    # Cycle 1 seeds the window: no judgment possible from one sample.
    routers = [RouterNetwatch(router="gw", targets=[_target(done=100, failed=0)])]
    tile, state = _eval(routers)
    assert tile.items[0].state == "green"
    # Cycle 2: 12 more tests, 2 failed ⇒ 16.7% over ≥10 tests ⇒ yellow, named with the window.
    routers = [RouterNetwatch(router="gw", targets=[_target(done=112, failed=2)])]
    tile, state = _eval(routers, state, now_us=NOW_US + MIN_US)
    item = tile.items[0]
    assert item.state == "yellow"
    assert "loss 17% (5m)" in item.detail
    assert item.metrics["window_tests"] == 12


def test_loss_below_min_tests_is_not_judged() -> None:
    # 5 tests with 1 failed reads 20% — statistically nothing; must stay green (finding U2).
    routers = [RouterNetwatch(router="gw", targets=[_target(done=100, failed=0)])]
    _tile, state = _eval(routers)
    routers = [RouterNetwatch(router="gw", targets=[_target(done=105, failed=1)])]
    tile, _state = _eval(routers, state, now_us=NOW_US + MIN_US)
    assert tile.items[0].state == "green"


def test_counter_decrease_resets_the_window() -> None:
    # A netwatch restart (config edit, reboot) resets the counters — observed live; the window
    # must start over rather than compute a negative/false delta.
    routers = [RouterNetwatch(router="gw", targets=[_target(done=300, failed=6)])]
    _tile, state = _eval(routers)
    routers = [RouterNetwatch(router="gw", targets=[_target(done=8, failed=0)])]
    tile, state = _eval(routers, state, now_us=NOW_US + MIN_US)
    assert tile.items[0].state == "green"  # one fresh sample — no judgment
    assert len(state["gw|8.8.8.8"]["samples"]) == 1  # the pre-reset history is gone


def test_window_drops_samples_older_than_the_window() -> None:
    routers = [RouterNetwatch(router="gw", targets=[_target(done=100, failed=0)])]
    _tile, state = _eval(routers)
    # 10 minutes later (window is 5m): the old sample must have aged out.
    routers = [RouterNetwatch(router="gw", targets=[_target(done=160, failed=60)])]
    tile, state = _eval(routers, state, now_us=NOW_US + 10 * MIN_US)
    assert tile.items[0].state == "green"  # only one in-window sample — no delta, no judgment
    assert len(state["gw|8.8.8.8"]["samples"]) == 1


def test_icmp_type_direct_stats_judge_without_a_window() -> None:
    targets = [_target(rtt=40.0, loss=8.0, probe_type="icmp")]
    routers = [RouterNetwatch(router="gw", targets=targets)]
    tile, _state = _eval(routers)
    item = tile.items[0]
    assert item.state == "yellow"
    assert "loss 8%" in item.detail
    assert "rtt 40 ms" in item.detail


# --- 128: maintenance ignore-if-down (FR-006a) -------------------------------------------------


def test_maintenance_target_down_is_yellow_and_rollup_stays_green() -> None:
    routers = [
        RouterNetwatch(
            router="tamarack-gw",
            targets=[
                _target(host="172.18.83.9", name="tamarack-ptp-60-e", up=False),
                _target(host="8.8.8.8", name="gw-google", up=True),
            ],
        )
    ]
    tile, _state = dash._eval_netwatch(
        routers, DashSettings(), now_us=NOW_US, prev={}, maintenance={"tamarack-ptp-60-e"}
    )
    flagged = next(i for i in tile.items if i.key == "tamarack-gw|172.18.83.9")
    assert flagged.state == "yellow"
    assert flagged.detail.startswith("maintenance · down")
    assert flagged.metrics["maintenance"] == 1
    assert tile.state == "green"  # planned work never turns the wall red — and never pages


def test_maintenance_match_is_case_insensitive_and_by_host_too() -> None:
    routers = [
        RouterNetwatch(
            router="tamarack-gw",
            targets=[_target(host="172.18.83.9", name="Tamarack-PTP-60-E", up=False)],
        )
    ]
    tile, _ = dash._eval_netwatch(
        routers, DashSettings(), now_us=NOW_US, prev={}, maintenance={"tamarack-ptp-60-e"}
    )
    assert tile.items[0].state == "yellow"
    tile, _ = dash._eval_netwatch(
        routers, DashSettings(), now_us=NOW_US, prev={}, maintenance={"172.18.83.9"}
    )
    assert tile.items[0].state == "yellow"


def test_maintenance_never_hides_a_real_red() -> None:
    routers = [
        RouterNetwatch(
            router="tamarack-gw",
            targets=[
                _target(host="172.18.83.9", name="tamarack-ptp-60-e", up=False),
                _target(host="172.18.83.1", name="tamarack-basswood", up=False),
            ],
        )
    ]
    tile, _ = dash._eval_netwatch(
        routers, DashSettings(), now_us=NOW_US, prev={}, maintenance={"tamarack-ptp-60-e"}
    )
    assert tile.state == "red"  # the unflagged down pair still owns the tile


def test_no_maintenance_set_changes_nothing() -> None:
    routers = [
        RouterNetwatch(
            router="tamarack-gw",
            targets=[_target(host="172.18.83.9", name="tamarack-ptp-60-e", up=False)],
        )
    ]
    tile, _ = dash._eval_netwatch(routers, DashSettings(), now_us=NOW_US, prev={})
    assert tile.items[0].state == "red"
    assert tile.state == "red"


# --- 135: probes from/toward maintenance sites, poll-failed routers, UISP ptp alias ------------

from obi.core.config import MaintenanceSettings  # noqa: E402
from obi.core.maintenance import Maintenance  # noqa: E402


def _teardown(flagged: tuple[str, ...] = ()) -> Maintenance:
    return Maintenance.build(
        MaintenanceSettings(sites=("wingnut", "manzanita", "noc", "magnolia")), flagged
    )


def test_probes_toward_maintenance_sites_are_labeled_and_out_of_rollup() -> None:
    routers = [
        RouterNetwatch(
            router="tamarack-gw",
            targets=[
                _target(host="169.254.13.5", name="tamarack-manzanita-11", up=False),
                _target(host="169.254.13.9", name="tamarack-magnolia-05", up=False),
                _target(host="172.18.112.2", name="tamarack-vtep-magnolia", up=False),
                _target(host="8.8.8.8", name="tamarack-google", up=True),
            ],
        ),
        RouterNetwatch(
            router="basswood-gw",
            targets=[_target(host="169.254.7.9", name="basswood-manzanita-70", up=False)],
        ),
    ]
    tile, _ = dash._eval_netwatch(
        routers, DashSettings(), now_us=NOW_US, prev={}, maintenance=_teardown()
    )
    assert tile.state == "green"
    flagged = [i for i in tile.items if i.maintenance is not None]
    assert len(flagged) == 4
    assert all(i.state == "yellow" for i in flagged)


def test_poll_failed_maintenance_router_does_not_stale_the_tile() -> None:
    routers = [
        RouterNetwatch(router="noc-gw", targets=[], error="timed_out: timed out"),
        RouterNetwatch(router="tamarack-gw", targets=[_target(name="tamarack-google")]),
    ]
    tile, _ = dash._eval_netwatch(
        routers, DashSettings(), now_us=NOW_US, prev={}, maintenance=_teardown()
    )
    assert tile.state == "green"
    noc = next(i for i in tile.items if i.key == "noc-gw")
    assert noc.maintenance is not None and "poll failed" in noc.detail


def test_poll_failed_live_router_still_stales() -> None:
    routers = [RouterNetwatch(router="chestnut-gw", targets=[], error="timed_out: timed out")]
    tile, _ = dash._eval_netwatch(
        routers, DashSettings(), now_us=NOW_US, prev={}, maintenance=_teardown()
    )
    assert tile.state == "stale"


def test_uisp_flag_reaches_the_ptp_less_netwatch_alias() -> None:
    routers = [
        RouterNetwatch(
            router="tamarack-gw",
            targets=[_target(host="169.254.13.5", name="tamarack-manzanita-11", up=False)],
        )
    ]
    tile, _ = dash._eval_netwatch(
        routers,
        DashSettings(),
        now_us=NOW_US,
        prev={},
        maintenance=Maintenance.build(None, ["tamarack-ptp-manzanita-11"]),
    )
    assert tile.state == "green"
    assert tile.items[0].maintenance == "uisp"
