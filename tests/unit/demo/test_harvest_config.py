"""The config-only harvest the 137 identity guard uses (T006): no store is ever opened."""

from __future__ import annotations

import tomllib
from pathlib import Path

from obi.core.demo.harvest import harvest_config

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "demo" / "fixture.toml"
WORDS = frozenset({"orchard", "gate"})

EXTRA = {
    "radio": {
        "known_callers": ["Zephyrine"],
        "operator_handles": ["Quillon"],
        "caller_aliases": {"zefreen": "Zephyrine"},
        "ftdi_serial": "QX7Z91AB",
    },
    "meshtastic": {"names": {"!0fa4e001": "Tumbleweed 4411"}, "aliases": {"tumble": "Tumbleweed"}},
    "paging": {"url": "https://pager.fictional-org.test/api.php"},
    "traffic": {"base_url": "https://traffic.fictional-org.test"},
    "brc": {"origin_lat": 45.01, "origin_lon": -100.02},
    "weather": {"latitude": 45.0, "longitude": -100.0},
}


def _raw() -> dict:
    raw = tomllib.loads(FIXTURE.read_text(encoding="utf-8"))
    for section, values in EXTRA.items():
        raw.setdefault(section, {}).update(values)
    return raw


def test_harvest_config_collects_every_identity_class() -> None:
    h = harvest_config(_raw(), WORDS)
    assert "fictional-org.test" in h.domains
    assert {"zephyrine", "quillon", "tumbleweed 4411", "tumble"} <= h.persons
    assert "!0fa4e001" in h.node_ids
    assert "QX7Z91AB" in h.serials
    assert "fakepass99" in h.secrets
    assert (45.0, -100.0) in h.coords and (45.01, -100.02) in h.coords
    assert "10.99.0.1" in h.ips


def test_harvest_config_opens_no_store(tmp_path: Path, monkeypatch) -> None:
    import sqlite3

    def boom(*_a, **_k):  # pragma: no cover - must not be called
        raise AssertionError("harvest_config opened a store")

    monkeypatch.setattr(sqlite3, "connect", boom)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "does-not-exist"))
    assert harvest_config(_raw(), WORDS).token_count() > 0
