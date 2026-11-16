"""T016: graceful degradation + composite outcome across the two console surfaces (FR-007/8)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from obi.core.drivers.unifi_common import UnifiError
from obi.core.drivers.unifi_network import UnifiNetworkController
from obi.core.models import SourceOutcome
from tests.fakes import load_unifi_official, unifi_network_settings


def _raises(exc: Exception) -> Callable[[float], list[dict[str, Any]]]:
    def transport(timeout: float) -> list[dict[str, Any]]:
        raise exc

    return transport


def test_one_surface_down_is_reachable_partial() -> None:
    off = load_unifi_official()
    controller = UnifiNetworkController(
        unifi_network_settings(),
        official=lambda t: off,
        controller=_raises(UnifiError(SourceOutcome.UNREACHABLE, "controller API down")),
    )
    report = controller.fetch(name_filter=None, timeout=5.0)

    assert report.outcome is SourceOutcome.REACHABLE  # partial success, not a failure (FR-007)
    assert report.detail is not None and "controller API" in report.detail
    assert {s.device.identity for s in report.devices} == {"udm-ridge", "usw-core", "uap-shade"}
    # the controller-only device is gone (its surface was down), and health fields are noted
    udm = next(s for s in report.devices if s.device.identity == "udm-ridge")
    assert udm.free_memory_bytes is None
    assert any("controller API unavailable" in n for n in udm.notes)


def test_all_surfaces_auth_failed_reports_auth() -> None:
    controller = UnifiNetworkController(
        unifi_network_settings(),
        official=_raises(UnifiError(SourceOutcome.AUTH_FAILED, "integration API rejected")),
        controller=_raises(UnifiError(SourceOutcome.AUTH_FAILED, "controller API rejected")),
    )
    report = controller.fetch(name_filter=None, timeout=5.0)

    assert report.outcome is SourceOutcome.AUTH_FAILED  # shared reason wins
    assert report.devices == []
    assert report.detail is not None
    assert "integration API" in report.detail and "controller API" in report.detail


def test_mixed_failures_report_unreachable_naming_both() -> None:
    controller = UnifiNetworkController(
        unifi_network_settings(),
        official=_raises(UnifiError(SourceOutcome.AUTH_FAILED, "integration API bad key")),
        controller=_raises(UnifiError(SourceOutcome.TIMED_OUT, "controller API slow")),
    )
    report = controller.fetch(name_filter=None, timeout=5.0)

    assert report.outcome is SourceOutcome.UNREACHABLE  # different reasons → unreachable
    assert report.detail is not None
    assert "integration API bad key" in report.detail and "controller API slow" in report.detail


def test_single_configured_surface_failure_passes_through() -> None:
    controller = UnifiNetworkController(
        unifi_network_settings(token=None),
        official=_raises(UnifiError(SourceOutcome.TIMED_OUT, "integration API did not respond")),
    )
    report = controller.fetch(name_filter=None, timeout=5.0)

    assert report.outcome is SourceOutcome.TIMED_OUT  # the only configured surface's outcome
    assert report.devices == []
