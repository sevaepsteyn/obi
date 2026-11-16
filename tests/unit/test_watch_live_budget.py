"""Unit (feature 028): the live tools are in the watcher toolset and bounded by the per-run budget.

The autonomous watcher may use routeros_query/ping/traceroute, but a LiveCallBudget caps how many
live device calls one run makes (FR-015/SC-007): past the cap the handler skips WITHOUT contacting a
device, and a pre-flight refusal never consumes the budget.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from obi.core.audit import AuditTrail
from obi.core.drivers.mikrotik import LiveReadResult, MikrotikDriver
from obi.core.inventory import InventoryEntry
from obi.core.toolset import (
    BUDGETED_LIVE_TOOL_NAMES,
    LiveCallBudget,
    ToolContext,
    build_dispatch,
    build_tools,
    build_watch_tools,
)
from tests.fakes import chat_config, save_inventory, settings

# feature 036: the budget now bounds the UISP controller reads and Siklu SSH reads too, not just
# the three RouterOS live tools.
# feature 111: mesh_locate is budgeted (it contacts the attached mesh radio) but deliberately
# NOT offered to the watcher at all (test_watch_toolset pins that), so it is excluded from
# the "watcher includes every budgeted tool" expectation here.
_LIVE = set(BUDGETED_LIVE_TOOL_NAMES) - {"mesh_locate"}


class _FakeDriver(MikrotikDriver):
    def __init__(self) -> None:
        super().__init__(settings())
        self.reads = 0

    def read_path(
        self,
        address: str,
        segments: Any,
        *,
        where: Any = (),
        fields: Any = (),
        timeout: float,
        max_records: int,
    ) -> LiveReadResult:  # type: ignore[override]
        self.reads += 1
        return LiveReadResult(records=[{"name": "ether1"}])


def _ctx(tmp_path: Path, driver: MikrotikDriver, *, budget: LiveCallBudget | None) -> ToolContext:
    config = chat_config(tmp_path)
    save_inventory(
        config.inventory_path, [InventoryEntry(address="172.18.84.1", identity="core-gw-1")]
    )
    return ToolContext(
        config=config, driver=driver, audit=AuditTrail(config.audit_path), live_budget=budget
    )


def test_watch_toolset_includes_live_tools_all_readonly() -> None:
    tools = build_watch_tools()
    names = {t.name for t in tools}
    assert names >= _LIVE
    for tool in tools:
        assert tool.mutating is False
        assert tool.risk_class == "read_only"


def test_budget_exhaustion_skips_without_contacting_device(tmp_path: Path) -> None:
    driver = _FakeDriver()
    dispatch = build_dispatch(_ctx(tmp_path, driver, budget=LiveCallBudget(1)), build_tools())
    out1 = dispatch["routeros_query"]({"device": "core-gw-1", "path": "interface"})
    out2 = dispatch["routeros_query"]({"device": "core-gw-1", "path": "interface"})
    assert "ether1" in out1
    assert "budget" in out2.lower()
    assert driver.reads == 1  # the second call never reached the device


def test_none_budget_is_unthrottled(tmp_path: Path) -> None:
    driver = _FakeDriver()
    dispatch = build_dispatch(_ctx(tmp_path, driver, budget=None), build_tools())
    for _ in range(5):
        dispatch["routeros_query"]({"device": "core-gw-1", "path": "interface"})
    assert driver.reads == 5


def test_refused_path_does_not_consume_budget(tmp_path: Path) -> None:
    driver = _FakeDriver()
    dispatch = build_dispatch(_ctx(tmp_path, driver, budget=LiveCallBudget(1)), build_tools())
    refused = dispatch["routeros_query"]({"device": "core-gw-1", "path": "system/reboot"})
    assert "read-only" in refused.lower() or "refused" in refused.lower()
    ok = dispatch["routeros_query"]({"device": "core-gw-1", "path": "interface"})
    assert "ether1" in ok  # the budget of 1 survived the refusal and served the real read


def test_live_call_budget_consume_semantics() -> None:
    budget = LiveCallBudget(2)
    assert budget.consume() is True
    assert budget.consume() is True
    assert budget.consume() is False
    assert LiveCallBudget(0).consume() is False
