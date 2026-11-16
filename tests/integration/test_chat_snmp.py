"""T014 (US1): an SNMP question drives a snmp_metrics tool call → a grounded, live-framed answer;
the SNMP read is audited per target with the operator's question as intent, credential-free."""

from __future__ import annotations

from pathlib import Path

from obi.chat.session import ask
from obi.core.inventory import InventoryEntry
from obi.core.models import DiscoveryState
from tests.fakes import (
    FakeMetricsSource,
    chat_config,
    chat_runtime,
    save_inventory,
    snmp_settings,
    text_response,
    tool_response,
)


def test_us1_snmp_question_drives_query_metrics_and_is_audited(tmp_path: Path) -> None:
    config = chat_config(tmp_path, snmp=snmp_settings())
    save_inventory(
        config.inventory_path,
        [InventoryEntry(address="172.18.84.1", identity="core-gw-1",
                        discovery_state=DiscoveryState.IDENTIFIED)],
    )
    runtime = chat_runtime(
        config,
        responses=[
            tool_response("snmp_metrics", {"target": "all", "source": "mikrotik"}),
            text_response("core-gw-1 reports a long uptime over SNMP."),
        ],
        metrics_source=FakeMetricsSource(),
    )

    result = ask(runtime, [], "what's the uptime on the core devices over SNMP?")

    assert result.tools_used == ["snmp_metrics"]
    assert "core-gw-1" in result.answer

    text = (tmp_path / "audit.jsonl").read_text()
    snmp_records = [ln for ln in text.splitlines() if '"snmp_metrics"' in ln]
    assert len(snmp_records) == 1  # one per target
    assert "what's the uptime on the core devices over SNMP?" in snmp_records[0]
    assert "supersecret" not in text  # credential never audited (FR-007/SC-004)
