"""Unit (feature 072): the host-originated ICMP prober + collector (``icmp``).

Covers target resolution (present inventory + Siklu, ignore, dedup, include_siklu), the
exit-code→bool mapping of ``probe_host`` over a stubbed subprocess runner (never a real ping), and
the debounced up/down state machine folded into the store via ``run_collect`` with an injected
probe function.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from obi.core import icmp, icmpstore
from obi.core.config import IcmpSettings
from obi.core.icmp import Target, resolve_targets, run_collect
from obi.core.inventory import Inventory, InventoryEntry
from obi.core.models import DiscoveryScope

NOW = 1_000_000_000


def _inv(entries: list[InventoryEntry]) -> Inventory:
    return Inventory(scope=DiscoveryScope(seeds=["172.25.110.1"]), entries=entries)


# --- resolve_targets --------------------------------------------------------------------------


def test_resolve_includes_present_inventory_and_siklu() -> None:
    inv = _inv(
        [
            InventoryEntry(address="172.18.84.1", identity="core-gw-1"),
            InventoryEntry(address="172.18.81.20", identity="chestnut-sw"),
        ]
    )
    siklu = "basswood-ptp-tamarack-70.net.example.net"
    targets = resolve_targets(inv, [siklu], ignore=[], include_siklu=True)
    assert Target(device="core-gw-1", address="172.18.84.1") in targets
    assert Target(device="chestnut-sw", address="172.18.81.20") in targets
    # Siklu device name is the SHORT identity (domain stripped) to match everywhere else;
    # the full DNS name stays the ping address.
    assert Target(device="basswood-ptp-tamarack-70", address=siklu) in targets
    assert len(targets) == 3


def test_resolve_drops_absent_from_latest() -> None:
    inv = _inv(
        [
            InventoryEntry(address="172.18.84.1", identity="core-gw-1"),
            InventoryEntry(address="172.18.93.9", identity="gone-gw", present_in_latest=False),
        ]
    )
    targets = resolve_targets(inv, [], ignore=[], include_siklu=True)
    assert [t.device for t in targets] == ["core-gw-1"]


def test_resolve_honors_ignore_substring() -> None:
    inv = _inv(
        [
            InventoryEntry(address="172.18.84.1", identity="core-gw-1"),
            InventoryEntry(address="172.18.117.1", identity="brc-gw"),
            InventoryEntry(address="172.18.117.21", identity="brc-sw-tower-1"),
        ]
    )
    targets = resolve_targets(inv, [], ignore=["brc-"], include_siklu=True)
    assert [t.device for t in targets] == ["core-gw-1"]


def test_resolve_ignore_matches_address() -> None:
    inv = _inv([InventoryEntry(address="172.18.84.1", identity="core-gw-1")])
    assert resolve_targets(inv, [], ignore=["172.18.84."], include_siklu=True) == []


def test_resolve_excludes_siklu_when_disabled() -> None:
    inv = _inv([InventoryEntry(address="172.18.84.1", identity="core-gw-1")])
    targets = resolve_targets(inv, ["siklu-a.net"], ignore=[], include_siklu=False)
    assert [t.device for t in targets] == ["core-gw-1"]


def test_resolve_dedupes_by_address() -> None:
    inv = _inv(
        [
            InventoryEntry(address="172.18.84.1", identity="core-gw-1"),
            InventoryEntry(address="172.18.84.1", identity="core-gw-1-dup"),
        ]
    )
    targets = resolve_targets(inv, [], ignore=[], include_siklu=True)
    assert len(targets) == 1


def test_resolve_no_inventory_is_siklu_only() -> None:
    targets = resolve_targets(
        None, ["tamarack-ptp-basswood-70.net.example.net"], ignore=[], include_siklu=True
    )
    assert [t.device for t in targets] == ["tamarack-ptp-basswood-70"]  # short name
    assert [t.address for t in targets] == [  # full DNS name stays the ping address
        "tamarack-ptp-basswood-70.net.example.net"
    ]


def test_short_host_name_strips_domain_keeps_ip() -> None:
    assert icmp._short_host_name("basswood-ptp-tamarack-70.net.example.net") == (
        "basswood-ptp-tamarack-70"
    )
    assert icmp._short_host_name("core-vms-1.net.example.net") == "core-vms-1"  # Linux VM
    assert icmp._short_host_name("radio1") == "radio1"  # already short
    assert icmp._short_host_name("172.18.83.224") == "172.18.83.224"  # IPv4 literal kept whole
    assert icmp._short_host_name("fd00::1") == "fd00::1"  # IPv6 literal kept whole


def test_resolve_includes_extra_hosts() -> None:
    inv = _inv([InventoryEntry(address="172.18.84.1", identity="core-gw-1")])
    targets = resolve_targets(
        inv,
        [],
        ignore=[],
        include_siklu=True,
        extra_hosts=["core-vms-1.net.example.net", "chestnut-vms-1.net.example.net"],
    )
    # short name (domain stripped) as identity; full DNS name as the ping address
    assert Target(device="core-vms-1", address="core-vms-1.net.example.net") in targets
    assert Target(device="chestnut-vms-1", address="chestnut-vms-1.net.example.net") in targets
    assert {t.device for t in targets} == {"core-gw-1", "core-vms-1", "chestnut-vms-1"}


def test_resolve_extra_hosts_honor_ignore_and_dedupe() -> None:
    # ignore by short name excludes; an extra_host duplicating an inventory address is deduped
    inv = _inv([InventoryEntry(address="172.18.84.1", identity="core-gw-1")])
    extra = [
        "core-vms-1.net.example.net",
        "chestnut-vms-1.net.example.net",
        "172.18.84.1",  # duplicates the inventory address
    ]
    targets = resolve_targets(
        inv, [], ignore=["chestnut-vms"], include_siklu=True, extra_hosts=extra
    )
    devices = {t.device for t in targets}
    assert "chestnut-vms-1" not in devices  # ignored
    assert "core-vms-1" in devices
    assert (
        sum(1 for t in targets if t.address == "172.18.84.1") == 1
    )  # deduped vs the inventory row


def test_resolve_siklu_ignore_matches_short_name_or_address() -> None:
    siklu = "basswood-ptp-tamarack-70.net.example.net"
    # ignore by the short name
    assert resolve_targets(None, [siklu], ignore=["basswood-ptp"], include_siklu=True) == []
    # ignore by a domain substring of the address still works
    assert resolve_targets(None, [siklu], ignore=["example.net"], include_siklu=True) == []


def test_resolve_falls_back_to_address_when_identity_blank() -> None:
    inv = _inv([InventoryEntry(address="172.18.83.1", identity="")])
    targets = resolve_targets(inv, [], ignore=[], include_siklu=True)
    assert targets == [Target(device="172.18.83.1", address="172.18.83.1")]


# --- the UISP backhaul roster (feature 095): snapshot-derived, cache-held ----------------------


def _write_snapshot(path, devices) -> None:
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"tool": "uisp", "data": {"devices": devices}}))


def _uisp_settings(tmp_path) -> IcmpSettings:
    return IcmpSettings(db_path=tmp_path / "icmp.db")


def test_uisp_ptp_targets_filter_and_cache(tmp_path) -> None:
    snapshot = tmp_path / "snapshots" / "uisp.json"
    _write_snapshot(
        snapshot,
        [
            {"identity": "yew-ptp-tamarack-60", "address": "172.18.84.129"},
            {"identity": "noc-ap-1", "address": "172.18.119.21"},  # not a -ptp- backhaul name
            {"identity": "aspen-ptp-somewhere-60", "address": "172.18.93.9"},  # ignore_names
            {"identity": "no-address-ptp-60"},  # unpingable — dropped
        ],
    )
    settings = _uisp_settings(tmp_path)
    targets, maintenance, note = icmp.load_uisp_ptp_targets(
        settings, snapshot_path=snapshot, ignore_names=["aspen"]
    )
    assert note is None
    assert targets == [Target(device="yew-ptp-tamarack-60", address="172.18.84.129")]
    assert maintenance == set()
    assert (tmp_path / "icmp-uisp-targets.json").exists()  # the U1 cache landed beside icmp.db


def test_uisp_ptp_snapshot_gap_reuses_the_cache(tmp_path) -> None:
    # Analysis finding U1: a snapshot-cron hiccup must never shrink the probe roster (a pruned
    # radio would re-seed its down-debounce and flap in and out of the board).
    snapshot = tmp_path / "snapshots" / "uisp.json"
    _write_snapshot(
        snapshot,
        [{"identity": "yew-ptp-tamarack-60", "address": "172.18.84.129", "maintenance": True}],
    )
    settings = _uisp_settings(tmp_path)
    icmp.load_uisp_ptp_targets(settings, snapshot_path=snapshot)  # warm the cache
    snapshot.unlink()
    targets, maintenance, note = icmp.load_uisp_ptp_targets(settings, snapshot_path=snapshot)
    assert targets == [Target(device="yew-ptp-tamarack-60", address="172.18.84.129")]
    assert maintenance == {"yew-ptp-tamarack-60"}  # the flag rides the cache through gaps
    assert note is not None and "cached" in note


def test_uisp_ptp_no_snapshot_no_cache_is_empty_with_note(tmp_path) -> None:
    targets, maintenance, note = icmp.load_uisp_ptp_targets(
        _uisp_settings(tmp_path), snapshot_path=tmp_path / "missing.json"
    )
    assert targets == []
    assert maintenance == set()
    assert note is not None and "not probed" in note


def test_uisp_ptp_maintenance_flag_is_read_from_the_snapshot(tmp_path) -> None:
    snapshot = tmp_path / "snapshots" / "uisp.json"
    _write_snapshot(
        snapshot,
        [
            {"identity": "yew-ptp-tamarack-60", "address": "172.18.84.129", "maintenance": True},
            {"identity": "noc-ptp-basswood-60", "address": "172.18.119.60"},
        ],
    )
    targets, maintenance, note = icmp.load_uisp_ptp_targets(
        _uisp_settings(tmp_path), snapshot_path=snapshot
    )
    assert note is None
    assert {t.device for t in targets} == {"yew-ptp-tamarack-60", "noc-ptp-basswood-60"}
    assert maintenance == {"yew-ptp-tamarack-60"}


def test_resolve_targets_merges_uisp_and_dedupes_by_address() -> None:
    inv = _inv([InventoryEntry(address="172.18.84.1", identity="core-gw-1")])
    siklu = "tamarack-ptp-basswood-70.net.example.net"
    uisp = [
        Target(device="yew-ptp-tamarack-60", address="172.18.84.129"),
        Target(device="dup-of-core", address="172.18.84.1"),  # duplicates the inventory address
        Target(device="aspen-ptp-x-60", address="172.18.93.9"),
    ]
    targets = resolve_targets(
        inv, [siklu], ignore=["aspen"], include_siklu=True, uisp_targets=uisp
    )
    devices = {t.device for t in targets}
    assert "yew-ptp-tamarack-60" in devices
    assert "dup-of-core" not in devices  # address dedupe wins
    assert "aspen-ptp-x-60" not in devices  # the shared ignore list applies to uisp targets too
    assert "tamarack-ptp-basswood-70" in devices


# --- probe_host exit-code mapping (stubbed subprocess) -----------------------------------------


def _fake_run(returncode: int):
    def _run(cmd, **kwargs):  # noqa: ANN001, ANN003 - test stub
        return subprocess.CompletedProcess(cmd, returncode, b"", b"")

    return _run


def test_probe_host_uses_absolute_ping_path(monkeypatch) -> None:
    # Regression: cron's minimal PATH (/usr/bin:/bin) lacks /sbin, where macOS `ping` lives — so the
    # command MUST invoke ping by absolute path, else every probe from cron throws and reads down.
    captured: dict[str, list[str]] = {}

    def _run(cmd, **kwargs):  # noqa: ANN001, ANN003 - test stub
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(icmp.subprocess, "run", _run)
    icmp.probe_host("172.25.110.1", count=1, per_ping_timeout_ms=1000, deadline_s=5.0)
    ping_cmd = captured["cmd"][0]
    assert ping_cmd.startswith("/"), "ping must be absolute (cron PATH lacks /sbin)"
    assert ping_cmd.endswith("ping")


def test_ping_binary_falls_back_to_known_path_off_path(monkeypatch) -> None:
    # When `which ping` fails (cron PATH), fall back to a known absolute location.
    icmp._ping_binary.cache_clear()
    monkeypatch.setattr(icmp.shutil, "which", lambda name: None)
    monkeypatch.setattr(icmp.os.path, "exists", lambda p: p == "/sbin/ping")
    try:
        assert icmp._ping_binary() == "/sbin/ping"
    finally:
        icmp._ping_binary.cache_clear()  # don't leak the mocked value to other tests


def test_probe_host_up_on_exit_zero(monkeypatch) -> None:
    monkeypatch.setattr(icmp.subprocess, "run", _fake_run(0))
    result = icmp.probe_host("172.25.110.1", count=3, per_ping_timeout_ms=1000, deadline_s=5.0)
    assert result == (True, None)  # empty stdout → up, no measurable rtt


def test_probe_host_down_on_nonzero(monkeypatch) -> None:
    monkeypatch.setattr(icmp.subprocess, "run", _fake_run(2))
    result = icmp.probe_host("172.25.110.1", count=3, per_ping_timeout_ms=1000, deadline_s=5.0)
    assert result == (False, None)


def test_probe_host_down_on_timeout(monkeypatch) -> None:
    def _raise(cmd, **kwargs):  # noqa: ANN001, ANN003 - test stub
        raise subprocess.TimeoutExpired(cmd, 5.0)

    monkeypatch.setattr(icmp.subprocess, "run", _raise)
    result = icmp.probe_host("172.25.110.1", count=3, per_ping_timeout_ms=1000, deadline_s=5.0)
    assert result == (False, None)


def test_probe_host_down_on_oserror(monkeypatch) -> None:
    def _raise(cmd, **kwargs):  # noqa: ANN001, ANN003 - test stub
        raise OSError("ping not found")

    monkeypatch.setattr(icmp.subprocess, "run", _raise)
    result = icmp.probe_host("172.25.110.1", count=3, per_ping_timeout_ms=1000, deadline_s=5.0)
    assert result == (False, None)


# --- rtt parsing (feature 095): both ping dialects' summary lines, captured verbatim -----------

_MACOS_PING = (
    b"PING 172.18.89.1 (172.18.89.1): 56 data bytes\n"
    b"64 bytes from 172.18.89.1: icmp_seq=0 ttl=64 time=2.108 ms\n"
    b"\n--- 172.18.89.1 ping statistics ---\n"
    b"3 packets transmitted, 3 packets received, 0.0% packet loss\n"
    b"round-trip min/avg/max/stddev = 1.914/2.345/2.771/0.350 ms\n"
)

_IPUTILS_PING = (
    b"PING 172.18.89.1 (172.18.89.1) 56(84) bytes of data.\n"
    b"64 bytes from 172.18.89.1: icmp_seq=1 ttl=64 time=0.045 ms\n"
    b"\n--- 172.18.89.1 ping statistics ---\n"
    b"3 packets transmitted, 3 received, 0% packet loss, time 2003ms\n"
    b"rtt min/avg/max/mdev = 0.041/0.052/0.061/0.008 ms\n"
)


def test_probe_host_parses_macos_rtt(monkeypatch) -> None:
    def _run(cmd, **kwargs):  # noqa: ANN001, ANN003 - test stub
        return subprocess.CompletedProcess(cmd, 0, _MACOS_PING, b"")

    monkeypatch.setattr(icmp.subprocess, "run", _run)
    result = icmp.probe_host("172.18.89.1", count=3, per_ping_timeout_ms=1000, deadline_s=5.0)
    assert result == (True, 2345)  # 2.345 ms → µs


def test_probe_host_parses_iputils_rtt(monkeypatch) -> None:
    def _run(cmd, **kwargs):  # noqa: ANN001, ANN003 - test stub
        return subprocess.CompletedProcess(cmd, 0, _IPUTILS_PING, b"")

    monkeypatch.setattr(icmp.subprocess, "run", _run)
    result = icmp.probe_host("172.18.89.1", count=3, per_ping_timeout_ms=1000, deadline_s=5.0)
    assert result == (True, 52)  # 0.052 ms → µs


def test_probe_host_unparseable_rtt_never_affects_up(monkeypatch) -> None:
    def _run(cmd, **kwargs):  # noqa: ANN001, ANN003 - test stub
        return subprocess.CompletedProcess(cmd, 0, b"weird ping output\n", b"")

    monkeypatch.setattr(icmp.subprocess, "run", _run)
    result = icmp.probe_host("172.18.89.1", count=3, per_ping_timeout_ms=1000, deadline_s=5.0)
    assert result == (True, None)


# --- run_collect: the debounced state machine -------------------------------------------------


class _ScriptedProbe:
    """A probe stub: maps a host address to an iterator of per-cycle up/down results.

    A reachable cycle reports a fixed 2500 µs rtt (feature 095's tuple contract); a miss
    reports ``None``, as the real prober does.
    """

    def __init__(self, script: dict[str, list[bool]]) -> None:
        self._script = {host: iter(results) for host, results in script.items()}

    def __call__(self, host: str) -> tuple[bool, int | None]:
        up = next(self._script[host])
        return (up, 2500 if up else None)


def _collect(conn, settings: IcmpSettings, targets, probe, now_us: int):
    return run_collect(settings, targets, conn=conn, now_us=now_us, probe=probe)


def test_reply_records_up_and_last_seen(tmp_path: Path) -> None:
    conn = icmpstore.connect(tmp_path / "icmp.db")
    settings = IcmpSettings()
    targets = [Target(device="a", address="172.25.110.1")]
    probe = _ScriptedProbe({"172.25.110.1": [True]})
    summary = _collect(conn, settings, targets, probe, NOW)
    state = icmpstore.load_states(conn)[0]
    conn.close()
    assert summary.up == 1 and summary.down == 0
    assert state.up is True
    assert state.last_seen_us == NOW
    assert state.consecutive_misses == 0
    assert state.last_rtt_us == 2500  # the scripted probe's rtt landed in the v2 column


def test_single_miss_flips_down_at_threshold_one(tmp_path: Path) -> None:
    conn = icmpstore.connect(tmp_path / "icmp.db")
    settings = IcmpSettings()  # down_after_cycles = 1
    targets = [Target(device="a", address="172.25.110.1")]
    # cycle 1 up (seed), cycle 2 miss → down
    _collect(conn, settings, targets, _ScriptedProbe({"172.25.110.1": [True]}), NOW)
    summary = _collect(
        conn, settings, targets, _ScriptedProbe({"172.25.110.1": [False]}), NOW + 300
    )
    state = icmpstore.load_states(conn)[0]
    conn.close()
    assert summary.new_down == 1
    assert state.up is False
    assert state.consecutive_misses == 1
    assert state.last_change_us == NOW + 300  # flipped this cycle
    assert state.last_seen_us == NOW  # preserved from the last reply
    assert state.last_rtt_us is None  # rtt means "this probe", never "last known good"


def test_two_misses_needed_when_threshold_two(tmp_path: Path) -> None:
    conn = icmpstore.connect(tmp_path / "icmp.db")
    settings = IcmpSettings(down_after_cycles=2)
    targets = [Target(device="a", address="172.25.110.1")]
    _collect(conn, settings, targets, _ScriptedProbe({"172.25.110.1": [True]}), NOW)
    s1 = _collect(conn, settings, targets, _ScriptedProbe({"172.25.110.1": [False]}), NOW + 300)
    assert s1.new_down == 0  # one miss tolerated
    assert icmpstore.load_states(conn)[0].up is True
    s2 = _collect(conn, settings, targets, _ScriptedProbe({"172.25.110.1": [False]}), NOW + 600)
    assert s2.new_down == 1  # second consecutive miss → down
    assert icmpstore.load_states(conn)[0].up is False
    conn.close()


def test_reply_after_down_recovers(tmp_path: Path) -> None:
    conn = icmpstore.connect(tmp_path / "icmp.db")
    settings = IcmpSettings()
    targets = [Target(device="a", address="172.25.110.1")]
    _collect(conn, settings, targets, _ScriptedProbe({"172.25.110.1": [True]}), NOW)
    _collect(conn, settings, targets, _ScriptedProbe({"172.25.110.1": [False]}), NOW + 300)
    summary = _collect(conn, settings, targets, _ScriptedProbe({"172.25.110.1": [True]}), NOW + 600)
    state = icmpstore.load_states(conn)[0]
    conn.close()
    assert summary.recovered == 1
    assert state.up is True
    assert state.consecutive_misses == 0
    assert state.last_change_us == NOW + 600


def test_prunes_devices_no_longer_targeted(tmp_path: Path) -> None:
    conn = icmpstore.connect(tmp_path / "icmp.db")
    settings = IcmpSettings()
    both = [Target(device="a", address="172.25.110.1"), Target(device="b", address="172.25.110.2")]
    _collect(
        conn, settings, both, _ScriptedProbe({"172.25.110.1": [True], "172.25.110.2": [True]}), NOW
    )
    # 'b' drops out of the target set → pruned
    summary = _collect(
        conn,
        settings,
        [Target(device="a", address="172.25.110.1")],
        _ScriptedProbe({"172.25.110.1": [True]}),
        NOW + 300,
    )
    devices = {s.device for s in icmpstore.load_states(conn)}
    conn.close()
    assert summary.pruned == 1
    assert devices == {"a"}


def test_empty_targets_is_clean_noop(tmp_path: Path) -> None:
    conn = icmpstore.connect(tmp_path / "icmp.db")
    summary = run_collect(IcmpSettings(), [], conn=conn, now_us=NOW, probe=lambda h: (True, None))
    conn.close()
    assert summary.probed == 0 and summary.up == 0 and summary.down == 0


def test_load_uisp_flagged_views_the_same_snapshot_and_cache(tmp_path) -> None:
    # feature 135 (research D3): the flags-only view the watch cycle uses.
    snapshot = tmp_path / "snapshots" / "uisp.json"
    _write_snapshot(
        snapshot,
        [
            {
                "identity": "tamarack-ptp-manzanita-11",
                "address": "172.18.89.1",
                "maintenance": True,
            },
            {"identity": "yew-ptp-tamarack-60", "address": "172.18.84.129"},
        ],
    )
    settings = _uisp_settings(tmp_path)
    assert icmp.load_uisp_flagged(settings, snapshot_path=snapshot) == (
        {"tamarack-ptp-manzanita-11"},
        False,
        None,
    )
    snapshot.unlink()
    flagged, from_cache, note = icmp.load_uisp_flagged(settings, snapshot_path=snapshot)
    assert flagged == {"tamarack-ptp-manzanita-11"} and from_cache and note is not None
    flagged, from_cache, _ = icmp.load_uisp_flagged(
        _uisp_settings(tmp_path / "fresh"), snapshot_path=tmp_path / "missing.json"
    )
    assert flagged == set()  # no snapshot, no cache ⇒ nothing in maintenance
