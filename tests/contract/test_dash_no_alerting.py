"""Contract (feature 121): the MESH and LIGHTING signals are display-only.

Full contract: specs/121-mesh-hardening/contracts/dash-signals.md.

The operator asked for these two tiles with no alerting, and "no alerting" has to be a
property rather than a default. The trap this file exists to hold shut: ``[dash.pager]
signals`` used to default to ``list(_DASH_SIGNALS)``, so simply appending the two new signals
to that tuple would have made both page on their first red — silently, on the day the mesh
radio was unplugged. The pageable set is therefore a separate, smaller tuple, and naming a
display-only signal in the pager config is a load-time error rather than a way to opt in.

The second property is that reading the mesh for a tile must never key the LoRa radio. The
dash cycle runs every minute; a status tile that transmits would be a standing airtime cost
and would collide with the durable rate limiter that guards the answer path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from obi.core import dash
from obi.core.config import (
    _DASH_PAGEABLE_SIGNALS,
    _DASH_SIGNALS,
    ConfigError,
    DashPagerSettings,
    DashSettings,
    load_config,
)
from obi.core.dash import Board, Item, Tile, run_pager

NOW_US = 1_787_000_000_000_000

DISPLAY_ONLY = ("mesh", "lighting")


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(f'[logs]\ndb_path = "{tmp_path}/logs.db"\n\n{body}')
    path.chmod(0o600)
    return path


class _Deliver:
    def __init__(self) -> None:
        self.events: list[dash.PagerEvent] = []

    def __call__(self, event: dash.PagerEvent) -> tuple[bool, str | None]:
        self.events.append(event)
        return True, None


class _FakeConnection:
    """An empty node table — enough to prove the collector reads without transmitting."""

    def read_nodes(self) -> tuple[str, dict[str, dict[str, object]]]:
        return "!f4dcd459", {}

    def close(self) -> None:
        pass


# --- E1/E2: not pageable, and not opt-in-able ---------------------------------------------


def test_display_only_signals_are_not_in_the_pageable_set() -> None:
    for signal in DISPLAY_ONLY:
        assert signal in _DASH_SIGNALS, f"{signal} should still render on the board"
        assert signal not in _DASH_PAGEABLE_SIGNALS, f"{signal} must never page"


def test_pager_defaults_to_the_pageable_set_not_every_signal() -> None:
    """The exact trap: a default of list(_DASH_SIGNALS) would page on mesh/lighting reds."""

    assert DashPagerSettings().signals == list(_DASH_PAGEABLE_SIGNALS)
    for signal in DISPLAY_ONLY:
        assert signal not in DashPagerSettings().signals


@pytest.mark.parametrize("signal", DISPLAY_ONLY)
def test_config_rejects_paging_on_a_display_only_signal(tmp_path: Path, signal: str) -> None:
    """Rejected, not ignored — an operator who tries this is told, not quietly overruled."""

    with pytest.raises(ConfigError, match=signal):
        load_config(_write(tmp_path, f'[dash]\n[dash.pager]\nsignals = ["{signal}"]\n'))


def test_config_still_rejects_a_genuinely_unknown_signal(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="unknown signal"):
        load_config(_write(tmp_path, '[dash]\n[dash.pager]\nsignals = ["nonsense"]\n'))


# --- E3: no events, in any state ----------------------------------------------------------


def _red_board() -> Board:
    """A board where BOTH display-only tiles are red, plus a red pageable tile as a control.

    The control matters: it proves the pager is working in this test, so a green result for
    mesh/lighting is a real exclusion rather than a silently broken harness.
    """

    return Board(
        generated_at_us=NOW_US,
        state="red",
        tiles=[
            Tile(
                signal="fleet",
                state="red",
                items=[Item(key="core-gw-1", label="core-gw-1", state="red", detail="down")],
                cadence_s=60,
            ),
            Tile(
                signal="mesh",
                state="red",
                items=[
                    Item(key="radio", label="mesh radio", state="red", detail="not reachable"),
                    Item(key="!91378e3a", label="Tooma", state="red", detail="quiet 13h"),
                ],
                cadence_s=60,
            ),
            Tile(
                signal="lighting",
                state="red",
                items=[Item(key="portal", label="portal", state="red", detail="unreachable")],
                cadence_s=60,
            ),
        ],
    )


def _settings(tmp_path: Path, **pager_kwargs: object) -> DashSettings:
    return DashSettings(
        state_dir=tmp_path / "dash",
        pager=DashPagerSettings(enabled=True, **pager_kwargs),  # type: ignore[arg-type]
    )


def test_red_display_only_tiles_never_page(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    deliver = _Deliver()
    # First pass seeds the baseline silently; the transition would page on the second.
    run_pager(_red_board(), settings, now_us=NOW_US, deliver=deliver)
    deliver.events.clear()
    run_pager(_red_board(), settings, now_us=NOW_US + 60_000_000, deliver=deliver)
    paged_signals = {event_signal for event in deliver.events for event_signal in _signals(event)}
    assert not (paged_signals & set(DISPLAY_ONLY)), (
        f"a display-only signal paged: {sorted(paged_signals & set(DISPLAY_ONLY))}"
    )


def test_the_control_signal_does_page(tmp_path: Path) -> None:
    """Proves the harness above can observe a page at all."""

    settings = _settings(tmp_path)
    deliver = _Deliver()
    green = Board(generated_at_us=NOW_US, state="green", tiles=[])
    run_pager(green, settings, now_us=NOW_US, deliver=deliver)
    run_pager(_red_board(), settings, now_us=NOW_US + 60_000_000, deliver=deliver)
    paged = {s for event in deliver.events for s in _signals(event)}
    assert "fleet" in paged


def _signals(event: dash.PagerEvent) -> set[str]:
    """Every signal cited by a pager event, however it was grouped (mass, incident, single)."""

    found: set[str] = set()
    for attr in ("signal", "members", "items"):
        value = getattr(event, attr, None)
        if isinstance(value, str):
            found.add(value)
        elif isinstance(value, (list, tuple, set)):
            for member in value:
                member_signal = getattr(member, "signal", None)
                if isinstance(member_signal, str):
                    found.add(member_signal)
    # Last resort: scan the rendered text, so a future grouping shape cannot hide a page.
    rendered = dash.format_slack_page(event).lower()
    for signal in DISPLAY_ONLY:
        if signal in rendered:
            found.add(signal)
    return found


def test_mass_coalesce_path_also_excludes_them(tmp_path: Path) -> None:
    """The per-signal mass path is a separate branch — it must exclude them too."""

    settings = _settings(tmp_path, mass_ratio=0.5)
    deliver = _Deliver()
    run_pager(_red_board(), settings, now_us=NOW_US, deliver=deliver)
    deliver.events.clear()
    run_pager(_red_board(), settings, now_us=NOW_US + 60_000_000, deliver=deliver)
    paged = {s for event in deliver.events for s in _signals(event)}
    assert not (paged & set(DISPLAY_ONLY))


# --- T1/T2/T3: the mesh tile never transmits ----------------------------------------------


def test_mesh_collector_cannot_transmit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Contract T1/T2, mirroring the record_pass non-transmission test.

    The collector is driven with a driver whose request_position raises. If any future edit
    reaches for a fresh position to make the tile prettier, this fails loudly.
    """

    from obi.core.drivers.meshtastic import MeshDriver

    config = load_config(
        _write(
            tmp_path,
            "[dash]\n\n[meshtastic]\n"
            f'positions_db_path = "{tmp_path}/positions.db"\n'
            'report_only = ["!91378e3a"]\n',
        )
    )
    assert config.meshtastic is not None

    def exploding_request(*args: object, **kwargs: object) -> object:
        raise AssertionError("the dash mesh collector transmitted a position request")

    # A fully faked driver: discovery and the node read are scripted, and the transmitting
    # path raises. Nothing here can reach real hardware — an earlier draft of this test built
    # the driver internally and opened the operator's actual radio.
    driver = MeshDriver(
        config.meshtastic,
        connection_factory=lambda port, timeout: _FakeConnection(),
        find_ports=lambda: ["/dev/cu.usbmodem-fake"],
    )
    monkeypatch.setattr(driver, "request_position", exploding_request)

    notes: list[str] = []
    health = dash.collect_mesh(config, notes, now_s=NOW_US / 1_000_000, driver=driver)
    assert health is not None
    assert health.reachable is True
    # The allowlisted node is absent from the faked table — it must still appear, or the tile
    # would go green by forgetting the node it exists to watch (the 117 eviction case).
    assert [node.node_id for node in health.nodes] == ["!91378e3a"]
    assert health.nodes[0].last_heard_age_s is None


def test_mesh_collector_absent_when_unconfigured(tmp_path: Path) -> None:
    config = load_config(_write(tmp_path, "[dash]\n"))
    assert dash.collect_mesh(config, [], now_s=1.0) is None


def test_the_tile_never_touches_the_serial_port(tmp_path: Path) -> None:
    """The mesh port is exclusive-access — and since feature 123, the tile's answer.

    Pre-123 this contract was a 5-minute serial-read throttle (a per-minute open would
    have cost real `mesh locate` answers PORT_BUSY a few percent of the time). The daemon
    makes the stronger guarantee cheap: the collector's default access is the daemon
    CLIENT, so the tile never opens the port at all — with the daemon down it degrades in
    bounded time to a red item that names the DAEMON, never a serial open (contract O1;
    "radio silent" ≠ "service down", FR-012).
    """

    import time as _time

    from obi.core.meshclient import MeshDaemonClient

    config = load_config(
        _write(
            tmp_path,
            f'[dash]\nstate_dir = "{tmp_path}/dash"\n\n'
            f'[meshtastic]\npositions_db_path = "{tmp_path}/positions.db"\n'
            f'daemon_socket = "{tmp_path}/no-daemon.sock"\n'
            'report_only = ["!91378e3a"]\n',
        )
    )
    assert config.meshtastic is not None
    (tmp_path / "dash").mkdir(parents=True, exist_ok=True)

    from obi.core.mesh import mesh_access

    assert isinstance(mesh_access(config.meshtastic), MeshDaemonClient)

    start = _time.monotonic()
    health = dash.collect_mesh(config, [], now_s=1_787_000_000.0)
    elapsed = _time.monotonic() - start
    assert health is not None
    assert health.reachable is False
    assert "daemon" in health.detail
    assert elapsed < 3.0  # bounded degradation, not a hang (O4/O8)


def test_a_failing_collector_never_fails_the_cycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Contract B2. These are status tiles; a bug in one must not cost the whole board.

    The dash cycle is the operator's primary view during an incident — it is the last thing
    that should break because a decorative-lighting HTTP read raised.
    """

    config = load_config(
        _write(
            tmp_path,
            f'[dash]\nenabled = true\nstate_dir = "{tmp_path}/dash"\n\n'
            f'[icmp]\ndb_path = "{tmp_path}/icmp.db"\n',
        )
    )

    def boom(*args: object, **kwargs: object) -> object:
        raise RuntimeError("collector exploded")

    monkeypatch.setattr(dash, "collect_mesh", boom)
    monkeypatch.setattr(dash, "collect_lighting", boom)

    board = dash.run_cycle(config, now_us=NOW_US, probe=lambda host: (True, 2100))
    assert board is not None
    # The five always-on signals still rendered, and the failure is on the board as a note.
    assert {t.signal for t in board.tiles} >= {"fleet", "netwatch", "ping", "internet", "pipeline"}
    assert any("collector exploded" in note for note in board.notes)


def test_a_failed_read_is_not_cached_so_recovery_is_immediate(tmp_path: Path) -> None:
    """Field-caught 2026-09-01, an hour after this shipped.

    The RAK was replugged and the mesh tile sat red on a 4-minute-old ENOENT, because the
    failure had been cached with the same 5-minute TTL as a success. Throttling exists to
    stop us holding the exclusive serial port every minute — a FAILED read never holds it, so
    caching one buys nothing and only delays recovery.
    """

    from obi.core.drivers.meshtastic import MeshDriver, MeshSnapshot

    config = load_config(
        _write(
            tmp_path,
            f'[dash]\nstate_dir = "{tmp_path}/dash"\n\n[dash.mesh]\nread_interval = "5m"\n\n'
            f'[meshtastic]\npositions_db_path = "{tmp_path}/positions.db"\n'
            'report_only = ["!91378e3a"]\n',
        )
    )
    assert config.meshtastic is not None
    (tmp_path / "dash").mkdir(parents=True, exist_ok=True)
    base = 1_787_000_000.0

    # Cycle 1: no radio (the unplugged state).
    absent = MeshDriver(config.meshtastic, find_ports=lambda: [])
    first = dash.collect_mesh(config, [], now_s=base, driver=absent)
    assert first is not None and first.reachable is False

    # Cycle 2, one minute later and well inside read_interval: the radio is back. The tile
    # must reflect that NOW, not in four more minutes.
    present = MeshDriver(config.meshtastic, find_ports=lambda: ["/dev/cu.fake"])
    present.snapshot = lambda port, **kw: MeshSnapshot(  # type: ignore[method-assign]
        port="/dev/cu.fake", my_node_id="!f4dcd459", nodes=()
    )
    second = dash.collect_mesh(config, [], now_s=base + 60, driver=present)
    assert second is not None
    assert second.reachable is True


def test_cached_node_ages_advance_between_serial_reads(tmp_path: Path) -> None:
    """Caching an AGE would freeze it; the cache stores absolute instants and re-ages."""

    from obi.core.drivers.meshtastic import MeshDriver, MeshNode, MeshSnapshot

    config = load_config(
        _write(
            tmp_path,
            f'[dash]\nstate_dir = "{tmp_path}/dash"\n\n[dash.mesh]\nread_interval = "5m"\n\n'
            f'[meshtastic]\npositions_db_path = "{tmp_path}/positions.db"\n'
            'report_only = ["!91378e3a"]\n',
        )
    )
    assert config.meshtastic is not None
    (tmp_path / "dash").mkdir(parents=True, exist_ok=True)
    base = 1_787_000_000.0

    driver = MeshDriver(config.meshtastic, find_ports=lambda: ["/dev/cu.fake"])
    node = MeshNode(
        node_id="!91378e3a", short_name="TUMA", long_name="Tooma 9831", last_heard=int(base) - 60
    )
    driver.snapshot = lambda port, **kw: MeshSnapshot(  # type: ignore[method-assign]
        port="/dev/cu.fake", my_node_id="!f4dcd459", nodes=(node,)
    )

    first = dash.collect_mesh(config, [], now_s=base, driver=driver)
    assert first is not None and first.nodes[0].last_heard_age_s == 60

    # Four minutes later, from cache: the node has been quiet four minutes longer.
    later = dash.collect_mesh(config, [], now_s=base + 240, driver=driver)
    assert later is not None
    assert later.nodes[0].last_heard_age_s == 300


def test_dash_cycle_leaves_the_transmission_limiter_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Contract T3: a cycle must not consume any of the mesh airtime budget."""

    state_dir = tmp_path / "mesh-state"
    state_dir.mkdir()
    limiter = state_dir / "ratelimit.json"
    limiter.write_text(json.dumps({"per_node": {"!91378e3a": 1.0}, "hour_window": [1.0]}))
    before = limiter.read_bytes()

    from obi.core.drivers.meshtastic import MeshDriver

    config = load_config(
        _write(
            tmp_path,
            "[dash]\n\n[meshtastic]\n"
            f'positions_db_path = "{tmp_path}/positions.db"\n'
            'report_only = ["!91378e3a"]\n',
        )
    )
    assert config.meshtastic is not None
    driver = MeshDriver(
        config.meshtastic,
        connection_factory=lambda port, timeout: _FakeConnection(),
        find_ports=lambda: ["/dev/cu.usbmodem-fake"],
    )
    dash.collect_mesh(config, [], now_s=NOW_US / 1_000_000, driver=driver)
    assert limiter.read_bytes() == before
