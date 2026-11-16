"""T012: the snmp_metrics chat handler against a FakeMetricsSource (feature 012).

Covers the rendered system output, the not-configured guidance (engine NOT called), and that the
SNMP credential never appears in the rendered output.
"""

from __future__ import annotations

from pathlib import Path

from obi.core.audit import AuditTrail
from obi.core.inventory import InventoryEntry
from obi.core.models import DiscoveryState
from obi.core.toolset import ToolContext, _snmp_metrics
from tests.fakes import (
    FakeMetricsSource,
    chat_config,
    save_inventory,
    snmp_settings,
)


def _ctx(
    tmp_path: Path, *, metrics_source: FakeMetricsSource | None, snmp: bool = True
) -> ToolContext:
    config = chat_config(tmp_path, snmp=snmp_settings() if snmp else None)
    save_inventory(
        config.inventory_path,
        [InventoryEntry(address="172.18.84.1", identity="core-gw-1",
                        discovery_state=DiscoveryState.IDENTIFIED)],
    )
    return ToolContext(
        config=config,
        driver=None,
        audit=AuditTrail(config.audit_path),
        metrics_source=metrics_source,
        question="snmp uptime on core?",
    )


def test_renders_system_metrics(tmp_path: Path) -> None:
    source = FakeMetricsSource()
    ctx = _ctx(tmp_path, metrics_source=source)

    out = _snmp_metrics(ctx, {"target": "all", "profile": "system", "source": "mikrotik"})

    assert "Live SNMP metrics" in out
    assert "core-gw-1" in out
    assert "sysName=core-gw-1" in out
    assert source.calls  # the handler reached the source


def test_not_configured_returns_guidance_without_calling_source(tmp_path: Path) -> None:
    # metrics_source present but config.snmp absent → not configured; the source must NOT be called.
    source = FakeMetricsSource()
    config = chat_config(tmp_path, snmp=None)
    ctx = ToolContext(
        config=config, driver=None, audit=AuditTrail(config.audit_path),
        metrics_source=source, question="snmp?",
    )

    out = _snmp_metrics(ctx, {"target": "all"})

    assert "not configured" in out.lower()
    assert source.calls == []


def test_unknown_profile_is_reported(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, metrics_source=FakeMetricsSource())
    out = _snmp_metrics(ctx, {"target": "all", "profile": "bogus"})
    assert "Unknown SNMP profile" in out


def test_credential_never_in_output(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, metrics_source=FakeMetricsSource())
    out = _snmp_metrics(ctx, {"target": "all"})
    assert "supersecret" not in out
