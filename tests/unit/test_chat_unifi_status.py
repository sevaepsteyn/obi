"""T023: the `_unifi_status` handler renders the live UniFi fleet, degrades cleanly, and keeps
the console credentials out of every surface (feature 011).

Feature 105 unregisters the tool (no surface offers it), so these tests exercise the handler
DIRECTLY — it stays behaviorally pinned for a future re-enable."""

from __future__ import annotations

from pathlib import Path

from obi.core.audit import AuditTrail
from obi.core.models import Device, DeviceStatus, Reachability, SourceOutcome
from obi.core.toolset import ToolContext, _unifi_status
from tests.fakes import FakeSource, chat_config, unifi_network_settings


def _ctx(tmp_path: Path, source: FakeSource | None, *, unifi: bool = True) -> ToolContext:
    config = chat_config(
        tmp_path, routeros=False, unifi_network=unifi_network_settings() if unifi else None
    )
    return ToolContext(
        config=config,
        driver=None,
        unifi_network_source=source,
        audit=AuditTrail(config.audit_path),
        question="q",
    )


def _run(ctx: ToolContext, args: dict[str, object] | None = None) -> str:
    return _unifi_status(ctx, args or {})


def _device(name: str, addr: str, reach: Reachability, **kw: object) -> DeviceStatus:
    return DeviceStatus(Device(name, addr), reach, **kw)  # type: ignore[arg-type]


def test_lists_fleet_vendor_labeled(tmp_path: Path) -> None:
    up = _device(
        "udm-ridge", "10.40.0.1", Reachability.REACHABLE,
        uptime_seconds=3600, cpu_load_percent=5.0, free_memory_bytes=100_000_000,
    )
    down = _device("uap-shade", "10.40.0.3", Reachability.UNREACHABLE, notes=["offline"])

    out = _run(_ctx(tmp_path, FakeSource([up, down])))

    assert "UniFi udm-ridge" in out and "reachable" in out
    assert "UniFi uap-shade" in out and "unreachable" in out


def test_empty_fleet(tmp_path: Path) -> None:
    out = _run(_ctx(tmp_path, FakeSource([])))
    assert "No UniFi devices" in out


def test_console_down_degrades_cleanly(tmp_path: Path) -> None:
    source = FakeSource([], outcome=SourceOutcome.UNREACHABLE, detail="connection refused")

    out = _run(_ctx(tmp_path, source))

    assert "unreachable" in out.lower()
    assert "connection refused" in out  # guided, non-secret detail surfaced — never a traceback


def test_name_filter_passed_through(tmp_path: Path) -> None:
    a = _device("udm-ridge", "10.40.0.1", Reachability.REACHABLE, uptime_seconds=1)
    b = _device("uap-shade", "10.40.0.3", Reachability.REACHABLE, uptime_seconds=1)
    source = FakeSource([a, b])

    out = _run(_ctx(tmp_path, source), {"name_contains": "ridge"})

    assert "udm-ridge" in out and "uap-shade" not in out
    assert source.calls[-1][0] == "ridge"  # the filter reached the console read


def test_no_console_configured(tmp_path: Path) -> None:
    out = _run(_ctx(tmp_path, None, unifi=False))
    assert "No UniFi Network console is configured" in out


def test_credentials_never_in_output_or_audit(tmp_path: Path) -> None:
    up = _device("udm-ridge", "10.40.0.1", Reachability.REACHABLE, uptime_seconds=1)
    ctx = _ctx(tmp_path, FakeSource([up]))

    out = _run(ctx)

    assert "UNIFI-KEY-supersecret" not in out and "UNIFI-TOKEN-supersecret" not in out
    audit = (tmp_path / "audit.jsonl").read_text()
    assert "UNIFI-KEY-supersecret" not in audit and "UNIFI-TOKEN-supersecret" not in audit
    assert '"unifi_status"' in audit  # the read WAS audited (FR-010)
