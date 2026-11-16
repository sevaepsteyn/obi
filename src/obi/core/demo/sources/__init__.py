"""Per-store sanitizing sources (feature 134, research R9).

Each source module owns one store's quirks — the vendor-adapter pattern applied to data: it reads
the LIVE store read-only over the window, rewrites every row through the shared
:class:`~obi.core.demo.rewrite.Rewriter`, and writes the result into the staging tree with the
owning store module's own schema, so obi's readers open the dataset unchanged. A source returns a
:class:`SourceReport`; it never decides publication (the orchestrator and the gate do).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from obi.core.demo.harvest import Harvest
from obi.core.demo.mapping import PseudonymMap
from obi.core.demo.profile import DemoProfile
from obi.core.demo.relocate import Relocator
from obi.core.demo.rewrite import Rewriter
from obi.core.demo.timeshift import TimeShift

INCLUDED = "included"
EXCLUDED = "excluded"
ABSENT = "absent"


@dataclass
class SourceContext:
    """Everything a source needs; ``state_dir`` is the LIVE tree and is only ever read."""

    state_dir: Path
    out_state: Path
    window: tuple[int, int]
    rewriter: Rewriter
    mapping: PseudonymMap
    shift: TimeShift
    relocator: Relocator
    profile: DemoProfile
    harvest: Harvest
    raw_config: dict[str, object]
    excluded_records: dict[str, int] = field(default_factory=dict)
    #: Optional injected fetchers ("tile", "elevation", "gnis") — tests and offline runs.
    fetchers: dict[str, object] = field(default_factory=dict)
    #: Exact values a source declares public (destination summit names) — exempt in the gate.
    public_values: set[str] = field(default_factory=set)

    def count_excluded(self, reason: str, n: int = 1) -> None:
        if n:
            self.excluded_records[reason] = self.excluded_records.get(reason, 0) + n


@dataclass
class SourceReport:
    name: str
    status: str
    reason: str | None = None
    rows_in: int = 0
    rows_out: int = 0
    bytes: int = 0
    #: Non-identifying per-source facts for the manifest (e.g. ``fresnel_clear``).
    notes: dict[str, int] = field(default_factory=dict)


def finish_db(conn: sqlite3.Connection) -> None:
    """Commit, leave WAL mode, VACUUM and close: one self-contained, deterministic file."""

    conn.commit()
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("VACUUM")
    conn.close()


def new_db(path: Path, connect: Callable[[Path], sqlite3.Connection]) -> sqlite3.Connection:
    """Create an output store with the owning module's ``connect`` (schema + migrations)."""

    path.parent.mkdir(parents=True, exist_ok=True)
    return connect(path)
