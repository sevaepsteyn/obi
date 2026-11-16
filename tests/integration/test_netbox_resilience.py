"""T020 / T023: NetBox failure handling — total adapter mapping + clean CLI exits (US3).

Two layers: (a) ``NetboxSource._default_transport`` maps transport/HTTP failures to a
``SourceOutcome`` (never raises out); (b) `obi netbox inventory` reports the source-level
outcome with
guidance and exits cleanly (no traceback, no hang).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from obi.cli.main import app
from obi.core.drivers.netbox import NetboxError, NetboxSource
from obi.core.models import SourceOutcome
from tests.fakes import netbox_settings
from tests.integration._netbox import patch_transport, write_netbox_config

runner = CliRunner()


# --- (a) adapter-level failure -> outcome mapping (T023) ---------------------------------------


def _client_raising(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    class _FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> _FakeClient:
            return self

        def __exit__(self, *e: object) -> bool:
            return False

        def get(self, url: str, headers: dict[str, str] | None = None) -> Any:
            raise exc

    monkeypatch.setattr(httpx, "Client", _FakeClient)


def _http_status(monkeypatch: pytest.MonkeyPatch, code: int) -> None:
    class _Resp:
        status_code = code

        def raise_for_status(self) -> None:
            request = httpx.Request("GET", "https://nb/api/dcim/devices/")
            response = httpx.Response(code, request=request)
            raise httpx.HTTPStatusError(f"HTTP {code}", request=request, response=response)

        def json(self) -> Any:  # pragma: no cover - never reached (raise_for_status first)
            return {}

    class _FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> _FakeClient:
            return self

        def __exit__(self, *e: object) -> bool:
            return False

        def get(self, url: str, headers: dict[str, str] | None = None) -> _Resp:
            return _Resp()

    monkeypatch.setattr(httpx, "Client", _FakeClient)


@pytest.mark.parametrize(
    ("code", "outcome"),
    [
        (401, SourceOutcome.AUTH_FAILED),
        (403, SourceOutcome.AUTH_FAILED),
        (404, SourceOutcome.UNREACHABLE),
        (500, SourceOutcome.UNREACHABLE),
    ],
)
def test_http_status_maps_to_outcome(
    monkeypatch: pytest.MonkeyPatch, code: int, outcome: SourceOutcome
) -> None:
    _http_status(monkeypatch, code)
    report = NetboxSource(netbox_settings()).fetch(name_filter=None, timeout=1.0)
    assert report.outcome is outcome
    assert report.devices == []


def test_connect_error_is_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    _client_raising(monkeypatch, httpx.ConnectError("refused"))
    report = NetboxSource(netbox_settings()).fetch(name_filter=None, timeout=1.0)
    assert report.outcome is SourceOutcome.UNREACHABLE


def test_timeout_is_timed_out(monkeypatch: pytest.MonkeyPatch) -> None:
    _client_raising(monkeypatch, httpx.TimeoutException("slow"))
    report = NetboxSource(netbox_settings()).fetch(name_filter=None, timeout=1.0)
    assert report.outcome is SourceOutcome.TIMED_OUT


# --- (b) CLI reports the outcome and exits cleanly (T020) --------------------------------------


@pytest.mark.parametrize(
    ("outcome", "detail"),
    [
        (SourceOutcome.UNREACHABLE, "could not connect to NetBox"),
        (SourceOutcome.AUTH_FAILED, "NetBox rejected the API token (HTTP 401)"),
        (SourceOutcome.TIMED_OUT, "NetBox did not respond"),
    ],
)
def test_cli_reports_failure_and_exits_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: SourceOutcome, detail: str
) -> None:
    patch_transport(monkeypatch, error=NetboxError(outcome, detail))
    config = write_netbox_config(tmp_path)

    result = runner.invoke(app, ["netbox", "inventory", "--scope", str(config)])

    assert result.exit_code == 0, result.output  # clean exit, no traceback (FR-011, SC-005)
    assert result.exception is None
    assert outcome.value in result.output


def test_cli_failure_is_audited(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_transport(
        monkeypatch, error=NetboxError(SourceOutcome.AUTH_FAILED, "rejected token")
    )
    config = write_netbox_config(tmp_path)

    result = runner.invoke(app, ["netbox", "inventory", "--scope", str(config)])

    assert result.exit_code == 0, result.output
    audit = (tmp_path / "audit.jsonl").read_text()
    assert '"netbox_inventory"' in audit
    assert '"auth_failed"' in audit
