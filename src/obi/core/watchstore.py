"""Persistence for the autonomous watcher (feature 014).

Holds the watcher's durable state under ``<state>/watch/``: ``state.json`` (the cursor + the latest
:class:`~obi.core.watch.WatchRun`) and ``reports/`` (one ``<id>.json`` + ``<id>.md`` per finding).
State is written atomically (temp + ``os.replace``) so a crash mid-write never corrupts it
(Principle VI), exactly like the inventory writer.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any

from obi.core.watch import (
    SelfCheckState,
    WatchCursor,
    WatchFinding,
    WatchRun,
    cursor_from_dict,
    cursor_to_dict,
    finding_from_dict,
    finding_to_dict,
    render_markdown,
    run_from_dict,
    run_to_dict,
    selfcheck_from_dict,
    selfcheck_to_dict,
)


class WatchStore:
    """Read/write the watcher's cursor, run record, and report files under ``base_dir``."""

    def __init__(self, base_dir: Path) -> None:
        self.base = base_dir
        self.reports_dir = base_dir / "reports"
        self.state_path = base_dir / "state.json"
        self.runs_path = base_dir / "runs.jsonl"

    # --- state (cursor + last run) ---

    def _read_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {}
        try:
            data = json.loads(self.state_path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def load_cursor(self) -> WatchCursor | None:
        cursor = self._read_state().get("cursor")
        return cursor_from_dict(cursor) if isinstance(cursor, dict) else None

    def load_last_run(self) -> WatchRun | None:
        run = self._read_state().get("last_run")
        return run_from_dict(run) if isinstance(run, dict) else None

    def load_open(self) -> dict[str, datetime]:
        """Load the alert-once dedup set: finding key → time it was last alerted."""

        raw = self._read_state().get("open_findings")
        out: dict[str, datetime] = {}
        if isinstance(raw, dict):
            for key, value in raw.items():
                try:
                    out[str(key)] = datetime.fromisoformat(str(value))
                except ValueError:
                    continue
        return out

    def load_selfcheck(self) -> SelfCheckState:
        """Load the self-monitoring state (feature 062, A4).

        Returns a fresh :class:`SelfCheckState` when the key is absent — a pre-062 ``state.json``
        loads clean (``last_heartbeat_at=None`` ⇒ the first upgraded run sends an inaugural
        heartbeat, FR-014)."""

        raw = self._read_state().get("selfcheck")
        return selfcheck_from_dict(raw) if isinstance(raw, dict) else SelfCheckState()

    def save_state(
        self,
        cursor: WatchCursor | None,
        run: WatchRun,
        *,
        open_findings: dict[str, datetime] | None = None,
        selfcheck: SelfCheckState | None = None,
    ) -> None:
        self.base.mkdir(parents=True, exist_ok=True)
        payload = {
            "cursor": cursor_to_dict(cursor) if cursor is not None else None,
            "last_run": run_to_dict(run),
            "open_findings": {
                key: seen.isoformat() for key, seen in (open_findings or {}).items()
            },
        }
        if selfcheck is not None:
            payload["selfcheck"] = selfcheck_to_dict(selfcheck)
        tmp = self.state_path.with_name(f"{self.state_path.name}.tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        os.replace(tmp, self.state_path)  # atomic on POSIX
        # Append to the run history so `obi watch status` can report a last-24h token total.
        with self.runs_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(run_to_dict(run), ensure_ascii=False) + "\n")

    def recent_runs(self, *, since: datetime | None = None) -> list[WatchRun]:
        """Return runs from the history (``runs.jsonl``), optionally only those after ``since``."""

        if not self.runs_path.exists():
            return []
        out: list[WatchRun] = []
        for line in self.runs_path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                run = run_from_dict(json.loads(line))
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
            if since is None or run.finished_at >= since:
                out.append(run)
        return out

    # --- reports ---

    def write_finding(self, finding: WatchFinding, *, display_tz: tzinfo | None = None) -> None:
        """Persist a finding's ``.json`` + ``.md`` reports.

        ``display_tz`` (069) renders the stored timestamps in the operator's ``log_tz`` so the
        on-disk artifacts and ``obi watch --show`` read in local time, matching the already-local
        syslog window fields. The stored *instant* is unchanged, and ``list_findings`` sorts/filters
        by the parsed ``datetime``, so the offset in the string is immaterial to any logic.
        """

        self.reports_dir.mkdir(parents=True, exist_ok=True)
        json_path = self.reports_dir / f"{finding.id}.json"
        payload = finding_to_dict(finding, display_tz=display_tz)
        json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        (self.reports_dir / f"{finding.id}.md").write_text(
            render_markdown(finding, display_tz=display_tz)
        )

    def list_findings(
        self,
        *,
        since: datetime | None = None,
        severity: str | None = None,
        device: str | None = None,
        limit: int | None = None,
    ) -> list[WatchFinding]:
        """Return stored findings, newest-first, after applying the optional filters."""

        out: list[WatchFinding] = []
        if not self.reports_dir.exists():
            return out
        for path in self.reports_dir.glob("*.json"):
            try:
                out.append(finding_from_dict(json.loads(path.read_text())))
            except (OSError, json.JSONDecodeError, KeyError, ValueError):
                continue
        out.sort(key=lambda finding: finding.created_at, reverse=True)
        if since is not None:
            out = [finding for finding in out if finding.created_at >= since]
        if severity is not None:
            out = [finding for finding in out if finding.severity == severity]
        if device is not None:
            needle = device.lower()
            out = [finding for finding in out if needle in finding.device.lower()]
        if limit is not None:
            out = out[:limit]
        return out

    def get_markdown(self, finding_id: str) -> str | None:
        """Return one finding's rendered markdown, or ``None`` if it is not on disk."""

        path = self.reports_dir / f"{finding_id}.md"
        try:
            return path.read_text()
        except OSError:
            return None

    def prune(self, retention_days: int, *, now: datetime) -> int:
        """Delete report files older than ``retention_days``; return how many were removed."""

        if retention_days <= 0 or not self.reports_dir.exists():
            return 0
        cutoff = now - timedelta(days=retention_days)
        removed = 0
        for path in self.reports_dir.glob("*.json"):
            try:
                created = datetime.fromisoformat(json.loads(path.read_text())["created_at"])
            except (OSError, json.JSONDecodeError, KeyError, ValueError):
                continue
            if created < cutoff:
                path.unlink(missing_ok=True)
                path.with_suffix(".md").unlink(missing_ok=True)
                removed += 1
        return removed
