"""Unit (feature 095): the full minute cycle — collect → evaluate → persist, offline.

One `run_cycle` call over a fake driver + scripted prober + fixture stores produces a board
with every data tile populated (pipeline stays the frame's placeholder until US4), writes
board.json + the carried state, and degrades per-tile when a collector input is missing.
The US3 pager seam is exercised from tests/unit/test_dash_pager.py.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from obi.core import dash
from obi.core.config import load_config
from obi.core.inventory import Inventory, InventoryEntry, save
from obi.core.models import DiscoveryScope

NOW_US = 1_787_000_000_000_000

NETWATCH_ROWS = json.loads(
    (Path(__file__).parent.parent / "fixtures" / "netwatch-tamarack-internet-gw.json").read_text()
)["netwatch"]


@dataclass
class _ReadResult:
    records: list[dict]
    truncated: bool = False
    error: str | None = None


@dataclass
class _PingResult:
    sent: int = 4
    received: int = 4
    loss_pct: float = 0.0
    rtt_avg_ms: float | None = 21.0
    error: str | None = None


@dataclass
class _FakeDriver:
    """read_path serves the recorded netwatch fixture for the gw, an error for the dead one."""

    reads: list[str] = field(default_factory=list)
    pings: list[tuple[str, str]] = field(default_factory=list)

    def read_path(self, address: str, segments: tuple, *, timeout: float, max_records: int):
        assert segments == ("tool", "netwatch")  # the one sanctioned dash read
        self.reads.append(address)
        if address == "172.18.83.1":
            return _ReadResult(records=[], error="connect timeout")
        return _ReadResult(records=NETWATCH_ROWS)

    def ping(self, address: str, destination: str, *, count: int, timeout: float):
        self.pings.append((address, destination))
        return _PingResult()


def _config(tmp_path: Path):
    save(
        Inventory(
            scope=DiscoveryScope(seeds=["172.18.75.1"]),
            entries=[
                InventoryEntry(address="172.18.75.1", identity="tamarack-internet-gw"),
                InventoryEntry(address="172.18.83.1", identity="basswood-gw"),
            ],
        ),
        tmp_path / "inventory.json",
    )
    config = tmp_path / "config.toml"
    config.write_text(
        f'[logs]\ndb_path = "{tmp_path}/logs.db"\n'
        f'[inventory]\npath = "{tmp_path}/inventory.json"\n'
        '[routeros]\nusername = "obi"\npassword = "x"\n'
        '[discovery]\nseeds = ["172.18.75.1"]\n'
        f'[icmp]\ndb_path = "{tmp_path}/icmp.db"\ninclude_uisp_ptp = false\n'
        f'[dash]\nenabled = true\nstate_dir = "{tmp_path}/dash"\n'
        f'ping_router = "tamarack-internet-gw"\n'
    )
    config.chmod(0o600)
    return load_config(config)


def test_one_cycle_populates_the_board_and_persists(tmp_path: Path) -> None:
    config = _config(tmp_path)
    driver = _FakeDriver()
    board = dash.run_cycle(config, now_us=NOW_US, probe=lambda host: (True, 2100), driver=driver)

    # fleet: both inventory devices probed up with rtt.
    fleet = board.tile("fleet")
    assert fleet is not None and fleet.state in ("green", "yellow")
    assert {i.key for i in fleet.items} == {"tamarack-internet-gw", "basswood-gw"}

    # netwatch: the gw's fixture pairs render; the dead router is a stale item, not red.
    netwatch = board.tile("netwatch")
    assert netwatch is not None
    assert sorted(driver.reads) == ["172.18.75.1", "172.18.83.1"]
    keys = {i.key for i in netwatch.items}
    assert "tamarack-internet-gw|8.8.8.8" in keys
    assert "basswood-gw" in keys  # the poll-failed router's own stale item
    assert netwatch.state == "stale"  # one router unreadable taints the tile, honestly

    # ping: both refs pinged FROM the border router.
    ping = board.tile("ping")
    assert ping is not None and ping.state == "green"
    assert driver.pings == [("172.18.75.1", "8.8.8.8"), ("172.18.75.1", "1.1.1.1")]

    # internet: unconfigured is a choice (green-muted); pipeline: real feeds now (US4) — in
    # this isolated state dir every collector log reads never-run ⇒ yellow, honestly.
    assert board.tile("internet").state == "green"  # type: ignore[union-attr]
    pipeline = board.tile("pipeline")
    assert pipeline is not None and pipeline.state == "yellow"
    assert any(i.key == "dash" for i in pipeline.items)

    # Persistence: board.json + carried state landed atomically.
    assert config.dash is not None
    assert dash.load_board(config.dash.state_dir) == board
    carried = dash.load_carried_state(config.dash.state_dir)
    assert "tamarack-internet-gw|8.8.8.8" in carried["netwatch"]
    assert carried["ping"] == {"full_loss_cycles": 0}


def test_second_cycle_reads_the_carried_state(tmp_path: Path) -> None:
    config = _config(tmp_path)
    driver = _FakeDriver()
    dash.run_cycle(config, now_us=NOW_US, probe=lambda host: (True, 2100), driver=driver)
    board = dash.run_cycle(
        config, now_us=NOW_US + 60_000_000, probe=lambda host: (True, 2100), driver=driver
    )
    assert config.dash is not None
    carried = dash.load_carried_state(config.dash.state_dir)
    samples = carried["netwatch"]["tamarack-internet-gw|8.8.8.8"]["samples"]
    assert len(samples) == 2  # the loss window is accumulating across cycles
    assert board.tile("netwatch") is not None


def test_cycle_runs_the_pager_when_enabled(tmp_path: Path) -> None:
    # US3 wiring (T027): the pager rides the cycle behind [dash.pager] enabled + a deliver seam.
    save(
        Inventory(
            scope=DiscoveryScope(seeds=["172.18.75.1"]),
            entries=[InventoryEntry(address="172.18.75.1", identity="tamarack-internet-gw")],
        ),
        tmp_path / "inventory.json",
    )
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'[logs]\ndb_path = "{tmp_path}/logs.db"\n'
        f'[inventory]\npath = "{tmp_path}/inventory.json"\n'
        '[routeros]\nusername = "obi"\npassword = "x"\n'
        '[discovery]\nseeds = ["172.18.75.1"]\n'
        f'[icmp]\ndb_path = "{tmp_path}/icmp.db"\ninclude_uisp_ptp = false\n'
        f'[dash]\nenabled = true\nstate_dir = "{tmp_path}/dash"\n'
        f'ping_router = "tamarack-internet-gw"\n'
        "[dash.pager]\nenabled = true\n"
        '[slack]\nbot_token = "xoxb-test"\napp_token = "xapp-test"\nallowlist = ["U0FAKEDCE78"]\n'
    )
    config_path.chmod(0o600)
    config = load_config(config_path)
    sent: list[str] = []

    def deliver(event: dash.PagerEvent) -> tuple[bool, str | None]:
        sent.append(dash.format_slack_page(event))
        return True, None

    driver = _FakeDriver()
    # Cycle 1 (device up): seeds the pager baseline silently.
    dash.run_cycle(
        config,
        now_us=NOW_US,
        probe=lambda host: (True, 2100),
        driver=driver,
        pager_deliver=deliver,
    )
    assert sent == []
    # Cycle 2 (device down): exactly one page.
    dash.run_cycle(
        config,
        now_us=NOW_US + 60_000_000,
        probe=lambda host: (False, None),
        driver=driver,
        pager_deliver=deliver,
    )
    assert len(sent) == 1
    assert sent[0].startswith("🔴 dash: host: tamarack-internet-gw")
    # Disabled pager (no deliver seam) ⇒ pure board-only: state file untouched by this call.
    assert (tmp_path / "dash" / dash.PAGER_STATE_FILE).exists()


def test_netwatch_workers_see_the_audit_scope(tmp_path: Path) -> None:
    # Contextvars are per-thread: without explicit re-entry the fan-out workers would record
    # nothing (found live: 2 audited events instead of 27). Pin that the cycle's ambient scope
    # is visible inside every worker's read_path call.
    from obi.core.audit import audit_scope, current_scope

    config = _config(tmp_path)
    seen: list[bool] = []

    class _ScopeProbeDriver(_FakeDriver):
        def read_path(self, address, segments, *, timeout, max_records):  # type: ignore[override]
            seen.append(current_scope() is not None)
            return super().read_path(address, segments, timeout=timeout, max_records=max_records)

    class _Recorder:  # the smallest AuditRecorder stand-in a scope will hold
        def record(self, *args: object, **kwargs: object) -> None:  # pragma: no cover - unused
            pass

    with audit_scope(_Recorder(), "cli:dash-run", "cli-dash-run-test"):  # type: ignore[arg-type]
        dash.run_cycle(
            config, now_us=NOW_US, probe=lambda host: (True, 2100), driver=_ScopeProbeDriver()
        )
    assert seen and all(seen)


def test_cycle_without_routeros_degrades_those_tiles_only(tmp_path: Path) -> None:
    _config(tmp_path)  # writes the inventory the credential-less config below reuses
    # A config without [routeros]: netwatch/internet must degrade to stale-with-note while the
    # fleet sweep (host-side, credential-free) still runs.
    config_path = tmp_path / "config2.toml"
    config_path.write_text(
        f'[logs]\ndb_path = "{tmp_path}/logs.db"\n'
        f'[inventory]\npath = "{tmp_path}/inventory.json"\n'
        f'[icmp]\ndb_path = "{tmp_path}/icmp.db"\ninclude_uisp_ptp = false\n'
        f'[dash]\nenabled = true\nstate_dir = "{tmp_path}/dash2"\n'
    )
    config_path.chmod(0o600)
    config = load_config(config_path)
    board = dash.run_cycle(config, now_us=NOW_US, probe=lambda host: (True, 2100))
    assert board.tile("fleet").state in ("green", "yellow")  # type: ignore[union-attr]
    assert board.tile("netwatch").state == "stale"  # type: ignore[union-attr]
    assert board.tile("ping").state == "stale"  # type: ignore[union-attr]
    assert any("no [routeros]" in note for note in board.notes)


def test_cycle_correlates_link_symptoms_into_one_incident(tmp_path: Path) -> None:
    # Feature 106 end-to-end: the cycle derives known sites from the inventory (+extra_sites),
    # and one link failure observed by both the fleet probe and a netwatch pair reaches the
    # deliver seam as ONE incident event, not two pages.
    save(
        Inventory(
            scope=DiscoveryScope(seeds=["172.18.75.1"]),
            entries=[
                InventoryEntry(address="172.18.75.1", identity="noc-gw"),
                InventoryEntry(address="172.18.55.9", identity="noc-ptp-magnolia-60"),
            ],
        ),
        tmp_path / "inventory.json",
    )
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'[logs]\ndb_path = "{tmp_path}/logs.db"\n'
        f'[inventory]\npath = "{tmp_path}/inventory.json"\n'
        '[routeros]\nusername = "obi"\npassword = "x"\n'
        '[discovery]\nseeds = ["172.18.75.1"]\n'
        f'[icmp]\ndb_path = "{tmp_path}/icmp.db"\ninclude_uisp_ptp = false\n'
        f'[dash]\nenabled = true\nstate_dir = "{tmp_path}/dash"\n'
        f'ping_router = "tamarack-internet-gw"\n'
        '[dash.pager]\nenabled = true\nextra_sites = ["magnolia"]\n'  # no magnolia-gw in inventory
        '[slack]\nbot_token = "xoxb-test"\napp_token = "xapp-test"\nallowlist = ["U0FAKEDCE78"]\n'
    )
    config_path.chmod(0o600)
    config = load_config(config_path)

    class _LinkDriver(_FakeDriver):
        """Scriptable netwatch: one link-named pair on noc-gw, up or down per cycle."""

        status = "up"

        def read_path(self, address, segments, *, timeout, max_records):  # type: ignore[override]
            self.reads.append(address)
            if address != "172.18.75.1":
                return _ReadResult(records=[])
            return _ReadResult(
                records=[{"host": "10.9.9.9", "name": "noc-magnolia-60", "status": self.status}]
            )

    driver = _LinkDriver()
    events: list[dash.PagerEvent] = []

    def deliver(event: dash.PagerEvent) -> tuple[bool, str | None]:
        events.append(event)
        return True, None

    # Cycle 1: everything up — seeds the pager baseline.
    dash.run_cycle(
        config,
        now_us=NOW_US,
        probe=lambda host: (True, 2100),
        driver=driver,
        pager_deliver=deliver,
    )
    assert events == []
    # Cycle 2: the radio stops answering ICMP AND the netwatch pair reports down — one event.
    driver.status = "down"
    dash.run_cycle(
        config,
        now_us=NOW_US + 60_000_000,
        probe=lambda host: (host != "172.18.55.9", 2100 if host != "172.18.55.9" else None),
        driver=driver,
        pager_deliver=deliver,
    )
    assert len(events) == 1
    assert events[0].scope == "incident" and events[0].sites == ("magnolia", "noc")
    assert {m.signal for m in events[0].members} == {"fleet", "netwatch"}
    assert dash.format_slack_page(events[0]).startswith("🔴 dash: link magnolia↔noc (60) — 2 red: ")


# --- 135: the cycle builds ONE matcher (config ∪ UISP flags), summarises it, audits changes -------


class _Events:
    """A recorder that keeps (kind, summary, target, payload) for every audited event."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, str | None, dict]] = []

    def record(self, kind: str, **kwargs: object) -> None:
        self.events.append(
            (kind, str(kwargs["summary"]), kwargs.get("target"), dict(kwargs["payload"]))  # type: ignore[arg-type]
        )

    def summaries(self) -> list[str]:
        return [summary for _, summary, _, _ in self.events]


def _maintenance_config(tmp_path: Path, body: str):
    base = _config(tmp_path)  # writes the inventory
    assert base.dash is not None
    path = tmp_path / "config.toml"
    path.write_text(path.read_text() + body)
    path.chmod(0o600)
    return load_config(path)


def _write_uisp(tmp_path: Path, devices: list[dict], sites: list[str]) -> None:
    web = tmp_path / "xdg-state" / "obi" / "ar"
    (web / "snapshots").mkdir(parents=True, exist_ok=True)
    (web / "snapshots" / "uisp.json").write_text(json.dumps({"data": {"devices": devices}}))
    (web / "links.json").write_text(json.dumps({"sites": [{"name": s} for s in sites]}))


def _run(config, recorder: _Events, *, now_us: int = NOW_US, probe=None):
    from obi.core.audit import audit_scope

    with audit_scope(recorder, "cli:dash-run", "t"):  # type: ignore[arg-type]
        return dash.run_cycle(
            config,
            now_us=now_us,
            probe=probe or (lambda host: (False, None)),
            driver=_FakeDriver(),
        )


def test_cycle_applies_config_maintenance_and_summarises(tmp_path: Path) -> None:
    _write_uisp(
        tmp_path,
        [{"identity": "tamarack-ptp-manzanita-11", "address": "172.18.89.1", "maintenance": True}],
        ["BRC - Business Park", "Tamarack"],
    )
    config = _maintenance_config(
        tmp_path,
        '[maintenance]\nsites = ["basswood"]\nsignals = ["mesh"]\nreason = "teardown"\n'
        'uisp_sites = ["BRC - Business Park"]\n',
    )
    recorder = _Events()
    board = _run(config, recorder)
    fleet = board.tile("fleet")
    assert fleet is not None
    basswood = next(i for i in fleet.items if i.key == "basswood-gw")
    assert basswood.maintenance == "config: teardown"
    # the poll-failed basswood-gw netwatch row is maintenance, not stale
    netwatch = board.tile("netwatch")
    assert netwatch is not None and netwatch.state != "stale"
    summary = board.maintenance
    assert summary is not None
    assert summary.sites == ["basswood"] and summary.signals == ["mesh"]
    assert summary.uisp_flagged == 1 and summary.unmatched == []
    assert summary.items >= 2
    assert "maintenance set changed" in recorder.summaries()  # first observation
    assert dash.load_board(config.dash.state_dir).maintenance == summary  # type: ignore[union-attr]


def test_cycle_flags_typos_and_audits_only_changes(tmp_path: Path) -> None:
    _write_uisp(tmp_path, [], ["BRC - Business Park"])
    config = _maintenance_config(
        tmp_path, '[maintenance]\nsites = ["mountian"]\nuisp_sites = ["BRC - Busness Park"]\n'
    )
    recorder = _Events()
    board = _run(config, recorder)
    assert board.maintenance is not None
    assert board.maintenance.unmatched == ["site mountian", "UISP site brc - busness park"]
    assert recorder.summaries().count("maintenance set changed") == 1
    _run(config, recorder, now_us=NOW_US + 60_000_000)
    assert recorder.summaries().count("maintenance set changed") == 1  # unchanged ⇒ silent


def test_cycle_without_maintenance_has_no_summary_and_no_audit(tmp_path: Path) -> None:
    config = _config(tmp_path)
    recorder = _Events()
    board = _run(config, recorder)
    assert board.maintenance is None
    assert "maintenance set changed" not in recorder.summaries()


def test_cycle_audits_pager_retirement(tmp_path: Path) -> None:
    from dataclasses import replace

    from obi.core.config import DashPagerSettings

    _write_uisp(tmp_path, [], [])
    config = _config(tmp_path)
    assert config.dash is not None
    config.dash = replace(config.dash, pager=DashPagerSettings(enabled=True))
    sent: list[object] = []

    def deliver(event: object) -> tuple[bool, str | None]:
        sent.append(event)
        return True, None

    from obi.core.audit import audit_scope

    def cycle(cfg, now_us: int, up: bool, recorder: _Events):
        with audit_scope(recorder, "cli:dash-run", "t"):  # type: ignore[arg-type]
            return dash.run_cycle(
                cfg,
                now_us=now_us,
                probe=lambda host: (up, 2100 if up else None),
                driver=_FakeDriver(),
                pager_deliver=deliver,
            )

    recorder = _Events()
    cycle(config, NOW_US, True, recorder)  # seed
    for n in range(1, 4):  # down long enough to pass the icmp debounce and page
        cycle(config, NOW_US + n * 60_000_000, False, recorder)
    assert sent, "expected a page for the down fleet"
    pages_before = len(sent)
    path = tmp_path / "config.toml"
    path.write_text(path.read_text() + '[maintenance]\nsites = ["basswood", "tamarack"]\n')
    config2 = load_config(path)
    assert config2.dash is not None
    config2.dash = replace(config2.dash, pager=DashPagerSettings(enabled=True))
    cycle(config2, NOW_US + 5 * 60_000_000, False, recorder)
    assert len(sent) == pages_before  # no recovery page
    retired = [t for k, s, t, _ in recorder.events if s.startswith("dash pager: incident retired")]
    assert retired
