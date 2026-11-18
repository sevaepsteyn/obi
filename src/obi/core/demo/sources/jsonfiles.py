"""The JSON-file sources (feature 134, T033/T034): OSPF layout, web snapshots, dash state.

All three are rewritten structurally by :meth:`Rewriter.json` — keys are identifiers (router-id
IPs, ``fleet|<device>`` pager keys), string values by field context, epoch ``*_us``/``*_s``
numbers time-shifted, lat/lon pairs relocated.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from obi.core.demo.sources import ABSENT, EXCLUDED, INCLUDED, SourceContext, SourceReport

#: Web snapshots shipped in the dataset; netbox-drift is excluded (NetBox URLs/tenants, R9).
SNAPSHOTS = ("status", "uisp", "uisp-links", "siklu", "unifi", "ospf")
DASH_FILES = ("board.json", "netwatch-state.json", "pager-state.json", "mesh-status.json")


#: Lists of site/device NAMES inside a ``maintenance`` block: identifiers, not prose, so a
#: dictionary-word site ("the bear site") is renamed like any device name would be.
_MAINT_NAME_LISTS = ("sites", "uisp_sites", "devices")


def _ident_maintenance(ctx: SourceContext, obj: Any) -> Any:
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            if key == "maintenance" and isinstance(value, dict):
                value = {
                    k: [ctx.rewriter.ident(x) if isinstance(x, str) else x for x in v]
                    if k in _MAINT_NAME_LISTS and isinstance(v, list)
                    else v
                    for k, v in value.items()
                }
            out[key] = _ident_maintenance(ctx, value)
        return out
    if isinstance(obj, list):
        return [_ident_maintenance(ctx, x) for x in obj]
    return obj


def _rewrite_file(ctx: SourceContext, src: Path, dest: Path) -> None:
    data: Any = json.loads(src.read_text(encoding="utf-8"))
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        json.dumps(ctx.rewriter.json(_ident_maintenance(ctx, data)), indent=1, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )


def run_ospf(ctx: SourceContext) -> SourceReport:
    """The blessed OSPF layout: ``[web] ospf_layout_path`` when set, else the state root file."""

    web = ctx.raw_config.get("web")
    configured = web.get("ospf_layout_path") if isinstance(web, dict) else None
    src = Path(str(configured)).expanduser() if configured else ctx.state_dir / "ospf-layout.json"
    if not src.exists():
        return SourceReport("ospf_layout", ABSENT)
    _rewrite_file(ctx, src, ctx.out_state / "ospf-layout.json")
    n = len(json.loads(src.read_text(encoding="utf-8")))
    return SourceReport("ospf_layout", INCLUDED, rows_in=n, rows_out=n)


def run_snapshots(ctx: SourceContext) -> list[SourceReport]:
    snaps = ctx.state_dir / "ar" / "snapshots"
    if not snaps.is_dir():
        return [SourceReport("snapshots", ABSENT)]
    written = 0
    for name in SNAPSHOTS:
        src = snaps / f"{name}.json"
        if src.exists():
            _rewrite_file(ctx, src, ctx.out_state / "ar" / "snapshots" / f"{name}.json")
            written += 1
    reports = [SourceReport("snapshots", INCLUDED, rows_in=written, rows_out=written)]
    if (snaps / "netbox-drift.json").exists():
        reports.append(
            SourceReport("snapshot:netbox-drift", EXCLUDED, "NetBox URLs and tenant data")
        )
    return reports


def run_dash(ctx: SourceContext) -> SourceReport:
    dash = ctx.state_dir / "dash"
    if not dash.is_dir():
        return SourceReport("dash", ABSENT)
    written = 0
    for name in DASH_FILES:
        src = dash / name
        if src.exists():
            _rewrite_file(ctx, src, ctx.out_state / "dash" / name)
            written += 1
    return SourceReport("dash", INCLUDED, rows_in=written, rows_out=written)
