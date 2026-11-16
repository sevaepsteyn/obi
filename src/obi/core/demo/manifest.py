"""The public dataset manifest (feature 134, FR-015, contracts/dataset.md).

The manifest describes the dataset without describing the real network: the ONLY window it
records is the shifted (target) one, per-source counts are over that window, and the relocation
names the destination region alone. Real dates, the shift, the source centroid, the anchor and
the rotation never appear (they would let a reader undo the transforms).
"""

from __future__ import annotations

import json
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from obi.core.demo.gate import GATE_VERSION, GateResult
from obi.core.demo.sources import SourceReport

FORMAT_VERSION = 1
ORGANISATION = "Example Valley Net"
DOMAIN = "example.net"


def _obi_version() -> str:
    try:
        return version("obi")
    except PackageNotFoundError:  # pragma: no cover - running from a source tree
        return "dev"


def build_manifest(
    *,
    region: str,
    window: tuple[datetime, datetime],
    timezone: str,
    reports: list[SourceReport],
    excluded_records: dict[str, int],
) -> dict[str, Any]:
    return {
        "format_version": FORMAT_VERSION,
        "generated_by": f"obi {_obi_version()}",
        "organisation": ORGANISATION,
        "domain": DOMAIN,
        "region": region,
        "window": {
            "start": window[0].isoformat(),
            "end": window[1].isoformat(),
            "tz": timezone,
        },
        "sources": [
            {
                "name": r.name,
                "status": r.status,
                **({"reason": r.reason} if r.reason else {}),
                **(
                    {"rows_in": r.rows_in, "rows_out": r.rows_out, "bytes": r.bytes}
                    if r.status == "included"
                    else {}
                ),
                **({"notes": dict(sorted(r.notes.items()))} if r.notes else {}),
            }
            for r in reports
        ],
        "excluded_records": dict(sorted(excluded_records.items())),
    }


def add_gate(manifest: dict[str, Any], result: GateResult) -> dict[str, Any]:
    manifest["gate"] = {
        "version": GATE_VERSION,
        "harvested_tokens": result.counts.get("harvested_tokens", 0),
        "denylist_terms": result.counts.get("denylist_terms", 0),
        "files_scanned": result.counts.get("files", 0),
        "findings": len(result.fatal),
        "warnings": len(result.warnings),
    }
    return manifest


def write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.write_text(json.dumps(manifest, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
