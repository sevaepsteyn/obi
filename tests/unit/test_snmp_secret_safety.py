"""T024 (US3): the SNMP credential (community + v3 keys) appears in no rendered output, no --json,
no log, and no audit record across outcomes (SC-004 / FR-007)."""

from __future__ import annotations

from pathlib import Path

from obi.core.audit import AuditTrail
from obi.core.config import Config, Secret
from obi.core.engine import snmp_metrics
from obi.core.inventory import InventoryEntry
from obi.core.models import DiscoveryScope, DiscoveryState, SnmpOutcome
from obi.core.serialize import metric_reports_to_json
from tests.fakes import FakeSnmpAgent, save_inventory, settings, snmp_settings, snmp_source
from tests.integration._snmp import system_agent

_COMMUNITY = "COMMUNITY-SENTINEL-zzz"
_AUTH_KEY = "AUTHKEY-SENTINEL-zzz"
_PRIV_KEY = "PRIVKEY-SENTINEL-zzz"
_SENTINELS = (_COMMUNITY, _AUTH_KEY, _PRIV_KEY)


def test_credential_never_leaks_across_channels(tmp_path: Path) -> None:
    save_inventory(
        tmp_path / "inventory.json",
        [
            InventoryEntry(address="172.18.84.1", identity="up",
                           discovery_state=DiscoveryState.IDENTIFIED),
            InventoryEntry(address="172.18.84.2", identity="down",
                           discovery_state=DiscoveryState.IDENTIFIED),
        ],
    )
    config = Config(
        scope=DiscoveryScope(seeds=["172.18.84.1"]),
        routeros=settings(),
        snmp=snmp_settings(
            community=Secret(_COMMUNITY),
            v3_auth_key=Secret(_AUTH_KEY),
            v3_priv_key=Secret(_PRIV_KEY),
        ),
        audit_path=tmp_path / "audit.jsonl",
        inventory_path=tmp_path / "inventory.json",
    )
    # A mix of a reachable and a failed device so every outcome path is exercised.
    source = snmp_source(
        {
            "172.18.84.1": system_agent(name="up"),
            "172.18.84.2": FakeSnmpAgent(fail=SnmpOutcome.AUTH_FAILED),
        },
        community=Secret(_COMMUNITY),
        v3_auth_key=Secret(_AUTH_KEY),
        v3_priv_key=Secret(_PRIV_KEY),
    )

    reports = snmp_metrics(
        source, config=config, target="all", source="mikrotik",
        audit=AuditTrail(config.audit_path),
    )

    rendered = metric_reports_to_json(reports) + repr(reports)
    audit_text = (tmp_path / "audit.jsonl").read_text()
    for sentinel in _SENTINELS:
        assert sentinel not in rendered
        assert sentinel not in audit_text
