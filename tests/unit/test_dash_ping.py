"""Unit (feature 095): the PING tile (reference latency) evaluator + collector (the 028 ping seam).

Evaluator rules under test: red requires EVERY measurable ref at full loss for
``ping_down_after`` consecutive cycles (debounce carried in state); one lost ref or a
threshold crossing is yellow; an unmeasurable probe is stale and judges nothing (a dead border
router must never read as "internet down"). Collector: full loss is data, connect error is
stale (the seam's totality contract), and the ref router resolves by identity or address.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from obi.core import dash
from obi.core.config import DashSettings
from obi.core.dash import RefProbe

NOW_US = 1_787_000_000_000_000


def _probe(
    address: str = "8.8.8.8",
    *,
    rtt: float | None = 22.0,
    loss: float | None = 0.0,
    error: str | None = None,
) -> RefProbe:
    return RefProbe(
        address=address, label=dash.ref_label(address), rtt_avg_ms=rtt, loss_pct=loss, error=error
    )


def _eval(probes: list[RefProbe], prev: dict | None = None, **kwargs: object):
    return dash._eval_ping(
        probes,
        DashSettings(**kwargs),
        now_us=NOW_US,
        prev=prev or {},  # type: ignore[arg-type]
    )


def test_healthy_refs_are_green_with_measurements() -> None:
    tile, state = _eval([_probe(), _probe("1.1.1.1", rtt=24.0)])
    assert tile.state == "green"
    assert tile.items[0].label == "google"
    assert tile.items[1].label == "cloudflare"
    assert "rtt 22 ms" in tile.items[0].detail
    assert state == {"full_loss_cycles": 0}


def test_threshold_rtt_or_loss_is_yellow() -> None:
    tile, _ = _eval([_probe(rtt=63.0), _probe("1.1.1.1")])
    assert tile.state == "yellow"
    assert "rtt 63 ms" in tile.items[0].detail
    tile, _ = _eval([_probe(loss=30.0), _probe("1.1.1.1")])
    assert "loss 30%" in tile.items[0].detail


def test_one_ref_at_full_loss_is_yellow_never_red() -> None:
    tile, state = _eval([_probe(rtt=None, loss=100.0), _probe("1.1.1.1")])
    assert tile.state == "yellow"
    assert tile.items[0].detail == "no replies"
    assert state == {"full_loss_cycles": 0}  # not ALL refs — the debounce never starts


def test_all_refs_full_loss_debounces_then_reds() -> None:
    both_lost = [_probe(rtt=None, loss=100.0), _probe("1.1.1.1", rtt=None, loss=100.0)]
    tile, state = _eval(both_lost)
    assert tile.state == "yellow"  # cycle 1 of 2 — still debouncing
    assert state == {"full_loss_cycles": 1}
    tile, state = _eval(both_lost, state)
    assert tile.state == "red"  # sustained: both refs, second cycle
    assert state == {"full_loss_cycles": 2}
    assert "2 cycles" in tile.items[0].detail
    # Recovery resets the debounce immediately.
    tile, state = _eval([_probe(), _probe("1.1.1.1")], state)
    assert tile.state == "green"
    assert state == {"full_loss_cycles": 0}


def test_unmeasurable_probes_are_stale_and_carry_the_debounce() -> None:
    failed = [_probe(error="connect timeout", rtt=None, loss=None)]
    tile, state = _eval(failed, {"full_loss_cycles": 1})
    assert tile.state == "stale"
    assert "probe failed" in tile.items[0].detail
    assert state == {"full_loss_cycles": 1}  # carried, not reset — judge nothing blind


# --- _collect_internet over a fake driver ------------------------------------------------------


@dataclass
class _FakePing:
    sent: int = 4
    received: int = 4
    loss_pct: float = 0.0
    rtt_avg_ms: float | None = 21.5
    error: str | None = None


@dataclass
class _FakeDriver:
    results: dict[str, _FakePing]
    calls: list[tuple[str, str, int, float]] = field(default_factory=list)

    def ping(self, address: str, destination: str, *, count: int, timeout: float) -> _FakePing:
        self.calls.append((address, destination, count, timeout))
        return self.results.get(destination, _FakePing())


def _config(tmp_path, *, ping_router_name: str = "tamarack-internet-gw"):
    from obi.core.config import load_config
    from obi.core.inventory import Inventory, InventoryEntry, save
    from obi.core.models import DiscoveryScope

    save(
        Inventory(
            scope=DiscoveryScope(seeds=["172.18.75.1"]),
            entries=[InventoryEntry(address="172.18.75.1", identity="tamarack-internet-gw")],
        ),
        tmp_path / "inventory.json",
    )
    config = tmp_path / "config.toml"
    config.write_text(
        f'[logs]\ndb_path = "{tmp_path}/logs.db"\n'
        f'[inventory]\npath = "{tmp_path}/inventory.json"\n'
        '[routeros]\nusername = "obi"\npassword = "x"\n'
        '[discovery]\nseeds = ["172.18.75.1"]\n'
        f'[dash]\nstate_dir = "{tmp_path}/dash"\nping_router = "{ping_router_name}"\n'
    )
    config.chmod(0o600)
    return load_config(config)


def test_collector_pings_each_ref_from_the_resolved_router(tmp_path) -> None:
    config = _config(tmp_path)
    lost = _FakePing(received=0, loss_pct=100.0, rtt_avg_ms=None)
    driver = _FakeDriver(results={"1.1.1.1": lost})
    notes: list[str] = []
    assert config.dash is not None
    probes = dash._collect_ping(config, config.dash, notes, driver=driver)
    assert probes is not None
    assert [(c[0], c[1]) for c in driver.calls] == [
        ("172.18.75.1", "8.8.8.8"),
        ("172.18.75.1", "1.1.1.1"),
    ]
    assert probes[0].loss_pct == 0.0 and probes[0].error is None
    assert probes[1].loss_pct == 100.0 and probes[1].error is None  # full loss is DATA
    assert notes == []


def test_collector_connect_error_becomes_a_stale_probe(tmp_path) -> None:
    config = _config(tmp_path)
    driver = _FakeDriver(
        results={
            "8.8.8.8": _FakePing(error="connect refused", rtt_avg_ms=None),
            "1.1.1.1": _FakePing(error="connect refused", rtt_avg_ms=None),
        }
    )
    assert config.dash is not None
    probes = dash._collect_ping(config, config.dash, [], driver=driver)
    assert probes is not None
    assert all(p.error == "connect refused" for p in probes)


def test_collector_unresolvable_router_is_a_note_not_an_error(tmp_path) -> None:
    config = _config(tmp_path, ping_router_name="ghost-gw")
    notes: list[str] = []
    assert config.dash is not None
    probes = dash._collect_ping(config, config.dash, notes, driver=_FakeDriver(results={}))
    assert probes is None
    assert any("ghost-gw" in note for note in notes)
