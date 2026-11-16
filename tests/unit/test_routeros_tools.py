"""Unit (feature 028): the routeros_query / routeros_ping / routeros_traceroute chat handlers.

Target resolution, the pre-flight read-only refusal (no device contact), where/fields pass-through,
redaction, the size cap, count clamping, and auditing — all via a fake MikrotikDriver, no device.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from obi.core.audit import AuditTrail
from obi.core.drivers.mikrotik import (
    LiveReadResult,
    MikrotikDriver,
    PingProbe,
    PingResult,
    TraceHop,
    TraceResult,
)
from obi.core.inventory import InventoryEntry
from obi.core.toolset import ToolContext, build_dispatch, build_tools
from tests.fakes import chat_config, save_inventory, settings

_REDACTED = "<redacted-by-obi>"


class FakeMikrotikDriver(MikrotikDriver):
    """A MikrotikDriver whose live methods return canned results and record their calls."""

    def __init__(
        self,
        *,
        read: LiveReadResult | None = None,
        ping: PingResult | None = None,
        trace: TraceResult | None = None,
    ) -> None:
        super().__init__(settings())
        self._read = read
        self._ping = ping
        self._trace = trace
        self.calls: list[Any] = []

    def read_path(  # type: ignore[override]
        self,
        address: str,
        segments: Any,
        *,
        where: Any = (),
        fields: Any = (),
        timeout: float,
        max_records: int,
    ) -> LiveReadResult:
        self.calls.append(
            ("read", address, tuple(segments), tuple(where), tuple(fields), max_records)
        )
        assert self._read is not None
        return self._read

    def ping(  # type: ignore[override]
        self, address: str, destination: str, *, count: int, timeout: float
    ) -> PingResult:
        self.calls.append(("ping", address, destination, count))
        assert self._ping is not None
        return self._ping

    def traceroute(  # type: ignore[override]
        self, address: str, destination: str, *, max_probes: int, deadline_s: float, timeout: float
    ) -> TraceResult:
        self.calls.append(("trace", address, destination, max_probes))
        assert self._trace is not None
        return self._trace


def _ctx(tmp_path: Path, driver: MikrotikDriver | None) -> ToolContext:
    config = chat_config(tmp_path)
    save_inventory(
        config.inventory_path, [InventoryEntry(address="172.18.84.1", identity="core-gw-1")]
    )
    return ToolContext(
        config=config, driver=driver, audit=AuditTrail(config.audit_path), question="q"
    )


def _run(ctx: ToolContext, tool: str, args: dict[str, Any]) -> str:
    return build_dispatch(ctx, build_tools())[tool](args)


def _audit_rows(ctx: ToolContext) -> list[dict[str, Any]]:
    path = ctx.config.audit_path
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# --- routeros_query --------------------------------------------------------------------------


def test_query_renders_rows_and_audits_ok(tmp_path: Path) -> None:
    driver = FakeMikrotikDriver(
        read=LiveReadResult(records=[{"name": "ether1", "running": "true"}])
    )
    ctx = _ctx(tmp_path, driver)
    out = _run(ctx, "routeros_query", {"device": "core-gw-1", "path": "interface"})
    assert "ether1" in out
    assert "core-gw-1" in out
    rows = [r for r in _audit_rows(ctx) if r["action"] == "routeros_query"]
    assert rows and rows[-1]["outcome"] == "ok"


def test_query_unknown_device_is_refused(tmp_path: Path) -> None:
    driver = FakeMikrotikDriver(read=LiveReadResult(records=[]))
    ctx = _ctx(tmp_path, driver)
    out = _run(ctx, "routeros_query", {"device": "nope", "path": "interface"})
    assert "No device named" in out
    assert driver.calls == []  # never contacted a device
    rows = [r for r in _audit_rows(ctx) if r["action"] == "routeros_query"]
    assert rows and rows[-1]["outcome"] == "error"


def test_query_command_path_refused_preflight(tmp_path: Path) -> None:
    driver = FakeMikrotikDriver(read=LiveReadResult(records=[]))
    ctx = _ctx(tmp_path, driver)
    out = _run(ctx, "routeros_query", {"device": "core-gw-1", "path": "system/reboot"})
    assert "read-only" in out.lower() or "refused" in out.lower()
    assert driver.calls == []  # refused BEFORE any device contact
    rows = [r for r in _audit_rows(ctx) if r["action"] == "routeros_query"]
    assert rows and rows[-1]["outcome"] == "error"


def test_query_redacts_secrets(tmp_path: Path) -> None:
    driver = FakeMikrotikDriver(
        read=LiveReadResult(records=[{"name": "public", "note": "uses fakesecret44028f"}])
    )
    ctx = _ctx(tmp_path, driver)
    out = _run(ctx, "routeros_query", {"device": "core-gw-1", "path": "snmp/community"})
    assert "public" not in out  # snmp 'name' field redacted
    assert "fakesecret44028f" not in out  # value-backstop (the configured mikrotik password)
    assert _REDACTED in out


def test_query_truncation_note(tmp_path: Path) -> None:
    driver = FakeMikrotikDriver(
        read=LiveReadResult(records=[{"n": str(i)} for i in range(3)], truncated=True)
    )
    out = _run(_ctx(tmp_path, driver), "routeros_query", {"device": "core-gw-1", "path": "ip/rt"})
    assert "truncated" in out.lower()


def test_query_forwards_where_and_fields_and_clamps_limit(tmp_path: Path) -> None:
    driver = FakeMikrotikDriver(read=LiveReadResult(records=[]))
    ctx = _ctx(tmp_path, driver)
    _run(
        ctx,
        "routeros_query",
        {
            "device": "core-gw-1",
            "path": "interface",
            "where": ["running=true", "disabled=false"],
            "fields": ["name", "running"],
            "limit": 99999,
        },
    )
    kind, _addr, segs, where, fields, max_records = driver.calls[0]
    assert kind == "read"
    assert segs == ("interface",)
    assert where == (("running", "true"), ("disabled", "false"))
    assert fields == ("name", "running")
    assert max_records == ctx.config.routeros_live.max_records  # clamped down from 99999


def test_query_without_mikrotik_reports_cleanly(tmp_path: Path) -> None:
    # a non-mikrotik (logs-only) host: no live driver, so the tool reports cleanly
    from tests.fakes import logs_settings

    config = chat_config(tmp_path, routeros=False, logs=logs_settings())
    ctx = ToolContext(config=config, driver=None, audit=AuditTrail(config.audit_path))
    out = _run(ctx, "routeros_query", {"device": "core-gw-1", "path": "interface"})
    assert "[routeros]" in out


# --- routeros_ping ---------------------------------------------------------------------------


def test_ping_renders_summary(tmp_path: Path) -> None:
    driver = FakeMikrotikDriver(
        ping=PingResult(
            sent=4,
            received=4,
            loss_pct=0.0,
            rtt_min_ms=1.0,
            rtt_avg_ms=2.0,
            rtt_max_ms=3.0,
            probes=[PingProbe(seq=i, status="ok", time_ms=1.0) for i in range(4)],
        )
    )
    args = {"device": "core-gw-1", "destination": "8.8.8.8"}
    out = _run(_ctx(tmp_path, driver), "routeros_ping", args)
    assert "sent=4" in out and "received=4" in out and "loss=0.0%" in out


def test_ping_unreachable_is_full_loss(tmp_path: Path) -> None:
    driver = FakeMikrotikDriver(ping=PingResult(sent=4, received=0, loss_pct=100.0, probes=[]))
    args = {"device": "core-gw-1", "destination": "192.0.2.1"}
    out = _run(_ctx(tmp_path, driver), "routeros_ping", args)
    assert "loss=100.0%" in out


def test_ping_clamps_count(tmp_path: Path) -> None:
    driver = FakeMikrotikDriver(ping=PingResult(sent=1, received=1, loss_pct=0.0))
    ctx = _ctx(tmp_path, driver)
    _run(ctx, "routeros_ping", {"device": "core-gw-1", "destination": "8.8.8.8", "count": 9999})
    cap = ctx.config.routeros_live.ping_count_max
    assert driver.calls[0] == ("ping", "172.18.84.1", "8.8.8.8", cap)


# --- routeros_traceroute ---------------------------------------------------------------------


def test_traceroute_renders_hops(tmp_path: Path) -> None:
    driver = FakeMikrotikDriver(
        trace=TraceResult(
            hops=[TraceHop(1, "172.25.110.1", avg_ms=1.0), TraceHop(2, "8.8.8.8", avg_ms=5.0)],
            complete=True,
        )
    )
    args = {"device": "core-gw-1", "destination": "8.8.8.8"}
    out = _run(_ctx(tmp_path, driver), "routeros_traceroute", args)
    assert "172.25.110.1" in out and "8.8.8.8" in out
    assert "1." in out and "2." in out


def test_traceroute_incomplete_note(tmp_path: Path) -> None:
    driver = FakeMikrotikDriver(
        trace=TraceResult(hops=[TraceHop(1, "172.25.110.1", avg_ms=1.0)], complete=False)
    )
    args = {"device": "core-gw-1", "destination": "9.9.9.9"}
    out = _run(_ctx(tmp_path, driver), "routeros_traceroute", args)
    assert "cap" in out.lower() or "did not reach" in out.lower()
