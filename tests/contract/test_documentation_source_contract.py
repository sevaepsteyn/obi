"""T011: DocumentationSource contract — read-only, total, bounded, complete, no-secrets.

Run against the real ``NetboxSource`` (with an injected transport) and the ``FakeDocSource``.
See ``contracts/documentation-source.md``.
"""

from __future__ import annotations

from typing import Any

import pytest

from obi.core.drivers.base import DocumentationReport, DocumentationSource
from obi.core.drivers.netbox import NetboxError, NetboxSource
from obi.core.models import SourceOutcome
from tests.fakes import FakeDocSource, load_netbox_devices, netbox_settings

# A source MUST NOT expose any state-changing operation (Principle I).
WRITE_VERBS = (
    "set", "add", "remove", "update", "write", "exec", "create", "delete", "apply",
    "post", "put", "patch",
)


def _netbox(
    devices: list[dict[str, Any]] | None = None, *, raises: Exception | None = None
) -> NetboxSource:
    def transport(timeout: float) -> list[dict[str, Any]]:
        if raises is not None:
            raise raises
        return list(devices or [])

    return NetboxSource(netbox_settings(), transport=transport)


def test_both_implementations_satisfy_protocol() -> None:
    assert isinstance(_netbox(), DocumentationSource)
    assert isinstance(FakeDocSource([]), DocumentationSource)


@pytest.mark.parametrize("source", [_netbox(), FakeDocSource([])])
def test_source_exposes_no_write_methods(source: DocumentationSource) -> None:
    for verb in WRITE_VERBS:
        assert not hasattr(source, verb), f"{source.name} exposes write-like method {verb!r}"


def test_fetch_is_total_on_source_error() -> None:
    report = _netbox(raises=NetboxError(SourceOutcome.AUTH_FAILED, "rejected token")).fetch(
        name_filter=None, timeout=1.0
    )
    assert isinstance(report, DocumentationReport)
    assert report.outcome is SourceOutcome.AUTH_FAILED
    assert report.devices == []


def test_fetch_is_total_on_unexpected_error() -> None:
    report = _netbox(raises=RuntimeError("boom")).fetch(name_filter=None, timeout=1.0)
    assert report.outcome is SourceOutcome.UNREACHABLE  # never raises out (FR-011)
    assert report.devices == []


def test_fetch_honors_timeout_argument() -> None:
    captured: dict[str, float] = {}

    def transport(timeout: float) -> list[dict[str, Any]]:
        captured["timeout"] = timeout
        return []

    NetboxSource(netbox_settings(), transport=transport).fetch(name_filter=None, timeout=3.5)
    assert captured["timeout"] == 3.5


def test_fetch_is_complete_over_all_devices() -> None:
    # Completeness: every documented device the transport yields is returned (no truncation).
    report = _netbox(load_netbox_devices()).fetch(name_filter=None, timeout=1.0)
    assert len(report.devices) == len(load_netbox_devices()) == 6


def test_returned_report_carries_no_secret() -> None:
    # netbox_settings() token is "NETBOX-TOKEN-supersecret"; it must not leak into returned data.
    report = _netbox(load_netbox_devices()).fetch(name_filter=None, timeout=1.0)
    assert "supersecret" not in repr(report)


def test_documented_record_has_no_live_health_fields() -> None:
    report = _netbox(load_netbox_devices()).fetch(name_filter=None, timeout=1.0)
    for device in report.devices:  # documentation, not live health (FR-004, SC-008)
        for forbidden in (
            "cpu_load_percent", "free_memory_bytes", "uptime_seconds", "reachability",
        ):
            assert not hasattr(device, forbidden)
