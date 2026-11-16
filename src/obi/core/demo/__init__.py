"""Demo dataset sanitizer (feature 134): real event-cycle stores → a publishable fictional network.

``obi demo snapshot`` reads obi's own local stores READ-ONLY over a bounded window and writes a
self-contained dataset of a fictional ISP ("Example Valley Net") that obi's own readers open
unchanged. Four transformations happen on the way, each owned by one module:

* :mod:`~obi.core.demo.mapping` — one stable real→fictional value per identity (IPs via
  :mod:`~obi.core.demo.ipremap`, names via :mod:`~obi.core.demo.naming`), persisted privately;
* :mod:`~obi.core.demo.rewrite` — free text through that mapping, secrets scrubbed first;
* :mod:`~obi.core.demo.relocate` — the whole network moved rigidly to the destination region;
* :mod:`~obi.core.demo.timeshift` — every timestamp moved by whole weeks to a neutral epoch.

:mod:`~obi.core.demo.gate` then scans every byte of the staged dataset INDEPENDENTLY of the
rewriter and refuses to publish on any fatal finding. :mod:`~obi.core.demo.snapshot` owns the
run (stage → gate → atomic rename → audit); the CLI is a thin caller (Principle III).

Like the store modules, this package reads and writes SQLite directly — it is a store-to-store
copier, and every output database is created with the owning store's own schema.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


class DemoError(Exception):
    """A snapshot failure with the CLI exit code it maps to (contracts/cli.md)."""

    exit_code = 1


class ProfileError(DemoError):
    """The demo profile / private files are unusable (exit 2)."""

    exit_code = 2


class LeakGateError(DemoError):
    """The leak gate found a FATAL finding (exit 3); the message is the redacted report."""

    exit_code = 3


class BudgetError(DemoError):
    """The staged dataset exceeds ``max_dataset_mb`` without ``--allow-oversize`` (exit 4)."""

    exit_code = 4


class SourceError(DemoError):
    """A source store or the terrain regeneration could not be read/built (exit 5)."""

    exit_code = 5


def open_source_db(path: Path) -> sqlite3.Connection:
    """Open a LIVE store strictly read-only (FR-002); never creates, locks, or writes it.

    A store with a live ``-wal`` (a writer is attached) opens ``mode=ro`` so committed WAL content
    is visible; its ``-wal``/``-shm`` already exist, so nothing is created. A store with no
    ``-wal`` is fully checkpointed and opens ``immutable=1`` — a ``mode=ro`` open there would
    CREATE ``-wal``/``-shm`` beside the live store (and fails outright when it cannot, as observed
    on logs.db).
    """

    if not path.exists():
        raise SourceError(f"source store not found: {path.name}")
    has_wal = path.with_name(path.name + "-wal").exists()
    mode = "mode=ro" if has_wal else "immutable=1"
    try:
        conn = sqlite3.connect(f"file:{path}?{mode}", uri=True)
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except sqlite3.Error as exc:
        raise SourceError(f"could not open source store {path.name}: {exc}") from exc
    conn.row_factory = sqlite3.Row
    return conn
