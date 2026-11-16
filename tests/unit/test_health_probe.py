"""Unit (feature 096): `engine.health_probe` — probe fan-out → store rows → one audit record.

Offline: the driver runs against the recorded core-vms-1 fixture through the ExecFn seam.
Covers row mapping (×10 conventions, per-filesystem instances, hwmon labels), per-host
failure isolation (FR-008), the unconfigured no-op, and the counts-only audit entry.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from obi.core import metricstore
from obi.core.audit import AuditTrail
from obi.core.config import HealthLinuxSettings
from obi.core.drivers.linux_health import LinuxHealthDriver
from obi.core.engine import health_probe
from tests.fakes import chat_config

NOW = datetime(2026, 8, 25, 12, 0, 0, tzinfo=UTC)
_FIXTURE = json.loads(
    (Path(__file__).parent.parent / "fixtures" / "linux_health" / "core-vms-1.json").read_text()
)["commands"]


def _driver(hosts_up: set[str]) -> LinuxHealthDriver:
    def exec_fn(host: str, command: str, timeout: float) -> str:
        if host not in hosts_up:
            raise ConnectionRefusedError("no route")
        return _FIXTURE.get(command, "")

    return LinuxHealthDriver(HealthLinuxSettings(username="obi", hosts=[]), exec_fn=exec_fn)


def _config(tmp_path: Path, hosts: list[str]):
    cfg = chat_config(tmp_path)
    cfg.health_linux = HealthLinuxSettings(
        username="obi", hosts=hosts, db_path=tmp_path / "metrics.db"
    )
    return cfg


def _rows(db: Path) -> list[tuple]:
    conn = metricstore.connect_readonly(db)
    try:
        return conn.execute(
            "SELECT device, profile, metric, ifindex, ifname, value FROM samples"
        ).fetchall()
    finally:
        conn.close()


def test_probe_maps_readings_to_store_rows(tmp_path: Path) -> None:
    cfg = _config(tmp_path, ["core-vms-1"])
    report = health_probe(
        cfg, audit=AuditTrail(cfg.audit_path), now=NOW, driver=_driver({"core-vms-1"})
    )
    assert report.hosts_probed == 1 and report.hosts_ok == 1
    assert report.failures == []
    assert report.readings_stored > 0

    rows = _rows(tmp_path / "metrics.db")
    by_metric: dict[str, list[tuple]] = {}
    for r in rows:
        by_metric.setdefault(r[2], []).append(r)
    assert all(r[0] == "core-vms-1" and r[1] == "linux-health" for r in rows)
    # memory: totals raw kB + a ×10 used percent derived from total/available
    assert by_metric["linux:memTotalKb"][0][5] == 32215988
    used_pct = by_metric["linux:memUsedPercent"][0][5]
    assert 0 < used_pct < 1000  # ×10 percent
    # disks: one row per real filesystem, mount as instance AND label
    disk_mounts = {r[3] for r in by_metric["linux:diskUsedPercent"]}
    assert "/" in disk_mounts and "/home" in disk_mounts
    assert "/sys/firmware/efi/efivars" not in disk_mounts
    # temps: hwmon instance + chip label, deci-°C
    temp_rows = by_metric["linux:temperatureC"]
    assert any(r[4] == "coretemp" for r in temp_rows)
    assert all(100 < r[5] < 1200 for r in temp_rows)  # 10–120 °C in deci
    # cpu: ×10 percent from the two-sample delta
    assert 0 <= by_metric["linux:cpuUtilPercent"][0][5] <= 1000


def test_host_failure_is_isolated_and_counted(tmp_path: Path) -> None:
    cfg = _config(tmp_path, ["core-vms-1", "magnolia-vms-1"])
    report = health_probe(
        cfg, audit=AuditTrail(cfg.audit_path), now=NOW, driver=_driver({"core-vms-1"})
    )
    assert report.hosts_probed == 2 and report.hosts_ok == 1
    assert report.failures == ["magnolia-vms-1: unreachable"]
    devices = {r[0] for r in _rows(tmp_path / "metrics.db")}
    assert devices == {"core-vms-1"}  # nothing fabricated for the dead host


def test_unconfigured_is_a_clean_noop(tmp_path: Path) -> None:
    cfg = chat_config(tmp_path)
    cfg.health_linux = None
    report = health_probe(cfg, audit=AuditTrail(cfg.audit_path), now=NOW)
    assert report.hosts_probed == 0
    assert report.detail and "health.linux" in report.detail


def test_one_audit_record_counts_only(tmp_path: Path) -> None:
    cfg = _config(tmp_path, ["core-vms-1"])
    health_probe(cfg, audit=AuditTrail(cfg.audit_path), now=NOW, driver=_driver({"core-vms-1"}))
    records = [
        json.loads(line)
        for line in cfg.audit_path.read_text().splitlines()
        if '"health_probe"' in line
    ]
    assert len(records) == 1
    params = records[0]["params"]
    assert params["hosts_ok"] == 1 and params["readings_stored"] > 0
    assert "32215988" not in json.dumps(records[0])  # counts, never readings
