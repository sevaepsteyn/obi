"""T010: InventorySource contract — read-only, total, bounded, no-secrets.

Run against the real ``UispController`` (with an injected transport) and the ``FakeSource``.
See ``contracts/inventory-source.md``.
"""

from __future__ import annotations

from typing import Any

import pytest

from obi.core.drivers.base import InventorySource, SourceReport
from obi.core.drivers.uisp import UispController, UispError
from obi.core.models import SourceOutcome
from tests.fakes import FakeSource, load_uisp_devices, uisp_settings

# A source MUST NOT expose any state-changing operation (Principle I).
WRITE_VERBS = (
    "set", "add", "remove", "update", "write", "exec", "create", "delete", "apply",
    "post", "put", "patch",
)


def _uisp(
    devices: list[dict[str, Any]] | None = None, *, raises: Exception | None = None
) -> UispController:
    def transport(timeout: float) -> list[dict[str, Any]]:
        if raises is not None:
            raise raises
        return list(devices or [])

    return UispController(uisp_settings(), transport=transport)


def test_both_implementations_satisfy_protocol() -> None:
    assert isinstance(_uisp(), InventorySource)
    assert isinstance(FakeSource([]), InventorySource)


@pytest.mark.parametrize("source", [_uisp(), FakeSource([])])
def test_source_exposes_no_write_methods(source: InventorySource) -> None:
    for verb in WRITE_VERBS:
        assert not hasattr(source, verb), f"{source.name} exposes write-like method {verb!r}"


def test_fetch_is_total_on_controller_error() -> None:
    report = _uisp(raises=UispError(SourceOutcome.AUTH_FAILED, "rejected token")).fetch(
        name_filter=None, timeout=1.0
    )
    assert isinstance(report, SourceReport)
    assert report.outcome is SourceOutcome.AUTH_FAILED
    assert report.devices == []


def test_fetch_is_total_on_unexpected_error() -> None:
    report = _uisp(raises=RuntimeError("boom")).fetch(name_filter=None, timeout=1.0)
    assert report.outcome is SourceOutcome.UNREACHABLE  # never raises out (FR-011)
    assert report.devices == []


def test_fetch_honors_timeout_argument() -> None:
    captured: dict[str, float] = {}

    def transport(timeout: float) -> list[dict[str, Any]]:
        captured["timeout"] = timeout
        return []

    UispController(uisp_settings(), transport=transport).fetch(name_filter=None, timeout=3.5)
    assert captured["timeout"] == 3.5


def test_returned_report_carries_no_secret() -> None:
    # uisp_settings() token is "UISP-TOKEN-supersecret"; it must not leak into returned data.
    report = _uisp(load_uisp_devices()).fetch(name_filter=None, timeout=1.0)
    assert "supersecret" not in repr(report)
