"""Unit tests: the per-tool snapshot builders + atomic writer (feature 040).

The parity rule under test: a snapshot's ``data`` is byte-for-byte the tool's existing CLI
serializer output; the wrapper adds only ``tool`` and ``generated_at``. Failure semantics:
a source that is down AS A WHOLE raises (the caller keeps the previous file); per-device
unreachability is data and snapshots fine.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from obi.core import snapshot as snap
from obi.core.audit import AuditTrail
from obi.core.drivers.uisp import UispController, UispError
from obi.core.engine import uisp_links, uisp_status
from obi.core.inventory import Inventory, InventoryEntry
from obi.core.models import DiscoveryScope, DiscoveryState, SourceOutcome
from obi.core.serialize import uisp_links_report_to_dict, uisp_report_to_dict
from tests.fakes import (
    FakeBehavior,
    FakeDriver,
    load_uisp_links_fixture,
    siklu_driver_from_fixture,
    siklu_settings,
    uisp_settings,
)

_UISP_FIXTURES = {
    "/nms/api/v2.1/data-links": load_uisp_links_fixture("data-links"),
    "/nms/api/v2.1/devices": load_uisp_links_fixture("devices"),
}


def _uisp_transport(path: str, timeout: float) -> list[dict[str, Any]]:
    return _UISP_FIXTURES[path]


def _failing_transport(path: str, timeout: float) -> list[dict[str, Any]]:
    raise UispError(SourceOutcome.AUTH_FAILED, "controller rejected the API token (HTTP 401)")


# fetch() (the device list) rides the plain transport seam, not the geo one.
def _uisp_devices_transport(timeout: float) -> list[dict[str, Any]]:
    return _UISP_FIXTURES["/nms/api/v2.1/devices"]


def _failing_devices_transport(timeout: float) -> list[dict[str, Any]]:
    raise UispError(SourceOutcome.AUTH_FAILED, "controller rejected the API token (HTTP 401)")


def _audit(tmp_path: Path) -> AuditTrail:
    return AuditTrail(tmp_path / "audit.jsonl")


# --- the wrapper + writer ------------------------------------------------------------------------


def test_write_snapshot_is_atomic_and_named_by_tool(tmp_path: Path) -> None:
    payload = {"tool": "uisp-links", "generated_at": "2026-07-09T00:00:00+00:00", "data": {}}
    out = snap.write_snapshot(tmp_path / "snapshots", payload)
    assert out == tmp_path / "snapshots" / "uisp-links.json"
    assert json.loads(out.read_text()) == payload
    assert not list((tmp_path / "snapshots").glob("*.tmp"))  # no partials left behind


def test_tool_names_are_the_catalog() -> None:
    assert snap.SNAPSHOT_TOOL_NAMES == (
        "status",
        "uisp",
        "uisp-links",
        "siklu",
        "unifi",
        "netbox-drift",
        "ospf",  # feature 052
    )


# --- uisp (devices, feature 043): parity + filters + whole-source failure -------------------------


def test_uisp_snapshot_matches_the_cli_serializer(tmp_path: Path) -> None:
    controller = UispController(uisp_settings(), transport=_uisp_devices_transport)
    built = snap.build_uisp(
        controller,
        audit=_audit(tmp_path),
        controller="https://uisp.example.com",
        name_filter=None,
        ignore_names=(),
        timeout=5.0,
        reason="test",
    )
    assert built["tool"] == "uisp"
    assert built["generated_at"].endswith("+00:00")  # UTC ISO

    # Parity: identical engine inputs produce the identical serializer dict.
    reference = uisp_report_to_dict(
        uisp_status(
            UispController(uisp_settings(), transport=_uisp_devices_transport),
            audit=_audit(tmp_path),
            controller="https://uisp.example.com",
            timeout=5.0,
            reason="test",
        )
    )
    built["data"].pop("started_at"), built["data"].pop("finished_at")
    reference.pop("started_at"), reference.pop("finished_at")
    for payload in (built["data"], reference):  # per-device stamps differ between the two runs
        for device in payload["devices"]:
            device.pop("gathered_at")
    assert built["data"] == reference
    assert built["data"]["counts"]["total"] > 0
    assert all(device["vendor"] == "uisp" for device in built["data"]["devices"])


def test_uisp_snapshot_honors_ignore_names(tmp_path: Path) -> None:
    full = snap.build_uisp(
        UispController(uisp_settings(), transport=_uisp_devices_transport),
        audit=_audit(tmp_path),
        controller="c",
        name_filter=None,
        ignore_names=(),
        timeout=5.0,
        reason="test",
    )
    ignored = next(d["identity"] for d in full["data"]["devices"] if d["identity"])
    narrowed = snap.build_uisp(
        UispController(uisp_settings(), transport=_uisp_devices_transport),
        audit=_audit(tmp_path),
        controller="c",
        name_filter=None,
        ignore_names=(ignored,),
        timeout=5.0,
        reason="test",
    )
    assert narrowed["data"]["counts"]["total"] < full["data"]["counts"]["total"]
    assert ignored not in {device["identity"] for device in narrowed["data"]["devices"]}


def test_uisp_device_controller_failure_raises_and_preserves_last_good(tmp_path: Path) -> None:
    out_dir = tmp_path / "snapshots"
    previous = {"tool": "uisp", "generated_at": "old", "data": {"marker": 1}}
    snap.write_snapshot(out_dir, previous)

    with pytest.raises(snap.SnapshotError, match="auth_failed"):
        snap.build_uisp(
            UispController(uisp_settings(), transport=_failing_devices_transport),
            audit=_audit(tmp_path),
            controller="c",
            name_filter=None,
            ignore_names=(),
            timeout=5.0,
            reason="test",
        )
    # The caller never writes on failure — the previous snapshot survives untouched.
    assert json.loads((out_dir / "uisp.json").read_text()) == previous


# --- uisp-links: parity + whole-source failure ----------------------------------------------------


def test_uisp_links_snapshot_matches_the_cli_serializer(tmp_path: Path) -> None:
    controller = UispController(uisp_settings(), geo_transport=_uisp_transport)
    built = snap.build_uisp_links(
        controller,
        audit=_audit(tmp_path),
        controller="https://uisp.example.com",
        name_filter=None,
        ignore_names=(),
        timeout=5.0,
        reason="test",
    )
    assert built["tool"] == "uisp-links"
    assert built["generated_at"].endswith("+00:00")  # UTC ISO

    # Parity: identical engine inputs produce the identical serializer dict.
    reference = uisp_links_report_to_dict(
        uisp_links(
            UispController(uisp_settings(), geo_transport=_uisp_transport),
            audit=_audit(tmp_path),
            controller="https://uisp.example.com",
            timeout=5.0,
            reason="test",
        )
    )
    built["data"].pop("started_at"), built["data"].pop("finished_at")
    reference.pop("started_at"), reference.pop("finished_at")
    assert built["data"] == reference
    assert built["data"]["counts"]["total"] == 40


def test_uisp_controller_failure_raises_and_preserves_last_good(tmp_path: Path) -> None:
    out_dir = tmp_path / "snapshots"
    previous = {"tool": "uisp-links", "generated_at": "old", "data": {"marker": 1}}
    snap.write_snapshot(out_dir, previous)

    controller = UispController(uisp_settings(), geo_transport=_failing_transport)
    with pytest.raises(snap.SnapshotError, match="auth_failed"):
        snap.build_uisp_links(
            controller,
            audit=_audit(tmp_path),
            controller="c",
            name_filter=None,
            ignore_names=(),
            timeout=5.0,
            reason="test",
        )
    # The caller never writes on failure — the previous snapshot survives untouched.
    assert json.loads((out_dir / "uisp-links.json").read_text()) == previous


# --- status: per-device unreachability is data, not failure ---------------------------------------


def test_status_snapshot_builds_with_unreachable_devices(tmp_path: Path) -> None:
    inventory = Inventory(
        scope=DiscoveryScope(seeds=["172.25.110.1"]),
        entries=[
            InventoryEntry("172.25.110.1", "gw", DiscoveryState.IDENTIFIED, provenance="seed"),
            InventoryEntry("172.25.110.9", "dark", DiscoveryState.IDENTIFIED, provenance="seed"),
        ],
    )
    driver = FakeDriver({
        "172.25.110.1": FakeBehavior(),
        "172.25.110.9": FakeBehavior(delay=1.0),  # slower than the timeout -> times out
    })
    built = snap.build_status(
        inventory, driver,
        audit=_audit(tmp_path), timeout=0.2, concurrency=4, reason="test",
    )
    assert built["tool"] == "status"
    assert built["data"]["counts"]["total"] == 2
    assert built["data"]["counts"]["reachable"] == 1  # degraded fleet still snapshots
    assert built["data"]["counts"]["timed_out"] == 1


# --- siklu: fixture-driven build ------------------------------------------------------------------


def test_siklu_snapshot_carries_radios_and_links(tmp_path: Path) -> None:
    driver, _sessions = siklu_driver_from_fixture(
        "tamarack-ptp-basswood-70", "basswood-ptp-tamarack-70"
    )
    settings = siklu_settings(hosts=["172.18.89.130", "172.18.83.224"])
    built = snap.build_siklu(driver, settings, audit=_audit(tmp_path), reason="test")
    assert built["tool"] == "siklu"
    assert built["data"]["counts"]["total"] == len(settings.hosts)
    assert "radios" in built["data"] and "links" in built["data"]
