"""T025 (US3): degradation + bounded sweep timing (feature 012).

Covers: a mixed full-fleet sweep where every device gets an explicit outcome and none is dropped
(SC-002), completing well within the bound even with a down subset (SC-005 + finding E1); and the
engine's per-target timeout net bounding a hung agent rather than hanging the run.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from obi.cli.main import app
from obi.core.audit import AuditTrail
from obi.core.config import Config
from obi.core.engine import snmp_metrics
from obi.core.inventory import InventoryEntry
from obi.core.models import DiscoveryScope, DiscoveryState, MetricReport, MetricTarget, SnmpOutcome
from tests.fakes import FakeSnmpAgent, save_inventory, snmp_settings
from tests.integration._snmp import patch_factory, save_roster, system_agent, write_snmp_config

runner = CliRunner()

_VALID_OUTCOMES = {o.value for o in SnmpOutcome}


def test_synthetic_50_device_sweep_bounded_and_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = write_snmp_config(tmp_path)
    devices = [(f"172.18.85.{i}", f"dev-{i}") for i in range(1, 51)]
    save_roster(tmp_path, devices)
    # ~40 agents answer; the remaining ~10 addresses have no agent → fast UNREACHABLE.
    agents: dict[str, FakeSnmpAgent] = {
        addr: system_agent(name=name) for addr, name in devices[:40]
    }
    patch_factory(monkeypatch, agents)

    started = time.monotonic()
    result = runner.invoke(
        app, ["metrics", "live", "--source", "mikrotik", "--json", "--scope", str(config)]
    )
    elapsed = time.monotonic() - started

    assert result.exit_code == 0, result.output
    reports = json.loads(result.output)["reports"]
    assert len(reports) == 50  # SC-002: no device silently dropped
    assert all(r["outcome"] in _VALID_OUTCOMES for r in reports)
    assert elapsed < 30.0  # SC-005: bounded sweep


class _HangingSource:
    """A MetricsSource whose reads sleep past the engine's deadline (a wedged agent)."""

    name = "snmp"

    def fetch_metrics(
        self, target: MetricTarget, *, profile: object, timeout: float
    ) -> MetricReport:
        time.sleep(2.0)  # ignore the timeout, like a hung agent
        return MetricReport(target=target, outcome=SnmpOutcome.REACHABLE)


def test_engine_timeout_net_bounds_a_hung_agent(tmp_path: Path) -> None:
    save_inventory(
        tmp_path / "inventory.json",
        [InventoryEntry(address="172.18.84.1", identity="hung",
                        discovery_state=DiscoveryState.IDENTIFIED)],
    )
    from tests.fakes import settings

    config = Config(
        scope=DiscoveryScope(seeds=["172.18.84.1"]),
        routeros=settings(),
        snmp=snmp_settings(timeout=0.2),
        audit_path=tmp_path / "audit.jsonl",
        inventory_path=tmp_path / "inventory.json",
    )

    started = time.monotonic()
    reports = snmp_metrics(
        _HangingSource(), config=config, target="all", source="mikrotik",
        audit=AuditTrail(config.audit_path),
    )
    elapsed = time.monotonic() - started

    assert [r.outcome for r in reports] == [SnmpOutcome.TIMED_OUT]  # net caught the hang
    assert elapsed < 1.9  # returned on the deadline, not after the agent's 2s sleep
