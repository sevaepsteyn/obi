"""Shared helpers for the SNMP CLI/chat integration tests (feature 012).

Not a test module (leading underscore) — provides a config writer, a roster saver, scripted SNMP
agents, and a patcher that swaps the adapter's default transport factory for an offline fake, so
``obi metrics live`` and the chat path are exercised with no real SNMP agent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from obi.core import inventory as inv
from obi.core.drivers import snmp as snmp_mod
from obi.core.inventory import Inventory, InventoryEntry
from obi.core.models import DiscoveryScope, DiscoveryState
from tests.fakes import (
    FakeSnmpAgent,
    fake_snmp_factory,
    interfaces_agent,
    system_agent,
)

__all__ = [
    "COMMUNITY",
    "interfaces_agent",
    "patch_factory",
    "save_roster",
    "system_agent",
    "write_snmp_config",
]

# A community sentinel that MUST NOT appear in any output/log/audit (SC-004).
COMMUNITY = "SNMP-COMMUNITY-supersecret"


def write_snmp_config(tmp_path: Path, *, extra: str = "") -> Path:
    """Write a config.toml with [routeros] (for a roster) + [snmp]; perms 0600."""

    lines = [
        "[discovery]",
        'seeds = ["172.18.84.1"]',
        "[routeros]",
        'username = "admin"',
        'password = "fakesecret44028f"',
        "use_tls = true",
        "[snmp]",
        f'community = "{COMMUNITY}"',
    ]
    if extra:
        lines.append(extra)
    lines += [
        "[audit]",
        f'path = "{tmp_path / "audit.jsonl"}"',
        "[inventory]",
        f'path = "{tmp_path / "inventory.json"}"',
    ]
    config = tmp_path / "config.toml"
    config.write_text("\n".join(lines) + "\n")
    config.chmod(0o600)
    return config


def save_roster(tmp_path: Path, devices: list[tuple[str, str]]) -> None:
    """Persist an inventory of IDENTIFIED entries ``(address, identity)`` at the config path."""

    entries = [
        InventoryEntry(address=address, identity=identity,
                       discovery_state=DiscoveryState.IDENTIFIED)
        for address, identity in devices
    ]
    inv.save(
        Inventory(scope=DiscoveryScope(seeds=["172.18.84.1"]), entries=entries),
        tmp_path / "inventory.json",
    )


def patch_factory(monkeypatch: pytest.MonkeyPatch, agents: dict[str, FakeSnmpAgent]) -> None:
    """Swap the adapter's default transport factory for one serving ``agents`` (offline)."""

    monkeypatch.setattr(snmp_mod, "_default_transport_factory", fake_snmp_factory(agents))
