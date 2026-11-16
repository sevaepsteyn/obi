"""Integration (feature 036): the watcher's investigation loop reaches the per-vendor read-only
tools a human has in chat — uisp_status (006), uisp_links (034), siklu_status (020), and
config_query (026) — and the live device/controller reads are bounded by the per-run live-call
budget while config_query (a local git read) is not.

Two layers are exercised:
- run_cycle end-to-end proves core/watch.py::_judge actually WIRES uisp_source/siklu_driver/
  config_source into the ToolContext (a handler only reaches its engine when its source is wired,
  so an engine spy that fires is proof — and for uisp_links this is the 034 latent-gap fix:
  pre-036 the watcher never gave it a source, so it dead-ended before the engine).
- build_dispatch proves the budget guard added to each live handler consumes exactly one unit, and
  that config_query consumes none.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from obi.core.audit import AuditTrail
from obi.core.drivers.siklu import SikluDriver
from obi.core.drivers.uisp import UispController
from obi.core.models import SourceOutcome, UispLinksReport
from obi.core.toolset import (
    BUDGETED_LIVE_TOOL_NAMES,
    ConfigBackupSource,
    LiveCallBudget,
    ToolContext,
    build_dispatch,
    build_tools,
)
from obi.core.watch import run_cycle
from obi.core.watchstore import WatchStore
from tests.fakes import (
    StubLLMClient,
    chat_config,
    config_backup_settings,
    prime_cursor,
    seed_logs,
    siklu_settings,
    text_response,
    tool_response,
    uisp_settings,
    watch_config,
)

NOW = datetime(2026, 7, 8, 12, 0, 0, tzinfo=UTC)


def _fault_rows() -> list[tuple[str, datetime, str, str]]:
    return [("core-1", NOW - timedelta(minutes=2), "interface,info", "link ether1 down")]


def _seeded_store(cfg) -> WatchStore:  # type: ignore[no-untyped-def]
    seed_logs(cfg.logs.mirror_root, cfg.logs.db_path, _fault_rows())
    store = WatchStore(cfg.watch.watch_dir)
    prime_cursor(store)
    return store


# --- run_cycle: the watcher wires every vendor source into the investigation loop ---------------


def test_investigation_reaches_all_vendor_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A watch cycle whose model calls each vendor tool reaches each source — proving _judge wired
    uisp_source (incl. the uisp_links latent-gap fix), siklu_driver, and config_source."""

    cfg = watch_config(tmp_path, investigation_max_steps=5)
    cfg.uisp = uisp_settings()
    cfg.siklu = siklu_settings()
    cfg.configs = config_backup_settings(tmp_path)
    # No deterministic UISP-link pass here (cfg.watch.uisp_links stays None), so any engine.uisp_*
    # call during this cycle comes from the investigation loop, not a background pass.
    store = _seeded_store(cfg)

    seen: list[str] = []

    def _spy(label: str, result: object):  # type: ignore[no-untyped-def]
        def run(*_a: object, **_k: object) -> object:
            seen.append(label)
            return result

        return run

    monkeypatch.setattr("obi.core.engine.uisp_status", _spy("uisp_status", None))
    monkeypatch.setattr(
        "obi.core.engine.uisp_links",
        _spy(
            "uisp_links",
            UispLinksReport(
                controller="c",
                outcome=SourceOutcome.REACHABLE,
                links=[],
                started_at=NOW,
                finished_at=NOW,
            ),
        ),
    )
    monkeypatch.setattr("obi.core.engine.siklu_status", _spy("siklu_status", None))
    monkeypatch.setattr(
        "obi.core.engine.config_show",
        _spy(
            "config_show", SimpleNamespace(found=True, content="/ip firewall", router_version="7")
        ),
    )

    responses = [
        tool_response("uisp_status", {}, use_id="t1"),
        tool_response("uisp_links", {}, use_id="t2"),
        tool_response("siklu_status", {}, use_id="t3"),
        tool_response("config_query", {"device": "core-1"}, use_id="t4"),
        tool_response("report_findings", {"findings": []}, use_id="t5"),
    ]
    run_cycle(
        cfg, store, audit=AuditTrail(cfg.audit_path), now=NOW, llm_client=StubLLMClient(responses)
    )

    # Every vendor engine was reached — the source is wired for each. (uisp_status returns None so
    # its renderer errors and the dispatch wrapper swallows it; the spy still recorded the reach.)
    assert seen == ["uisp_status", "uisp_links", "siklu_status", "config_show"]


def test_budget_zero_strips_live_tools_but_keeps_config(tmp_path: Path) -> None:
    """At investigation_live_call_budget=0 the watcher offers the model none of the budgeted live
    tools, but keeps config_query (a local read) and the non-live tools."""

    cfg = watch_config(tmp_path, investigation_max_steps=1)
    cfg.uisp = uisp_settings()
    cfg.siklu = siklu_settings()
    cfg.configs = config_backup_settings(tmp_path)
    cfg.watch.investigation_live_call_budget = 0
    store = _seeded_store(cfg)

    stub = StubLLMClient(
        [text_response("done")]
    )  # model ends immediately; we inspect offered tools
    run_cycle(cfg, store, audit=AuditTrail(cfg.audit_path), now=NOW, llm_client=stub)

    offered = {t["name"] for t in stub.calls[0]["tools"]}
    for name in BUDGETED_LIVE_TOOL_NAMES:
        assert name not in offered, f"{name} must be stripped at budget 0"
    assert "config_query" in offered  # local read stays available
    assert "logs_query" in offered  # non-live tools stay available


# --- dispatch layer: the per-handler budget guard (consume / do-not-consume) --------------------


def _budgeted_ctx(tmp_path: Path, budget: LiveCallBudget) -> ToolContext:
    cfg = chat_config(
        tmp_path,
        routeros=False,
        uisp=uisp_settings(),
        siklu=siklu_settings(),
        configs=config_backup_settings(tmp_path),
    )
    return ToolContext(
        config=cfg,
        driver=None,
        audit=AuditTrail(cfg.audit_path),
        uisp_source=UispController(cfg.uisp),
        siklu_driver=SikluDriver(cfg.siklu),
        config_source=ConfigBackupSource(cfg.configs),
        live_budget=budget,
    )


def test_live_vendor_tools_each_consume_one_budget_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reached: list[str] = []

    def _spy(label: str):  # type: ignore[no-untyped-def]
        def run(*_a: object, **_k: object) -> object:
            reached.append(label)
            raise RuntimeError("stub: reached the source")  # dispatch wrapper swallows this

        return run

    monkeypatch.setattr("obi.core.engine.uisp_status", _spy("uisp_status"))
    monkeypatch.setattr("obi.core.engine.uisp_links", _spy("uisp_links"))
    monkeypatch.setattr("obi.core.engine.siklu_status", _spy("siklu_status"))

    budget = LiveCallBudget(5)
    dispatch = build_dispatch(_budgeted_ctx(tmp_path, budget), build_tools())
    dispatch["uisp_status"]({})
    dispatch["uisp_links"]({})
    dispatch["siklu_status"]({})

    # Each reached its source (so the guard let it through) and each spent one unit: 5 - 3 = 2.
    assert reached == ["uisp_status", "uisp_links", "siklu_status"]
    assert budget.remaining == 2


def test_query_config_does_not_consume_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "obi.core.engine.config_show",
        lambda *_a, **_k: SimpleNamespace(
            found=True, content="/ip firewall filter add chain=input", router_version="7.14"
        ),
    )
    budget = LiveCallBudget(2)
    dispatch = build_dispatch(_budgeted_ctx(tmp_path, budget), build_tools())

    out = dispatch["config_query"]({"device": "core-gw-1"})

    assert "firewall" in out  # the local read went through
    assert budget.remaining == 2  # ...but a local git read never decrements the live-call budget


def test_budgeted_tools_skip_before_contact_when_budget_exhausted(tmp_path: Path) -> None:
    """With no budget left, each live vendor tool returns the skip message WITHOUT contacting its
    source (no engine patched — a real contact would error/hang, so a clean skip proves the guard
    short-circuits first)."""

    dispatch = build_dispatch(_budgeted_ctx(tmp_path, LiveCallBudget(0)), build_tools())
    for name in ("uisp_status", "uisp_links", "siklu_status"):
        out = dispatch[name]({})
        assert "budget" in out.lower(), f"{name} should report the exhausted budget"
