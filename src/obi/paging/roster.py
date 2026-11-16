"""The operator-local paging roster (feature 102): snapshot, search, strict resolution.

A CSV snapshot (``type,id,name,fullname``) of the account's pageable people and groups,
living in operator-local state — real ESD names never enter the repository. It exists so
name→ID resolution works fully OFFLINE (field resilience) and so a name can never silently
pick the wrong human: resolution is case-insensitive substring over ``name``/``fullname``,
and anything but exactly one match REFUSES — unknown fails with guidance, ambiguous fails
listing every candidate (mispaging a real responder is the failure mode this module
prevents). Numeric tokens pass through untouched.

The snapshot refreshes from the live uAPI ``listpeople``/``listgroups`` responses (shapes
confirmed by capture 2026-08-26 — the official reference does not fix them); a refresh
rewrites atomically and keeps the previous snapshot as ``.bak``.
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_FIELDS = ("type", "id", "name", "fullname")


@dataclass(frozen=True)
class RosterEntry:
    """One pageable recipient. ``fullname`` is people-only (live groups carry none)."""

    type: str  # "person" | "group"
    id: str
    name: str
    fullname: str = ""

    def display(self) -> str:
        tail = f" — {self.fullname}" if self.fullname else ""
        return f"{self.type:<6} {self.id:>4}  {self.name}{tail}"


@dataclass(frozen=True)
class Resolved:
    """Deduped, order-preserving ID buckets ready for one ``sendpage`` call."""

    people: list[str]
    groups: list[str]


class ResolutionError(Exception):
    """A token could not be resolved to exactly one recipient. NOTHING may be sent.

    ``kind`` is ``unknown`` (no match), ``ambiguous`` (several — ``candidates`` lists them),
    or ``no-roster`` (name given but no snapshot exists).
    """

    def __init__(self, message: str, *, kind: str, candidates: list[RosterEntry] | None = None):
        super().__init__(message)
        self.kind = kind
        self.candidates = candidates or []


def load_roster(path: Path) -> list[RosterEntry] | None:
    """Read the snapshot; ``None`` when it does not exist (distinct from an empty roster)."""

    if not path.exists():
        return None
    with path.open(newline="") as handle:
        return [
            RosterEntry(
                type=row.get("type", ""),
                id=row.get("id", ""),
                name=row.get("name", ""),
                fullname=row.get("fullname", "") or "",
            )
            for row in csv.DictReader(handle)
        ]


def save_roster(path: Path, entries: list[RosterEntry]) -> None:
    """Atomic rewrite; an existing snapshot survives as ``<path>.bak``."""

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=_FIELDS)
        writer.writeheader()
        for entry in entries:
            writer.writerow(
                {"type": entry.type, "id": entry.id, "name": entry.name, "fullname": entry.fullname}
            )
    if path.exists():
        os.replace(path, path.with_suffix(path.suffix + ".bak"))
    os.replace(tmp, path)


def search(
    entries: list[RosterEntry],
    query: str = "",
    *,
    people_only: bool = False,
    groups_only: bool = False,
) -> list[RosterEntry]:
    """Offline lookup: substring over name/fullname (case-insensitive) or exact ID."""

    rows = entries
    if people_only:
        rows = [e for e in rows if e.type == "person"]
    if groups_only:
        rows = [e for e in rows if e.type == "group"]
    q = query.strip().lower()
    if q:
        rows = [
            e for e in rows if q in e.name.lower() or q in e.fullname.lower() or q == e.id
        ]
    return rows


def split_tokens(raw: str) -> list[str]:
    return [t for t in (piece.strip() for piece in raw.split(",")) if t]


def resolve(
    tokens: list[str], roster: list[RosterEntry] | None, force_type: str | None = None
) -> Resolved:
    """Strictly resolve tokens to ID buckets; raises :class:`ResolutionError` on any doubt."""

    people: list[str] = []
    groups: list[str] = []
    for token in tokens:
        token = token.strip()
        if not token:
            continue
        if token.isdigit():
            (groups if force_type == "group" else people).append(token)
            continue
        if roster is None:
            raise ResolutionError(
                f"cannot resolve {token!r}: no roster snapshot — run `obi page refresh` "
                f"or pass numeric IDs",
                kind="no-roster",
            )
        pool = [e for e in roster if force_type is None or e.type == force_type]
        needle = token.lower()
        matches = [
            e for e in pool if needle in e.name.lower() or needle in e.fullname.lower()
        ]
        if not matches:
            raise ResolutionError(f"no recipient matches {token!r}", kind="unknown")
        if len(matches) > 1:
            raise ResolutionError(
                f"{token!r} is ambiguous — narrow the name or pass the numeric ID",
                kind="ambiguous",
                candidates=matches,
            )
        entry = matches[0]
        (groups if entry.type == "group" else people).append(entry.id)
    return Resolved(people=_dedupe(people), groups=_dedupe(groups))


def parse_live(people_body: Any, groups_body: Any) -> list[RosterEntry]:
    """Build roster entries from live ``listpeople``/``listgroups`` responses.

    Strict about the captured shape: an unexpected response raises ``ValueError`` so a
    refresh FAILS (snapshot untouched) rather than silently writing a gutted roster.
    """

    people_raw = people_body.get("people") if isinstance(people_body, dict) else None
    groups_raw = groups_body.get("groups") if isinstance(groups_body, dict) else None
    if not isinstance(people_raw, list) or not isinstance(groups_raw, list):
        raise ValueError("unexpected list response shape (no 'people'/'groups' arrays)")
    entries: list[RosterEntry] = []
    for group in groups_raw:
        if not isinstance(group, dict) or "group_id" not in group:
            raise ValueError(f"unexpected group entry: {group!r}")
        entries.append(
            RosterEntry(type="group", id=str(group["group_id"]), name=str(group.get("name", "")))
        )
    for person in people_raw:
        if not isinstance(person, dict) or "person_id" not in person:
            raise ValueError(f"unexpected person entry: {person!r}")
        entries.append(
            RosterEntry(
                type="person",
                id=str(person["person_id"]),
                name=str(person.get("name", "")),
                fullname=str(person.get("fullname", "")),
            )
        )
    return entries


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out
