"""Shared scaffolding for obi's SQLite stores (feature 059).

Every obi SQLite store — :mod:`obi.core.logstore`, :mod:`obi.core.metricstore`,
:mod:`obi.core.auditstore` — opened with the same three-step ``connect``: ``mkdir`` the parent,
``sqlite3.connect``, then ``PRAGMA journal_mode=WAL`` / ``PRAGMA synchronous=NORMAL`` and an
``executescript`` of a ``CREATE ... IF NOT EXISTS`` schema. That last step had **no migration
story**: on an existing, cron-fed store ``IF NOT EXISTS`` silently no-ops, so the first feature to
add a column would pass every (fresh-tmpdir) test and then raise ``no such column`` against the
operator's real database in the field.

:func:`apply_schema` closes that gap. It stamps the schema version into ``PRAGMA user_version`` and
applies an ordered, run-exactly-once migration list, so a store's on-disk shape converges to the
code's expectation whether the database is brand new or predates a change. The base schema stays
frozen at v1; every later change is a migration entry, never an edit to the base schema.

``sqlite3`` remains quarantined to the store modules (the :mod:`obi.core.logstore` rule); this
module is store-internal scaffolding, not a general database layer.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

# The shared home for the alias each store re-exports so callers can catch store failures without
# importing sqlite3 themselves.
StoreError = sqlite3.Error


def apply_schema(
    conn: sqlite3.Connection,
    base_schema: str,
    migrations: Sequence[str] = (),
) -> int:
    """Bring ``conn`` up to the current schema version; return the version it now sits at.

    ``base_schema`` is the FROZEN v1 schema — an idempotent block of ``CREATE ... IF NOT EXISTS``
    statements. Running it establishes version 1 for a brand-new database *and* leaves an existing
    (pre-versioning) database untouched, so both are, by definition, at v1 afterwards.

    ``migrations[i]`` upgrades the store FROM version ``i + 1`` TO version ``i + 2`` and is applied
    exactly once, in order, only when the stored ``user_version`` is behind. **Every schema change
    after v1 MUST be appended here as a new entry — never edited into ``base_schema``** — so that a
    database created before the change (via the old base schema) and one created after (via the same
    base schema + this migration) converge to the identical shape.

    ``PRAGMA user_version`` takes no bound parameter, so the target version is formatted into the
    statement; it is always a small integer derived from the migration list's own indexing, never
    caller-supplied text.
    """

    conn.executescript(base_schema)
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version == 0:
        # A fresh database (just created by base_schema) or one that predates version stamping
        # (its tables already existed, so executescript no-op'd): both are at v1 by definition.
        version = 1
        conn.execute("PRAGMA user_version = 1")
    for index, statement in enumerate(migrations):
        target = index + 2  # migrations[0] takes v1 -> v2, migrations[1] takes v2 -> v3, ...
        if version < target:
            conn.executescript(statement)
            conn.execute(f"PRAGMA user_version = {target}")
            version = target
    return version
