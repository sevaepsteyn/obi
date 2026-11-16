"""Unit tests for the independent leak gate (feature 134, T018/T020)."""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from obi.core.demo.gate import FATAL, WARN, GateResult, LeakGate, format_report
from obi.core.demo.harvest import Harvest

DISTINCT = "northfield"


def make_harvest() -> Harvest:
    h = Harvest()
    words = frozenset({"orchard"})
    h.add_name("northfield-gw", words)
    h.add_name("Orchard", words)
    h.add_person("Quillon", words)
    h.add_serial("HZ90A1B2C3D")
    h.add_secret("fakecommunity")
    h.slack_ids.add("U0FAKE0001")
    h.node_ids.add("!0fa4e001")
    h.domains.add("fictional-org.test")
    h.add_address("10.99.0.1")
    h.add_event_term("playa")
    h.add_event_term("zephyrine", fatal=True)
    h.site_coords.append((45.0, -100.0))
    return h


def clean_tree(root: Path) -> Path:
    """A dataset of fictional values only."""

    (root / "state").mkdir(parents=True)
    (root / "manifest.json").write_text(json.dumps({"organisation": "Example Valley Net"}))
    (root / "state" / "board.json").write_text(
        json.dumps(
            {
                "items": [
                    {
                        "name": "aspen-gw",
                        "address": "172.20.5.1",
                        "latitude": 37.16,
                        "longitude": -118.27,
                    }
                ]
            }
        )  # fmt: skip
    )
    conn = sqlite3.connect(root / "state" / "logs.db")
    conn.execute("CREATE TABLE logs (id INTEGER PRIMARY KEY, host TEXT, message TEXT)")
    conn.execute(
        "CREATE VIRTUAL TABLE logs_fts USING fts5(message, content='logs', content_rowid='id')"
    )
    conn.execute(
        "INSERT INTO logs VALUES (1, 'aspen-gw', 'peer 172.20.0.1 up; oid 1.3.6.1.4.1.14988.1')"
    )
    conn.execute("INSERT INTO logs_fts(logs_fts) VALUES ('rebuild')")
    conn.commit()
    conn.close()
    repo = root / "state" / "config-repo"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "main"], check=True)
    _commit(repo, "aspen-gw.rsc", "/system identity set name=aspen-gw\n")
    return root


def _commit(repo: Path, name: str, text: str, author: str = "obi <obi@example.net>") -> None:
    (repo / name).write_text(text)
    subprocess.run(["git", "-C", str(repo), "add", name], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=obi", "-c", "user.email=obi@example.net",
         "-c", "commit.gpgsign=false", "commit", "-q", "-m", f"{name} change",
         f"--author={author}"],
        check=True,
    )  # fmt: skip


def scan(root: Path, *, strict: bool = False) -> GateResult:
    return LeakGate(make_harvest(), strict_event_terms=strict).scan(root)


def test_clean_tree_has_no_findings(tmp_path: Path) -> None:
    result = scan(clean_tree(tmp_path / "ds"))
    assert result.fatal == [], format_report(result)
    assert result.counts["files"] >= 3


def _plant_sqlite(root: Path, message: str, host: str = "aspen-gw") -> None:
    conn = sqlite3.connect(root / "state" / "logs.db")
    conn.execute("INSERT INTO logs (host, message) VALUES (?, ?)", (host, message))
    conn.execute("INSERT INTO logs_fts(logs_fts) VALUES ('rebuild')")
    conn.commit()
    conn.close()


@pytest.mark.parametrize(
    ("message", "category"),
    [
        (f"link to {DISTINCT}-gw down", "place"),
        ("peer 10.99.0.1 up", "ip"),
        ("public 8.8.4.4 seen", "ip"),
        ("client 4C:5E:0C:11:22:01 joined", "mac"),
        ("client 4c5e.0c11.2201 joined", "mac"),
        ("serial hz90a1b2c3d", "serial"),
        ("community fakecommunity", "secret"),
        ("token sk-ant-abcdef123", "secret"),
        ("dm U0FAKE0001", "slack_id"),
        ("fix 45.0123,-100.0456", "coord"),
        ("mail pat@fictional-org.test", "email"),
        ("ask Quillon", "person"),
        ("node !0fa4e001 heard", "node"),
        ("near eastmor village", "fuzzy"),
        ("zephyrine on shift", "denylist"),
        ("lookup orchard-ap", "dictionary_word"),
    ],
)
def test_sqlite_cell_findings(tmp_path: Path, message: str, category: str) -> None:
    root = clean_tree(tmp_path / "ds")
    harvest = make_harvest()
    if category == "fuzzy":
        harvest.add_name("eastmoor", frozenset())
    _plant_sqlite(root, message)
    result = LeakGate(harvest).scan(root)
    assert category in {f.category for f in result.fatal}, format_report(result)
    hit = next(f for f in result.fatal if f.category == category)
    assert hit.file == "state/logs.db"
    if category != "fuzzy":
        assert hit.locator.startswith("logs:")


def test_fts_vocabulary_is_scanned(tmp_path: Path) -> None:
    root = clean_tree(tmp_path / "ds")
    conn = sqlite3.connect(root / "state" / "logs.db")
    # A standalone (non-external-content) FTS table keeps its own copy of the text.
    conn.execute("CREATE VIRTUAL TABLE notes USING fts5(body)")
    conn.execute("INSERT INTO notes VALUES ('northfield')")
    conn.commit()
    conn.close()
    result = scan(root)
    assert any(f.locator == "notes:vocab" or f.locator.startswith("notes_content") or
               f.locator == "raw-bytes" for f in result.fatal), format_report(result)  # fmt: skip


def test_json_key_and_value(tmp_path: Path) -> None:
    root = clean_tree(tmp_path / "ds")
    (root / "state" / "keys.json").write_text(json.dumps({"10.99.0.1": {"x": 1}}))
    (root / "state" / "vals.json").write_text(json.dumps({"items": [{"serial": "HZ90A1B2C3D"}]}))
    result = scan(root)
    files = {(f.file, f.category) for f in result.fatal}
    assert ("state/keys.json", "ip") in files
    assert ("state/vals.json", "serial") in files


def test_json_coordinate_pair(tmp_path: Path) -> None:
    root = clean_tree(tmp_path / "ds")
    (root / "state" / "geo.json").write_text(json.dumps({"latitude": 45.01, "longitude": -100.0}))
    assert any(f.category == "coord" for f in scan(root).fatal)


def test_git_old_commit_and_metadata(tmp_path: Path) -> None:
    root = clean_tree(tmp_path / "ds")
    repo = root / "state" / "config-repo"
    _commit(repo, "aspen-gw.rsc", "/ip address add address=10.99.0.1/24\n")
    _commit(repo, "aspen-gw.rsc", "/ip address add address=172.20.0.1/24\n")
    result = scan(root)
    assert any(f.category == "ip" and f.locator.startswith("git:blob") for f in result.fatal)

    root2 = clean_tree(tmp_path / "ds2")
    _commit(root2 / "state" / "config-repo", "b.rsc", "x\n", author="Quillon <q@example.net>")
    assert any(f.category == "person" for f in scan(root2).fatal)


def test_raw_file(tmp_path: Path) -> None:
    root = clean_tree(tmp_path / "ds")
    (root / "state" / "shade.png").write_bytes(b"\x89PNG\x00northfield\x00")
    assert any(f.file == "state/shade.png" for f in scan(root).fatal)


def test_event_terms_warn_by_default_and_fatal_when_strict(tmp_path: Path) -> None:
    root = clean_tree(tmp_path / "ds")
    _plant_sqlite(root, "dust on the playa today")
    result = scan(root)
    assert not result.fatal
    assert any(f.category == "event_term" and f.severity == WARN for f in result.findings)
    assert any(f.category == "event_term" for f in scan(root, strict=True).fatal)


def test_dictionary_word_prose_warns_identifier_fatal(tmp_path: Path) -> None:
    root = clean_tree(tmp_path / "ds")
    _plant_sqlite(root, "the back orchard printer is online")
    result = scan(root)
    assert not result.fatal
    assert any(f.category == "dictionary_word" and f.severity == WARN for f in result.findings)

    root2 = clean_tree(tmp_path / "ds2")
    _plant_sqlite(root2, "ok", host="orchard")
    assert any(f.category == "dictionary_word" for f in scan(root2).fatal)


def test_oids_and_versions_never_trip_the_ip_allowlist(tmp_path: Path) -> None:
    root = clean_tree(tmp_path / "ds")
    _plant_sqlite(root, "oid 1.3.6.1.4.1.14988.1.1.1 polled; build 6.49.10.1.2")
    assert not any(f.category == "ip" for f in scan(root).findings)


def test_report_never_contains_a_token(tmp_path: Path) -> None:
    root = clean_tree(tmp_path / "ds")
    planted = [
        "northfield", "10.99.0.1", "4C:5E:0C:11:22:01", "HZ90A1B2C3D", "fakecommunity",
        "U0FAKE0001", "45.0123,-100.0456", "pat@fictional-org.test", "Quillon", "!0fa4e001",
        "zephyrine", "playa",
    ]  # fmt: skip
    _plant_sqlite(root, " ".join(planted))
    result = scan(root)
    report = format_report(result)
    assert result.fatal
    for token in planted:
        assert token.casefold() not in report.casefold()
    assert all(f.severity in (FATAL, WARN) for f in result.findings)


def test_token_inside_camelcase_ordinary_word_is_not_a_leak(tmp_path: Path) -> None:
    root = clean_tree(tmp_path / "ds")
    harvest = make_harvest()
    harvest.add_person("Pantr", frozenset())
    _plant_sqlite(root, "dhcp lease for AspenPantry")
    assert not any(f.category == "person" for f in LeakGate(harvest).scan(root).fatal)
