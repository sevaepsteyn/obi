"""Unit (US1): the `_uisp_status` handler renders the live UISP fleet, degrades cleanly, and
keeps the controller token out of every surface (feature 006)."""

from __future__ import annotations

from pathlib import Path

from obi.core.audit import AuditTrail
from obi.core.models import Device, DeviceStatus, Reachability, SourceOutcome
from obi.core.toolset import ToolContext, build_dispatch, build_tools
from tests.fakes import FakeSource, chat_config, uisp_settings


def _ctx(tmp_path: Path, source: FakeSource | None, *, uisp: bool = True) -> ToolContext:
    config = chat_config(tmp_path, routeros=False, uisp=uisp_settings() if uisp else None)
    return ToolContext(
        config=config,
        driver=None,
        uisp_source=source,
        audit=AuditTrail(config.audit_path),
        question="q",
    )


def _run(ctx: ToolContext, args: dict[str, object] | None = None) -> str:
    return build_dispatch(ctx, build_tools())["uisp_status"](args or {})


def _device(name: str, addr: str, reach: Reachability, **kw: object) -> DeviceStatus:
    return DeviceStatus(Device(name, addr), reach, **kw)  # type: ignore[arg-type]


def test_lists_fleet_vendor_labeled(tmp_path: Path) -> None:
    up = _device(
        "ridge-ap", "10.9.0.5", Reachability.REACHABLE,
        uptime_seconds=3600, cpu_load_percent=5.0, free_memory_bytes=100_000_000,
    )
    down = _device("valley-ap", "10.9.0.6", Reachability.UNREACHABLE, notes=["offline"])

    out = _run(_ctx(tmp_path, FakeSource([up, down])))

    assert "UISP ridge-ap" in out and "reachable" in out
    assert "UISP valley-ap" in out and "unreachable" in out


def test_empty_fleet(tmp_path: Path) -> None:
    out = _run(_ctx(tmp_path, FakeSource([])))
    assert "No UISP devices" in out


def test_controller_down_degrades_cleanly(tmp_path: Path) -> None:
    source = FakeSource([], outcome=SourceOutcome.UNREACHABLE, detail="connection refused")

    out = _run(_ctx(tmp_path, source))

    assert "unreachable" in out.lower()
    assert "connection refused" in out  # guided, non-secret detail surfaced — never a traceback


def test_name_filter_passed_through(tmp_path: Path) -> None:
    a = _device("ridge-ap", "10.9.0.5", Reachability.REACHABLE, uptime_seconds=1)
    b = _device("valley-ap", "10.9.0.6", Reachability.REACHABLE, uptime_seconds=1)
    source = FakeSource([a, b])

    out = _run(_ctx(tmp_path, source), {"name_contains": "ridge"})

    assert "ridge-ap" in out and "valley-ap" not in out
    assert source.calls[-1][0] == "ridge"  # the filter reached the controller read


def test_no_controller_configured(tmp_path: Path) -> None:
    out = _run(_ctx(tmp_path, None, uisp=False))
    assert "No UISP controller is configured" in out


def test_token_never_in_output_or_audit(tmp_path: Path) -> None:
    up = _device("ridge-ap", "10.9.0.5", Reachability.REACHABLE, uptime_seconds=1)
    ctx = _ctx(tmp_path, FakeSource([up]))

    out = _run(ctx)

    assert "UISP-TOKEN-supersecret" not in out
    assert "UISP-TOKEN-supersecret" not in (tmp_path / "audit.jsonl").read_text()
