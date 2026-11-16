"""T005: InventorySource contract for the UniFi adapter — read-only, total, bounded, no-secrets.

Run against the real ``UnifiNetworkController`` with an injected transport. See
``contracts/inventory_source.md``. (UISP's coverage of ``FakeSource`` lives in
``test_inventory_source_contract.py``; this file pins the same protocol for UniFi.)
"""

from __future__ import annotations

from typing import Any

from obi.core.drivers.base import InventorySource, SourceReport
from obi.core.drivers.unifi_common import UnifiError
from obi.core.drivers.unifi_network import UnifiNetworkController
from obi.core.models import SourceOutcome
from tests.fakes import load_unifi_official, unifi_network_settings

# A source MUST NOT expose any state-changing operation (Principle I).
WRITE_VERBS = (
    "set", "add", "remove", "update", "write", "exec", "create", "delete", "apply",
    "post", "put", "patch", "reboot", "restart", "adopt",
)


def _unifi(
    devices: list[dict[str, Any]] | None = None, *, raises: Exception | None = None
) -> UnifiNetworkController:
    def official(timeout: float) -> list[dict[str, Any]]:
        if raises is not None:
            raise raises
        return list(devices or [])

    # Only the official surface configured (token=None) so a single-surface outcome passes through.
    return UnifiNetworkController(unifi_network_settings(token=None), official=official)


def test_satisfies_inventory_source_protocol() -> None:
    assert isinstance(_unifi(), InventorySource)


def test_exposes_no_write_methods() -> None:
    source = _unifi()
    for verb in WRITE_VERBS:
        assert not hasattr(source, verb), f"unifi exposes write-like method {verb!r}"


def test_fetch_is_total_on_surface_error() -> None:
    report = _unifi(raises=UnifiError(SourceOutcome.AUTH_FAILED, "rejected key")).fetch(
        name_filter=None, timeout=1.0
    )
    assert isinstance(report, SourceReport)
    assert report.outcome is SourceOutcome.AUTH_FAILED
    assert report.devices == []


def test_fetch_is_total_on_unexpected_error() -> None:
    report = _unifi(raises=RuntimeError("boom")).fetch(name_filter=None, timeout=1.0)
    assert report.outcome is SourceOutcome.UNREACHABLE  # never raises out (FR-003)
    assert report.devices == []


def test_fetch_honors_timeout_argument() -> None:
    captured: dict[str, float] = {}

    def official(timeout: float) -> list[dict[str, Any]]:
        captured["timeout"] = timeout
        return []

    # Single configured surface gets the whole budget.
    UnifiNetworkController(unifi_network_settings(token=None), official=official).fetch(
        name_filter=None, timeout=3.5
    )
    assert captured["timeout"] == 3.5


def test_split_budget_stays_within_total() -> None:
    seen: dict[str, float] = {}

    def official(timeout: float) -> list[dict[str, Any]]:
        seen["official"] = timeout
        return []

    def controller(timeout: float) -> list[dict[str, Any]]:
        seen["controller"] = timeout
        return []

    UnifiNetworkController(
        unifi_network_settings(), official=official, controller=controller
    ).fetch(name_filter=None, timeout=10.0)
    assert seen["official"] <= 10.0 and seen["controller"] <= 10.0
    assert seen["official"] + seen["controller"] <= 10.0 + 1e-9  # bounded by the single budget


def test_returned_report_carries_no_secret() -> None:
    report = _unifi(load_unifi_official()).fetch(name_filter=None, timeout=1.0)
    assert "supersecret" not in repr(report)
