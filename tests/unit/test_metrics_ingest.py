"""Unit (feature 021): the `obi metrics ingest` cycle — mapping, name-filter targeting, resilience,
and the single credential-free audit record.

Drives the real ``metrics_ingest`` engine over the fake SNMP transport (no device), so it exercises
the whole collect→map→persist path: which samples are stored (numeric only, keyed by identity),
which targets are polled (the name filter), how per-device failures are isolated (a gap, never an
abort), and the one ``metrics_ingest`` audit entry (counts only, no secret).
"""

from __future__ import annotations

import json
from pathlib import Path

from obi.core import metricstore
from obi.core.audit import AuditTrail
from obi.core.config import Secret, SnmpHistorySettings, SnmpSettings
from obi.core.engine import metrics_ingest
from obi.core.inventory import InventoryEntry
from obi.core.models import SnmpOutcome
from tests.fakes import (
    FakeSnmpAgent,
    chat_config,
    interfaces_agent,
    save_inventory,
    snmp_source,
    system_agent,
)


def _combined_agent(name: str) -> FakeSnmpAgent:
    """One agent answering BOTH the system and interfaces profiles (the collector reads both)."""

    sysa = system_agent(name)
    ifa = interfaces_agent()
    return FakeSnmpAgent(scalars=dict(sysa.scalars), table={**sysa.table, **ifa.table})


def _setup(
    tmp_path: Path,
    entries: list[InventoryEntry],
    agents: dict[str, FakeSnmpAgent],
    *,
    targets: str = "-gw",
    profiles: list[str] | None = None,
    community: str = "public",
):
    snmp = SnmpSettings(
        community=Secret(community),
        history=SnmpHistorySettings(
            db_path=tmp_path / "metrics.db",
            targets=targets,
            profiles=profiles or ["system", "interfaces"],
            retain_days=90,
        ),
    )
    config = chat_config(tmp_path, snmp=snmp)
    save_inventory(config.inventory_path, entries)
    return config, snmp_source(agents), AuditTrail(config.audit_path)


def _rows(db: Path) -> list[tuple]:
    conn = metricstore.connect_readonly(db)
    try:
        return conn.execute(
            "SELECT device, address, profile, metric, ifindex, ifname, kind, value FROM samples"
        ).fetchall()
    finally:
        conn.close()


# --- T006: collect → store mapping ------------------------------------------------------------


def test_stores_numeric_samples_with_correct_fields(tmp_path: Path) -> None:
    config, source, audit = _setup(
        tmp_path,
        [InventoryEntry(address="172.18.75.1", identity="test-gw")],
        {"172.18.75.1": _combined_agent("test-gw")},
    )
    report = metrics_ingest(config, audit=audit, metrics_source=source)

    assert report.targets_matched == 1
    assert report.devices_reachable == 1
    assert report.readings_stored > 0

    rows = _rows(config.snmp.history.db_path)  # type: ignore[union-attr]
    metrics = {r[3] for r in rows}
    # counters + gauges (incl. the feature-021 error/discard counters and ifOperStatus) are stored
    assert {
        "ifHCInOctets",
        "ifInErrors",
        "ifOutDiscards",
        "ifOperStatus",
        "cpuLoad",
        "memUsedKb",
        "sysUpTime",
    } <= metrics
    # STRING samples are labels, never stored as values
    assert not ({"sysDescr", "sysName", "ifName"} & metrics)
    # every row is keyed by the roster IDENTITY, with the queried address as provenance
    assert all(r[0] == "test-gw" and r[1] == "172.18.75.1" for r in rows)


def test_interface_and_scalar_row_shapes(tmp_path: Path) -> None:
    config, source, audit = _setup(
        tmp_path,
        [InventoryEntry(address="172.18.75.1", identity="test-gw")],
        {"172.18.75.1": _combined_agent("test-gw")},
    )
    metrics_ingest(config, audit=audit, metrics_source=source)
    rows = _rows(config.snmp.history.db_path)  # type: ignore[union-attr]

    inoct = next(r for r in rows if r[3] == "ifHCInOctets" and r[5] == "ether1")
    assert inoct[4] == "1"  # ifindex captured
    assert inoct[6] == "counter64"  # kind
    assert inoct[7] == 1_000_000  # value from the fake

    cpu = next(r for r in rows if r[3] == "cpuLoad")
    assert cpu[4] == ""  # device-scalar → empty ifindex
    assert cpu[5] is None  # no ifname
    assert cpu[7] == 40  # avg(30, 50)


# --- T007: name-filter targeting --------------------------------------------------------------


def test_name_filter_only_polls_matching_gateways(tmp_path: Path) -> None:
    entries = [
        InventoryEntry(address="172.18.75.1", identity="test-gw"),
        InventoryEntry(address="172.18.81.1", identity="chestnut-ap"),  # not a -gw
    ]
    agents = {
        "172.18.75.1": _combined_agent("test-gw"),
        "172.18.81.1": _combined_agent("chestnut-ap"),
    }
    config, source, audit = _setup(tmp_path, entries, agents, targets="-gw")
    report = metrics_ingest(config, audit=audit, metrics_source=source)

    assert report.targets_matched == 1
    assert {r[0] for r in _rows(config.snmp.history.db_path)} == {"test-gw"}  # type: ignore[union-attr]


# --- T015: resilience / no-ops ----------------------------------------------------------------


def test_failures_are_isolated_reachable_still_stored(tmp_path: Path) -> None:
    entries = [
        InventoryEntry(address="172.25.110.1", identity="a-gw"),
        InventoryEntry(address="172.25.110.2", identity="b-gw"),
        InventoryEntry(address="172.25.110.3", identity="c-gw"),
    ]
    agents = {
        "172.25.110.1": _combined_agent("a-gw"),  # reachable
        "172.25.110.2": FakeSnmpAgent(fail=SnmpOutcome.AUTH_FAILED),
        # 172.25.110.3 has no agent → the factory reports it unreachable
    }
    config, source, audit = _setup(tmp_path, entries, agents)
    report = metrics_ingest(config, audit=audit, metrics_source=source)

    assert report.targets_matched == 3
    assert report.devices_reachable == 1
    assert report.outcomes.get("auth_failed") == 1
    assert report.outcomes.get("unreachable") == 1
    # only the reachable gateway produced rows; the others are gaps, not a cycle failure
    assert {r[0] for r in _rows(config.snmp.history.db_path)} == {"a-gw"}  # type: ignore[union-attr]


def test_no_roster_is_clean_noop(tmp_path: Path) -> None:
    snmp = SnmpSettings(
        community=Secret("public"),
        history=SnmpHistorySettings(db_path=tmp_path / "metrics.db"),
    )
    config = chat_config(tmp_path, snmp=snmp)  # inventory file intentionally not written
    report = metrics_ingest(
        config, audit=AuditTrail(config.audit_path), metrics_source=snmp_source({})
    )

    assert report.targets_matched == 0
    assert report.readings_stored == 0
    assert report.detail is not None
    assert not (tmp_path / "metrics.db").exists()  # nothing created — a true no-op


def test_no_matching_target_is_noop(tmp_path: Path) -> None:
    config, source, audit = _setup(
        tmp_path,
        [InventoryEntry(address="172.25.110.1", identity="chestnut-ap")],
        {"172.25.110.1": _combined_agent("chestnut-ap")},
        targets="-gw",
    )
    report = metrics_ingest(config, audit=audit, metrics_source=source)
    assert report.targets_matched == 0
    assert report.readings_stored == 0


# --- T016: one audit record, no secrets -------------------------------------------------------


def test_one_audit_record_counts_only_no_secret(tmp_path: Path) -> None:
    config, source, audit = _setup(
        tmp_path,
        [InventoryEntry(address="172.18.75.1", identity="test-gw")],
        {"172.18.75.1": _combined_agent("test-gw")},
        community="topsecret-community-xyz",
    )
    metrics_ingest(config, audit=audit, metrics_source=source)

    records = [
        json.loads(line)
        for line in config.audit_path.read_text().splitlines()
        if line and json.loads(line)["action"] == "metrics_ingest"
    ]
    assert len(records) == 1
    rec = records[0]
    assert rec["outcome"] == "ok"
    assert set(rec["params"]) >= {
        "targets_matched",
        "devices_reachable",
        "devices_failed",
        "readings_stored",
        "readings_pruned",
        "profiles",
    }
    # the community string appears nowhere in the record, and no raw metric value is leaked
    assert "topsecret-community-xyz" not in json.dumps(rec)


# --- the hang net + per-profile failure visibility (metrics-pipeline fix, 2026-07-11) -----------


def test_walk_deadline_is_a_hang_net_not_a_rate_limit() -> None:
    """A table walk is many PDU rounds: a big router legitimately outlives the per-PDU SNMP
    timeout while answering promptly. The outer net only catches a truly hung transport —
    it must be far looser than timeout+grace (which silently killed core-gw-1's interfaces
    profile every cron cycle)."""

    from obi.core.engine import _TIMEOUT_GRACE, _WALK_BUDGETS, _walk_deadline

    assert _WALK_BUDGETS >= 3  # several whole timeout budgets, not one
    assert _walk_deadline(10.0) == 10.0 * _WALK_BUDGETS + _TIMEOUT_GRACE  # 31s, not 11s
    assert _walk_deadline(10.0) > 10.0 + _TIMEOUT_GRACE  # strictly looser than the old net


def test_per_profile_failure_is_named_even_when_the_device_stays_reachable(
    tmp_path: Path,
) -> None:
    """A device answering one profile but losing another must not be a silent gap: the
    report names the device+profile so the cron log shows the loss (the core-gw-1 lesson)."""

    from datetime import UTC, datetime

    from obi.core.models import MetricKind, MetricReport, MetricSample

    class _ProfileFlakySource:
        name = "snmp"

        def fetch_metrics(self, target, *, profile, timeout):  # noqa: ANN001, ANN202
            if profile.name == "interfaces":
                return MetricReport(
                    target=target,
                    outcome=SnmpOutcome.TIMED_OUT,
                    profile=profile.name,
                    notes=["query exceeded the per-target timeout budget"],
                )
            return MetricReport(
                target=target,
                outcome=SnmpOutcome.REACHABLE,
                profile=profile.name,
                gathered_at=datetime.now(UTC),
                samples=[
                    MetricSample(
                        name="cpuLoad", oid="1.3.6.1.2.1.25.3.3.1.2", kind=MetricKind.GAUGE,
                        value=7,
                    )
                ],
            )

    config, _source, audit = _setup(
        tmp_path,
        [InventoryEntry(address="172.25.110.1", identity="a-gw")],
        {},
    )
    report = metrics_ingest(config, audit=audit, metrics_source=_ProfileFlakySource())

    assert report.devices_reachable == 1  # the system profile answered
    assert report.failures == ["a-gw: interfaces timed_out"]  # the loss is named
    stored_metrics = {r[3] for r in _rows(config.snmp.history.db_path)}  # type: ignore[union-attr]
    assert stored_metrics == {"cpuLoad"}


# --- 096: the health/optics profiles ride the same collector ----------------------------------


def test_health_and_optics_profiles_store_rows(tmp_path: Path) -> None:
    from tests.fakes import snmp_agent_from_fixture

    fixture = snmp_agent_from_fixture("health-ccr2004")
    agent = FakeSnmpAgent(table=dict(fixture.table))
    config, source, audit = _setup(
        tmp_path,
        [InventoryEntry(address="172.18.81.1", identity="chestnut-gw")],
        {"172.18.81.1": agent},
        profiles=["health", "optics"],
    )
    report = metrics_ingest(config, audit=audit, metrics_source=source)
    assert report.devices_reachable == 1
    assert report.failures == []

    rows = _rows(config.snmp.history.db_path)  # type: ignore[union-attr]
    by_metric: dict[str, list[tuple]] = {}
    for r in rows:
        by_metric.setdefault(r[3], []).append(r)

    # health rows: profile tag, sensor-name label, sensor-row-id instance, gauge kind
    psu = by_metric["health:psu1-state"][0]
    assert psu[2] == "health" and psu[4] == "7401" and psu[5] == "psu1-state"
    assert psu[6] == "gauge"
    # temperatures normalized ×10 at ingest (deci-°C in the store)
    cpu_temp = by_metric["health:cpu-temperature"][0]
    assert cpu_temp[7] % 10 == 0 and cpu_temp[7] >= 200  # a live CPU is ≥20.0 °C
    # optics rows: ifIndex instance, port-name label, signed mdBm value
    rx = by_metric["optics:rxPowerMdbm"]
    assert any(r[7] < 0 for r in rx)
    assert all(r[2] == "optics" and r[5] for r in rx)
    # absent samples are never persisted (FR-003): no NULLs can exist by schema, and no
    # zero-DDM fabrications either — every stored optics supply voltage is a live module's
    assert all(r[7] > 0 for r in by_metric["optics:supplyVoltageMv"])


def test_zero_ddm_modules_store_nothing(tmp_path: Path) -> None:
    from tests.fakes import snmp_agent_from_fixture

    config, source, audit = _setup(
        tmp_path,
        [InventoryEntry(address="172.18.84.1", identity="core-gw-1")],
        {"172.18.84.1": snmp_agent_from_fixture("optics-zero-row")},
        profiles=["optics"],
    )
    metrics_ingest(config, audit=audit, metrics_source=source)
    rows = _rows(config.snmp.history.db_path)  # type: ignore[union-attr]
    assert rows == []  # the only module answers no diagnostics — nothing is fabricated
