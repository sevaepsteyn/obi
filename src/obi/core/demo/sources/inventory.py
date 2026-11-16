"""inventory.json source (feature 134, T030): every field, the crawl scope and alt addresses."""

from __future__ import annotations

import json

from obi.core.demo.sources import ABSENT, INCLUDED, SourceContext, SourceReport

NAME = "inventory"


def run(ctx: SourceContext) -> SourceReport:
    src = ctx.state_dir / "inventory.json"
    if not src.exists():
        return SourceReport(NAME, ABSENT)
    data = json.loads(src.read_text(encoding="utf-8"))
    entries = data.get("entries", [])
    for entry in entries:
        if entry.get("serial"):
            entry["serial"] = ctx.mapping.serial(str(entry["serial"]))
    rewritten = ctx.rewriter.json(data)
    (ctx.out_state / "inventory.json").write_text(
        json.dumps(rewritten, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return SourceReport(NAME, INCLUDED, rows_in=len(entries), rows_out=len(entries))
