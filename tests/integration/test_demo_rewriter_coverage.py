"""SC-003 part (b): the rewriter covers every entity class × source in variant spellings
(feature 134, T045). Each case plants a REAL token in a SOURCE store; the run must publish
(exit 0), the token must be gone from every byte of the dataset, and — where the mapping is
checkable — its fictional counterpart must be there instead.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from obi.core.demo.mapping import PseudonymMap
from tests.fixtures.demo.build_state import BUSY_START
from tests.fixtures.demo.harness import DemoEnv, dump_text, make_env, run_cli

Plant = Callable[[Path], None]


def log_line(text: str) -> Plant:
    def plant(state: Path) -> None:
        conn = sqlite3.connect(state / "logs.db")
        ts = int(BUSY_START.timestamp() * 1e6) + 777
        conn.execute(
            "INSERT INTO logs (ts_us, ts_iso, host, subsystem, topics, severity, message,"
            " raw_line, collectors, dedup_key) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (ts, BUSY_START.isoformat(), "core-gw-1", "system", "system,info", None, text, text,
             "core", f"planted-{abs(hash(text))}"),
        )  # fmt: skip
        conn.commit()
        conn.close()

    return plant


def audit_payload(text: str) -> Plant:
    def plant(state: Path) -> None:
        conn = sqlite3.connect(state / "audit.db")
        ts = int(BUSY_START.timestamp() * 1e6) + 999
        conn.execute(
            "INSERT INTO events (ts_us, ts_iso, kind, source, target, summary, payload, outcome)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (ts, BUSY_START.isoformat(), "device_command", "cli:status", "core-gw-1", "probe",
             json.dumps({"note": text}), "ok"),
        )  # fmt: skip
        conn.commit()
        conn.close()

    return plant


def dash_label(text: str) -> Plant:
    def plant(state: Path) -> None:
        path = state / "dash" / "board.json"
        data = json.loads(path.read_text())
        data["tiles"][0]["items"][0]["label"] = text
        path.write_text(json.dumps(data))

    return plant


CASES: list[tuple[str, str, Plant, str | None]] = [
    ("mac-dash", "4c-5e-0c-11-22-02", log_line("client 4c-5e-0c-11-22-02 roamed"), None),
    ("mac-dotted", "4C5E.0C11.2203", log_line("client 4C5E.0C11.2203 roamed"), None),
    ("fqdn", "northfield-gw.fictional-org.test",
     log_line("resolved northfield-gw.fictional-org.test"), "northfield"),
    ("url-ip", "10.99.0.1", log_line("GET https://10.99.0.1:8443/status"), "10.99.0.1"),
    ("ipv6", "2600:1700:abcd:1::5", log_line("peer 2600:1700:abcd:1::5 up"), None),
    ("coord-space", "45.0123 -100.0456", audit_payload("fix 45.0123 -100.0456"), None),
    ("serial-lower", "hz90a1b2c3d", audit_payload("serial hz90a1b2c3d"), None),
    ("person-label", "Quillon", dash_label("Quillon's console"), None),
    ("site-upper", "SOUTHGATE", dash_label("SOUTHGATE-GW"), "southgate"),
    ("dictionary-identifier", "orchard-ap", log_line("roam to orchard-ap ok"), "orchard"),
]  # fmt: skip


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DemoEnv:
    return make_env(tmp_path, monkeypatch)


@pytest.mark.parametrize(("name", "token", "plant", "lookup"), CASES, ids=[c[0] for c in CASES])
def test_planted_variant_is_rewritten(
    env: DemoEnv, name: str, token: str, plant: Plant, lookup: str | None
) -> None:
    plant(env.state)
    result = run_cli("demo", "snapshot", "--config", str(env.config))
    assert result.exit_code == 0, result.output
    text = dump_text(env.output)
    assert token not in text
    if lookup is not None:
        hits = PseudonymMap.load_or_create(env.mapping).lookup(lookup)
        assert hits, f"{name}: no mapping recorded"
        assert any(fake in text for _cls, fake in hits)
