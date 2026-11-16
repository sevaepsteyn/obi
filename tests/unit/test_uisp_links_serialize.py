"""Unit tests: the UISP link-health serializer (feature 034 — T007, contracts/surfaces.md)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from obi.core.models import (
    SourceOutcome,
    UispLinkEnd,
    UispLinksReport,
    UispRadioHealth,
    UispWirelessLink,
)
from obi.core.serialize import uisp_links_report_to_dict, uisp_links_report_to_json

_T0 = datetime(2026, 7, 8, 12, 0, tzinfo=UTC)


def _report() -> UispLinksReport:
    healthy = UispWirelessLink(
        id="L1",
        end_a=UispLinkEnd(
            name="chestnut-gw",
            site="BRC - Chestnut",
            device_id="d1",
            radio=UispRadioHealth(
                status="active",
                signal_dbm=-65.0,
                frequency_mhz=5550.0,
                score=0.82,
                link_score=0.9,
                downlink_score=0.83,
                uplink_score=0.96,
                airtime=0.008,
                capacity_down_bps=208_000_000,
                capacity_up_bps=219_600_000,
            ),
        ),
        end_b=UispLinkEnd(name="chestnut-ptp-60", site=None, device_id="d2", radio=None),
        state="active",
        signal_dbm=-62.0,
        ssid="BM-Chestnut-airMAX",
        frequency_mhz=5550.0,
        distance_m=2822.0,
    )
    down = UispWirelessLink(
        id="L2",
        end_a=UispLinkEnd(name="noc-ptp-basswood-60"),
        end_b=UispLinkEnd(name=None),
        state="disconnected",
    )
    return UispLinksReport(
        controller="https://uisp.example.com",
        outcome=SourceOutcome.REACHABLE,
        links=[healthy, down],
        started_at=_T0,
        finished_at=_T0,
        name_filter=None,
        detail=None,
    )


def test_report_dict_shape_matches_the_contract() -> None:
    data = uisp_links_report_to_dict(_report())
    assert data["controller"] == "https://uisp.example.com"
    assert data["outcome"] == "reachable"
    assert data["counts"] == {"total": 2, "active": 1, "down": 1}
    healthy, down = data["links"]
    assert healthy["label"] == "chestnut-gw ↔ chestnut-ptp-60"
    assert healthy["signal_dbm"] == -62.0
    assert healthy["ends"][0]["radio"]["score"] == 0.82
    assert healthy["ends"][0]["radio"]["capacity_up_bps"] == 219_600_000
    assert healthy["ends"][1]["radio"] is None  # join miss serializes as null, link kept
    assert down["state"] == "disconnected"
    assert down["signal_dbm"] is None  # absent values are null, never fabricated
    assert down["label"] == "? ↔ noc-ptp-basswood-60"  # unnamed end renders the placeholder
    assert data["started_at"] == _T0.isoformat()


def test_json_round_trips() -> None:
    text = uisp_links_report_to_json(_report())
    assert json.loads(text) == uisp_links_report_to_dict(_report())
