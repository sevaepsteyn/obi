"""Unit (feature 135): the maintenance matcher, driven by the LIVE 2026-10-14 name list.

Contract invariants M1 (never hides a real outage), M5 (whole-token sites), M6 (a UISP flag
does not spread), M7 (fail toward alarming).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from obi.core.config import MaintenanceSettings
from obi.core.maintenance import (
    Maintenance,
    MaintenanceSummary,
    normalise,
    read_radio_maintenance,
)

FIXTURE = Path(__file__).parent.parent / "fixtures" / "maintenance" / "live_2026-10-14.json"
LIVE = json.loads(FIXTURE.read_text(encoding="utf-8"))

DECLARED = MaintenanceSettings(
    sites=("wingnut", "manzanita", "noc", "magnolia"),
    uisp_sites=("brc - business park", "brc - noc", "brc - cottonwood - mrs", "wingnut"),
    signals=("radio", "mesh", "lighting"),
    reason="post-burn teardown 2026-10",
)
FLAGGED = [d["identity"] for d in LIVE["uisp_devices"] if d["maintenance"]]
LIVE_M = Maintenance.build(DECLARED, FLAGGED)


def test_fixture_shape() -> None:
    assert len(FLAGGED) == 21
    assert "rowan-ptp-joshua-st-05" in LIVE["fleet"]


@pytest.mark.parametrize(
    ("name", "full", "tokens"),
    [
        (
            "tamarack-ptp-magnolia-05 MIDDLE",
            "tamarack-ptp-magnolia-05",
            ("tamarack", "ptp", "magnolia", "05"),
        ),
        (
            "manzanita-ptp-basswood-70.net.example.net",
            "manzanita-ptp-basswood-70",
            ("manzanita", "ptp", "basswood", "70"),
        ),
        ("NOC-GW", "noc-gw", ("noc", "gw")),
        ("172.18.112.1", "172.18.112.1", ("172.18.112.1",)),
        ("", "", ()),
    ],
)
def test_normalise(name: str, full: str, tokens: tuple[str, ...]) -> None:
    assert normalise(name) == (full, tokens)


# --- M5: whole-token site matching ------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "noc-gw",
        "noc-ap-birch",
        "birch-ptp-noc-60",
        "magnolia-ap",
        "wingnut-gw",
        "manzanita-ptp-basswood-70.net.example.net",
        "manzanita-gw",
    ],
)
def test_site_tokens_match_devices(name: str) -> None:
    assert LIVE_M.device(name) == "config: post-burn teardown 2026-10"


@pytest.mark.parametrize("name", ["nocturne-gw", "cottonwood-gw", "tamarack-gw", "basswood-gw"])
def test_site_tokens_never_substring(name: str) -> None:
    m = Maintenance.build(DECLARED)
    assert m.device(name) is None


@pytest.mark.parametrize(
    ("router", "target"),
    [
        ("tamarack-gw", "tamarack-manzanita-11"),
        ("tamarack-gw", "tamarack-noc-11"),
        ("tamarack-gw", "tamarack-vtep-magnolia"),
        ("basswood-gw", "basswood-manzanita-70"),
        ("noc-gw", None),  # poll-failed row: the router alone
    ],
)
def test_netwatch_toward_or_from_site(router: str, target: str | None) -> None:
    assert Maintenance.build(DECLARED).link(router, target) is not None


def test_netwatch_unrelated_probes_stay_live() -> None:
    m = Maintenance.build(DECLARED)
    for router, target in LIVE["netwatch_targets"]:
        tokens = set(normalise(router)[1]) | set(normalise(target)[1])
        if not tokens & set(DECLARED.sites):
            assert m.link(router, target) is None, (router, target)


# --- M1: real outages are untouched ----------------------------------------------------------


def test_joshua_matches_nothing() -> None:
    assert LIVE_M.device("rowan-ptp-joshua-st-05") is None
    assert LIVE_M.link("rowan-ptp-joshua-st-05") is None


def test_live_fleet_partition() -> None:
    live = [n for n in LIVE["fleet"] if LIVE_M.device(n) is None]
    assert "rowan-ptp-joshua-st-05" in live
    assert "tamarack-gw" in live and "zelkova-gw" in live
    assert not any(n.startswith(("magnolia-", "manzanita-", "noc-", "wingnut-")) for n in live)


# --- M6: UISP flag is per-device, plus the ptp-less probe alias --------------------------------


def test_uisp_flag_matches_identity_and_alias_only() -> None:
    m = Maintenance.build(None, FLAGGED)
    assert m.device("tamarack-ptp-manzanita-11") == "uisp"
    assert m.device("tamarack-ptp-magnolia-05 MIDDLE") == "uisp"
    assert m.device("yew-ptp-basswood-05") == "uisp"
    assert m.link("tamarack-gw", "tamarack-manzanita-11") == "uisp"
    # the alias is for probe names only — never a device identity
    assert m.device("tamarack-manzanita-11") is None
    # a flagged radio never puts its site's gateway in maintenance
    assert m.device("manzanita-gw") is None
    assert m.device("tamarack-gw") is None
    assert m.device("rowan-ptp-joshua-st-05") is None


def test_config_label_wins_over_uisp() -> None:
    assert LIVE_M.device("noc-ptp-manzanita-60") == "config: post-burn teardown 2026-10"
    assert (
        Maintenance.build(MaintenanceSettings(sites=("noc",)), FLAGGED).device(
            "yew-ptp-basswood-05"
        )
        == "uisp"
    )
    assert Maintenance.build(MaintenanceSettings(sites=("noc",))).device("noc-gw") == "config"


def test_device_exact_match() -> None:
    m = Maintenance.build(MaintenanceSettings(devices=("rowan-gw",)))
    assert m.device("Rowan-GW.net.example") == "config"
    assert m.device("rowan-gw-2") is None


# --- UISP site names, log hosts, signals -------------------------------------------------------


def test_uisp_site_exact_casefold_collapsed() -> None:
    assert LIVE_M.uisp_site("BRC  - Business Park") is not None
    assert LIVE_M.uisp_site("BRC - Business") is None
    assert LIVE_M.uisp_site("Tamarack") is None


def test_log_host() -> None:
    assert LIVE_M.log_host("BRC - Cottonwood - MRS") is not None
    assert LIVE_M.log_host("magnolia-gw") is not None
    assert LIVE_M.log_host("Tamarack") is None
    assert LIVE_M.log_host("Rowan - House") is None
    assert LIVE_M.log_host(None) is None


def test_signals() -> None:
    assert LIVE_M.signal("radio") is not None
    assert LIVE_M.signal("mesh") is not None
    assert LIVE_M.signal("fleet") is None


# --- M7: empty means nothing ---------------------------------------------------------------------


def test_empty_matcher_matches_nothing() -> None:
    m = Maintenance.build()
    assert m.is_empty
    for name in LIVE["fleet"]:
        assert m.device(name) is None
    assert m.link("tamarack-gw", "tamarack-manzanita-11") is None
    assert m.signal("radio") is None
    assert m.log_host("BRC - NOC") is None


# --- typo detection, fingerprint, summary --------------------------------------------------------


def test_unmatched_flags_typos() -> None:
    m = Maintenance.build(
        MaintenanceSettings(
            sites=("shorline", "magnolia"),
            devices=("nope-gw",),
            uisp_sites=("brc - noc", "brc - nope"),
        )
    )
    seen = LIVE["fleet"] + [t for _, t in LIVE["netwatch_targets"]]
    assert m.unmatched(seen, LIVE["uisp_site_names"]) == [
        "site shorline",
        "device nope-gw",
        "UISP site brc - nope",
    ]
    assert LIVE_M.unmatched(seen, LIVE["uisp_site_names"]) == []


def test_fingerprint_stable_and_sensitive() -> None:
    a = Maintenance.build(MaintenanceSettings(sites=("manzanita", "magnolia")), ["x-ptp-y"])
    b = Maintenance.build(MaintenanceSettings(sites=("magnolia", "manzanita")), ["X-PTP-Y"])
    c = Maintenance.build(MaintenanceSettings(sites=("magnolia",)), ["x-ptp-y"])
    assert a.fingerprint() == b.fingerprint()
    assert a.fingerprint() != c.fingerprint()


def test_summary_line_and_json_round_trip() -> None:
    summary = LIVE_M.summary(items=30, unmatched=["site shorline"])
    assert summary.uisp_flagged == 21
    line = summary.line()
    assert line.startswith("sites wingnut, manzanita, noc, magnolia;")
    assert "signals radio, mesh, lighting" in line
    assert "21 UISP-flagged radios" in line
    assert "— post-burn teardown 2026-10" in line
    assert "matches nothing: site shorline" in line
    assert MaintenanceSummary.from_json(summary.to_json()) == summary
    assert MaintenanceSummary.from_json(None) is None


# --- radio runtime reader (fail toward alarming) -------------------------------------------------


def test_read_radio_maintenance(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[maintenance]\nsignals = ["radio"]\nreason = "teardown"\n')
    assert read_radio_maintenance(path) == "config: teardown"
    path.write_text('[maintenance]\nsignals = ["mesh"]\n')
    assert read_radio_maintenance(path) is None
    path.write_text("")
    assert read_radio_maintenance(path) is None
    path.write_text('[maintenance]\nsignals = ["radios"]\n')  # invalid ⇒ alarm
    assert read_radio_maintenance(path) is None
    path.write_text("not = [toml")
    assert read_radio_maintenance(path) is None
    assert read_radio_maintenance(tmp_path / "missing.toml") is None
