"""Per-source tests over one snapshot of the synthetic state (feature 134, T027–T041).

One module-scoped run publishes a dataset; each test checks one source's contract on it — row
counts (or decimation), cross-reference consistency, and the source-specific rules. Every source
lands behind the gate: the run only publishes when the gate is clean.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import sqlite3
import subprocess
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from obi.core.config import load_config
from obi.core.demo.gate import LeakGate
from obi.core.demo.harvest import harvest
from obi.core.demo.naming import load_dictionary
from obi.core.demo.profile import load_profile
from obi.core.demo.relocate import haversine_m
from obi.core.demo.snapshot import SnapshotResult, run_snapshot
from obi.core.demo.sources.excluded import excluded_paths
from tests.fixtures.demo.build_state import (
    BUSY_END,
    BUSY_START,
    DEVICES,
    RADIOS,
    SyntheticTruth,
    build_state,
    write_config,
)
from tests.fixtures.demo.harness import dump_text, offline_terrain


@dataclass
class Snap:
    result: SnapshotResult
    out: Path
    state: Path
    live: Path
    truth: SyntheticTruth
    config: Path


@pytest.fixture(scope="module")
def snap(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Snap]:
    root = tmp_path_factory.mktemp("demo")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("XDG_STATE_HOME", str(root / "xdg"))
        mp.setenv("XDG_CACHE_HOME", str(root / "cache"))
        offline_terrain(mp)
        truth = build_state(root / "xdg")
        out = root / "out" / "dataset"
        config = write_config(root / "cfg" / "config.toml", output_dir=out)
        raw = tomllib.loads(config.read_text())
        result = run_snapshot(raw, load_profile(raw, config_path=config))
    yield Snap(result, out, out / "state" / "obi", root / "xdg" / "obi", truth, config)


def report(snap: Snap, name: str) -> object:
    return next(r for r in snap.result.reports if r.name == name)


def db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def test_published_and_gate_clean(snap: Snap) -> None:
    assert snap.result.state == "audited"
    assert snap.result.gate is not None and snap.result.gate.fatal == []
    text = dump_text(snap.out)
    for token in snap.truth.all_fatal_tokens():
        assert token not in text, f"planted {type(token).__name__} survived"


# --- T027 logs --------------------------------------------------------------------------------


def test_logs_window_dedup_fts_and_watermarks(snap: Snap) -> None:
    rep = report(snap, "logs")
    live = db(snap.live / "logs.db")
    lo, hi = int(BUSY_START.timestamp() * 1e6), int(BUSY_END.timestamp() * 1e6)
    expected = live.execute("SELECT count(*) FROM logs WHERE ts_us BETWEEN ? AND ?", (lo, hi))
    assert rep.rows_out == rep.rows_in == expected.fetchone()[0]  # type: ignore[attr-defined]
    out = db(snap.state / "logs.db")
    assert out.execute("SELECT min(id) FROM logs").fetchone()[0] == 1
    row = out.execute("SELECT ts_us, host, message, dedup_key FROM logs LIMIT 1").fetchone()
    key = hashlib.sha256(f"{row['ts_us']}\x1f{row['host']}\x1f{row['message']}".encode())
    assert row["dedup_key"] == key.hexdigest()
    host = row["host"].split("-")[0]
    assert out.execute("SELECT count(*) FROM logs_fts WHERE logs_fts MATCH ?", (host,)).fetchone()[
        0
    ]
    assert out.execute("SELECT count(*) FROM ingest_state").fetchone()[0] == 0


# --- T028 configs -----------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout


def test_configs_fresh_history(snap: Snap) -> None:
    repo = snap.state / "config-repo"
    authors = set(_git(repo, "log", "--format=%an <%ae>|%cn <%ce>").split())
    assert authors <= {"obi", "<obi@example.net>|obi", "<obi@example.net>"}
    subjects = _git(repo, "log", "--format=%s").splitlines()
    assert subjects[-1].startswith("baseline")
    assert len(subjects) == 1 + report(snap, "configs").rows_out  # type: ignore[attr-defined]
    names = {p.name for p in repo.glob("*.rsc")}
    assert "core-gw-1.rsc" in names and not any("northfield" in n for n in names)
    assert not (repo / ".git" / "index").exists()
    assert (repo / "config-captures.json").exists() is False
    assert (snap.state / "config-captures.json").exists()


# --- T029 audit -------------------------------------------------------------------------------


def test_audit_allowlist_truncation_and_personas(snap: Snap) -> None:
    out = db(snap.state / "audit.db")
    kinds = {r[0] for r in out.execute("SELECT DISTINCT kind FROM events")}
    assert kinds <= {"device_command", "action", "tool_call"}
    sources = {r[0] for r in out.execute("SELECT DISTINCT source FROM events")}
    assert not sources & {"chat", "slack", "radio", "voice", "mcp"}
    assert out.execute("SELECT count(*) FROM events WHERE truncated = 1").fetchone()[0] > 0
    big = out.execute("SELECT max(length(payload)) FROM events").fetchone()[0]
    assert big <= 4096
    lines = (snap.state / "audit.jsonl").read_text().splitlines()
    assert all(json.loads(line).get("actor", "operator") != "U0FAKE0002" for line in lines)
    manifest = json.loads((snap.out / "manifest.json").read_text())
    assert manifest["excluded_records"]["audit-kind-not-allowlisted"] > 0
    assert manifest["excluded_records"]["audit-conversational-source"] > 0


# --- T030 inventory / T031 metrics / T032 icmp -------------------------------------------------


def test_inventory_fields(snap: Snap) -> None:
    data = json.loads((snap.state / "inventory.json").read_text())
    assert len(data["entries"]) == len(DEVICES)
    assert data["scope"]["ranges"][0].endswith(".0.0/16")
    for entry in data["entries"]:
        assert len(entry["serial"]) == 12 and entry["alt_addresses"]


def test_metrics_decimated_per_series_with_health(snap: Snap) -> None:
    rep = report(snap, "metrics")
    out = db(snap.state / "metrics.db")
    per_series = out.execute(
        "SELECT device, metric, ifindex, count(*) FROM samples GROUP BY 1, 2, 3"
    ).fetchall()
    assert len(per_series) == len(DEVICES) * 3
    assert rep.rows_out == sum(r[3] for r in per_series)  # type: ignore[attr-defined]
    every = 6  # the profile default
    assert rep.rows_out * every >= rep.rows_in >= (rep.rows_out - len(per_series)) * every  # type: ignore[attr-defined]
    profiles = {r[0] for r in out.execute("SELECT DISTINCT profile FROM samples")}
    assert "health" in profiles


def test_icmp(snap: Snap) -> None:
    out = db(snap.state / "icmp.db")
    assert out.execute("SELECT count(*) FROM device_state").fetchone()[0] == len(DEVICES)
    targets = json.loads((snap.state / "icmp-uisp-targets.json").read_text())
    assert len(targets) == len(RADIOS)


# --- T033 OSPF + snapshots: topology fidelity (SC-004) ------------------------------------------


def test_ospf_edges_and_layout_resolve_to_output_devices(snap: Snap) -> None:
    ospf = json.loads((snap.state / "ar" / "snapshots" / "ospf.json").read_text())["data"]
    node_ids = {n["router_id"] for n in ospf["nodes"]}
    assert len(ospf["edges"]) == 3
    for edge in ospf["edges"]:
        assert edge["a"] in node_ids and edge["b"] in node_ids
    inventory = json.loads((snap.state / "inventory.json").read_text())
    addresses = {e["address"] for e in inventory["entries"]}
    identities = {e["identity"] for e in inventory["entries"]}
    for node in ospf["nodes"]:
        assert node["address"] in addresses and node["identity"] in identities
    layout = json.loads((snap.state / "ospf-layout.json").read_text())
    assert set(layout) - {"__cloud__"} <= addresses
    assert not (snap.state / "ar" / "snapshots" / "netbox-drift.json").exists()


def test_dash(snap: Snap) -> None:
    pager = json.loads((snap.state / "dash" / "pager-state.json").read_text())
    (key,) = pager["items"]
    assert key.startswith("fleet|") and key.endswith("-gw") and "southgate" not in key
    assert not (snap.state / "dash" / "run.lock").exists()


def test_weather_without_alerts(snap: Snap) -> None:
    out = db(snap.state / "weather.db")
    snaps_n = out.execute("SELECT count(*) FROM snapshots").fetchone()[0]
    assert snaps_n == 72
    assert out.execute("SELECT count(*) FROM hourly").fetchone()[0] == snaps_n
    assert out.execute("SELECT count(*) FROM alerts").fetchone()[0] == 0


# --- T037 geo / T038 mesh ---------------------------------------------------------------------


def test_geo_relocated_lengths_preserved_and_no_city_grid(snap: Snap) -> None:
    live = json.loads((snap.live / "ar" / "links.json").read_text())
    out = json.loads((snap.state / "ar" / "links.json").read_text())
    live_pts = {e["name"]: (e["latitude"], e["longitude"]) for e in live["endpoints"]}
    out_pts = [(e["latitude"], e["longitude"]) for e in out["endpoints"]]
    assert all(36 < lat < 38.5 for lat, _ in out_pts)
    before = sorted(
        haversine_m(*a, *b) for a, b in itertools.combinations(sorted(live_pts.values()), 2)
    )
    after = sorted(haversine_m(*a, *b) for a, b in itertools.combinations(sorted(out_pts), 2))
    assert all(abs(x - y) < 1.0 for x, y in zip(before, after, strict=True))
    assert (snap.state / "ar" / "terrain.json").exists()
    assert not (snap.state / "ar" / "brc.json").exists()
    assert not (snap.state / "ar" / "brc-2026").exists()
    for site in out["sites"]:
        assert site["ground_elevation_m"] == 1201.0 and len(site["enu"]) == len(out_pts)


def test_mesh_relocated_and_renamed(snap: Snap) -> None:
    out = db(snap.state / "meshtastic" / "positions.db")
    rows = out.execute("SELECT node_id, lat_udeg, address FROM position_fix").fetchall()
    assert rows and {r["node_id"] for r in rows} == {"node-01"}
    assert all(36 < r["lat_udeg"] / 1e7 < 38.5 for r in rows)
    assert all("," in r["address"] and "near" not in r["address"] for r in rows)
    assert not (snap.state / "meshtastic" / "daemon.lock").exists()


# --- T039 config.toml / T040 manifest / exclusions ----------------------------------------------


def test_config_round_trips_through_load_config(snap: Snap) -> None:
    config = load_config(snap.out / "config.toml")
    assert config.routeros is not None and config.routeros.password.reveal() == "demo"
    assert config.weather is not None and 36 < config.weather.latitude < 38.5
    raw = tomllib.loads((snap.out / "config.toml").read_text())
    for dropped in ("radio", "paging", "slack", "demo", "wled"):
        assert dropped not in raw


def test_manifest_never_holds_private_transforms(snap: Snap) -> None:
    text = (snap.out / "manifest.json").read_text()
    manifest = json.loads(text)
    assert manifest["window"]["start"].startswith("2025-06-0")
    for forbidden in ("2026", "shift", "centroid", "anchor", "rotation", "45.0", "-100.0"):
        assert forbidden not in text
    assert manifest["gate"]["findings"] == 0
    assert manifest["region"] == "Owens Valley, CA"


def test_no_excluded_path_in_output(snap: Snap) -> None:
    for pattern in excluded_paths():
        assert not list(snap.state.glob(pattern)), pattern


def test_independent_gate_rescan_is_clean(snap: Snap) -> None:
    raw = tomllib.loads(snap.config.read_text())
    window = (int(BUSY_START.timestamp() * 1e6), int(BUSY_END.timestamp() * 1e6))
    deny = snap.config.parent / "demo-denylist.txt"
    h = harvest(raw, snap.live, window, deny, load_dictionary())
    assert LeakGate(h).scan(snap.out).fatal == []
