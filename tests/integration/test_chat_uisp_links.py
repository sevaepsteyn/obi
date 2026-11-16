"""Integration: the `uisp_links` chat tool (feature 034 — T011, contracts/surfaces.md).

Fixture-backed and offline: the tool renders down-links-first with per-end scores, writes exactly
one engine audit record carrying the operator's question, degrades conversationally (unconfigured
controller / classified failure), and never leaks the token.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from obi.core.audit import AuditTrail
from obi.core.drivers.uisp import UispController, UispError
from obi.core.models import SourceOutcome
from obi.core.toolset import ToolContext, build_dispatch, build_tools
from tests.fakes import chat_config, load_uisp_links_fixture, uisp_settings

_FIXTURES = {
    "/nms/api/v2.1/data-links": load_uisp_links_fixture("data-links"),
    "/nms/api/v2.1/devices": load_uisp_links_fixture("devices"),
}


def _transport(path: str, timeout: float) -> list[dict[str, Any]]:
    return _FIXTURES[path]


def _ctx(
    tmp_path: Path,
    *,
    uisp: bool = True,
    transport: Any = _transport,
    question: str = "how's the chestnut backhaul?",
    ignore_names: list[str] | None = None,
) -> ToolContext:
    uisp_cfg = uisp_settings(ignore_names=ignore_names or [])
    config = chat_config(tmp_path, routeros=False, uisp=uisp_cfg if uisp else None)
    source = (
        UispController(uisp_settings(), geo_transport=transport) if uisp else None
    )
    return ToolContext(
        config=config,
        driver=None,
        uisp_source=source,
        audit=AuditTrail(config.audit_path),
        question=question,
    )


def _run(ctx: ToolContext, args: dict[str, Any] | None = None) -> str:
    return build_dispatch(ctx, build_tools())["uisp_links"](args or {})


def test_renders_links_down_first_with_scores(tmp_path: Path) -> None:
    out = _run(_ctx(tmp_path))

    assert "Live UISP wireless links" in out
    assert "active=" in out and "down=" in out
    lines = [line for line in out.splitlines() if line.startswith("- ")]
    assert len(lines) == 40  # every wireless link, no ethernet
    down = [i for i, line in enumerate(lines) if "DOWN" in line]
    active = [i for i, line in enumerate(lines) if "active" in line]
    assert down and active and max(down) < min(active)  # down links listed first
    assert "score" in out  # joined per-end quality reaches the model
    assert "dBm" in out
    assert "UISP-TOKEN" not in out


def test_name_filter_narrows_to_the_backhaul(tmp_path: Path) -> None:
    out = _run(_ctx(tmp_path), {"name_contains": "chestnut"})

    lines = [line for line in out.splitlines() if line.startswith("- ")]
    assert lines and all("chestnut" in line.lower() for line in lines)


def test_ignore_names_excludes_matching_links(tmp_path: Path) -> None:
    """feature 035: [uisp] ignore_names drops matching links from the chat answer."""
    baseline = [ln for ln in _run(_ctx(tmp_path)).splitlines() if ln.startswith("- ")]
    out = _run(_ctx(tmp_path, ignore_names=["chestnut"]))
    lines = [ln for ln in out.splitlines() if ln.startswith("- ")]

    assert lines, "non-chestnut links still answer"
    assert len(lines) < len(baseline), "the chestnut links were excluded"
    assert all("chestnut" not in ln.lower() for ln in lines)
    assert "UISP-TOKEN" not in out


def test_unconfigured_controller_degrades_conversationally(tmp_path: Path) -> None:
    out = _run(_ctx(tmp_path, uisp=False))
    assert "No UISP controller is configured" in out


def test_controller_failure_degrades_conversationally(tmp_path: Path) -> None:
    def _fail(path: str, timeout: float) -> list[dict[str, Any]]:
        raise UispError(SourceOutcome.AUTH_FAILED, "controller rejected the API token (HTTP 401)")

    out = _run(_ctx(tmp_path, transport=_fail))

    assert "auth" in out.lower()
    assert "no wireless link health" in out.lower()
    assert "Traceback" not in out


def test_writes_one_audit_record_with_question(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, question="any weak links right now?")
    _run(ctx)

    records = [
        json.loads(line)
        for line in ctx.audit.path.read_text().splitlines()
        if line.strip()
    ]
    link_records = [r for r in records if r["action"] == "uisp_links"]
    assert len(link_records) == 1  # the engine's record; no handler double-count
    assert link_records[0]["reason"] == "any weak links right now?"
    assert "UISP-TOKEN" not in json.dumps(records)
