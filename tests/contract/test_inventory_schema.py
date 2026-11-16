"""T011: persisted inventory conforms to contracts/inventory.schema.json (FR-011)."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema

from obi.core.inventory import Inventory, InventoryEntry, to_dict
from obi.core.models import DiscoveryScope, DiscoveryState

_SCHEMA = json.loads(
    (
        Path(__file__).resolve().parents[2]
        / "tests" / "schemas" / "inventory.schema.json"
    ).read_text()
)


def test_inventory_document_conforms_to_schema() -> None:
    inv = Inventory(
        scope=DiscoveryScope(seeds=["172.25.110.1"], ranges=["172.25.110.0/24"]),
        entries=[
            InventoryEntry(
                address="172.25.110.1",
                identity="gw",
                discovery_state=DiscoveryState.IDENTIFIED,
                model="CCR2004",
                version="7.19.1",
                provenance="seed",
            ),
            # A v2 collapsed device: serial set, alternate address retained, unioned provenance.
            InventoryEntry(
                address="172.18.84.1",
                identity="core-gw-1",
                discovery_state=DiscoveryState.IDENTIFIED,
                model="CCR2004-1G-12S+2XS",
                version="7.15.3",
                provenance="ospf-peer-of:172.18.89.1@169.254.3.36 | neighbor-of:172.18.84.41",
                serial="ANON12345678",
                alt_addresses=["172.18.84.11"],
            ),
            InventoryEntry(
                address="172.25.110.50",
                identity="ap",
                discovery_state=DiscoveryState.UNSUPPORTED,
                platform="Ubiquiti Networks",
                provenance="neighbor-of:172.25.110.1",
                present_in_latest=False,
            ),
        ],
    )
    document = to_dict(inv)
    assert document["version"] == 2
    jsonschema.validate(document, _SCHEMA)


def test_v1_document_still_validates() -> None:
    """A pre-feature roster (version 1, no serial/alt_addresses) remains schema-valid (FR-010)."""

    v1_document = {
        "version": 1,
        "scope": {"ranges": ["172.25.110.0/24"], "seeds": ["172.25.110.1"]},
        "generated_at": "2026-06-01T00:00:00+00:00",
        "entries": [
            {
                "address": "172.25.110.1",
                "identity": "gw",
                "discovery_state": "identified",
                "model": "CCR2004",
                "version": "7.19.1",
                "platform": None,
                "provenance": "seed",
                "first_seen": "2026-06-01T00:00:00+00:00",
                "last_seen": "2026-06-01T00:00:00+00:00",
                "present_in_latest": True,
            }
        ],
    }
    jsonschema.validate(v1_document, _SCHEMA)
