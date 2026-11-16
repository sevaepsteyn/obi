"""The ``obi demo snapshot`` run (feature 134, data-model §Snapshot run states).

    init → harvested → staged → terrain_regenerated → budget_ok → gate_passed → published → audited

Everything is written into a private (0700) staging directory beside the output; only a clean
gate leads to the atomic ``rename`` into place, and only then is the private mapping saved. Any
failure removes the staging directory, so a partial dataset never exists at the output path. The
live state directory is read-only throughout; the run's only writes outside the output are the
one audit record and the private mapping (FR-002). Stale staging directories left by a killed run
(sanitized but UNGATED data) are removed at start.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from obi.core.audit import AuditTrail
from obi.core.demo import BudgetError, DemoError, LeakGateError, ProfileError, open_source_db
from obi.core.demo.gate import GateResult, LeakGate, format_report
from obi.core.demo.harvest import Harvest, harvest
from obi.core.demo.manifest import add_gate, build_manifest, write_manifest
from obi.core.demo.mapping import PseudonymMap
from obi.core.demo.naming import load_dictionary
from obi.core.demo.profile import DemoProfile
from obi.core.demo.relocate import Relocator, centroid
from obi.core.demo.rewrite import Rewriter
from obi.core.demo.sources import (
    INCLUDED,
    SourceContext,
    SourceReport,
    config_toml,
    configs,
    excluded,
    geo,
    icmp,
    inventory,
    jsonfiles,
    logs,
    mesh,
    metrics,
    weather,
)
from obi.core.demo.sources import (
    audit as audit_source,
)
from obi.core.demo.timeshift import TimeShift
from obi.core.models import AuditOutcome, AuditRecord

_HOUR_US = 3600 * 1_000_000
_MB = 1024 * 1024

#: Output paths → the source that owns them (for the per-source byte counts).
_OWNERS = (
    ("logs.db", "logs"),
    ("metrics.db", "metrics"),
    ("audit", "audit"),
    ("icmp", "icmp"),
    ("config-repo", "configs"),
    ("config-captures.json", "configs"),
    ("ar/snapshots", "snapshots"),
    ("ar", "geo"),
    ("dash", "dash"),
    ("weather.db", "weather"),
    ("meshtastic", "mesh"),
    ("inventory.json", "inventory"),
    ("ospf-layout.json", "ospf_layout"),
)


@dataclass
class SnapshotResult:
    state: str
    output_dir: Path | None
    window: tuple[datetime, datetime]
    reports: list[SourceReport] = field(default_factory=list)
    gate: GateResult | None = None
    total_bytes: int = 0
    harvested_tokens: int = 0
    estimates: dict[str, int] = field(default_factory=dict)
    excluded_records: dict[str, int] = field(default_factory=dict)


# --- window ----------------------------------------------------------------------------------


def _busiest_window(state_dir: Path, hours: int) -> tuple[int, int]:
    """The busiest contiguous ``hours`` by hourly log counts (index range counts only)."""

    for name, table, column in (("logs.db", "logs", "ts_us"), ("metrics.db", "samples", "ts_us")):
        path = state_dir / name
        if not path.exists():
            continue
        conn = open_source_db(path)
        try:
            lo, hi = conn.execute(f"SELECT min({column}), max({column}) FROM {table}").fetchone()
            if lo is None:
                continue
            start = lo - lo % _HOUR_US
            counts: list[int] = []
            t = start
            while t <= hi:
                counts.append(
                    conn.execute(
                        f"SELECT count(*) FROM {table} WHERE {column} >= ? AND {column} < ?",
                        (t, t + _HOUR_US),
                    ).fetchone()[0]
                )
                t += _HOUR_US
        finally:
            conn.close()
        if len(counts) <= hours:
            return start, start + len(counts) * _HOUR_US - 1
        running = sum(counts[:hours])
        best_i, best = 0, running
        for i in range(1, len(counts) - hours + 1):
            running += counts[i + hours - 1] - counts[i - 1]
            if running > best:
                best_i, best = i, running
        begin = start + best_i * _HOUR_US
        return begin, begin + hours * _HOUR_US - 1
    raise ProfileError("no logs or metrics store to choose a window from; pass --since/--until")


def resolve_window(profile: DemoProfile, state_dir: Path) -> tuple[int, int]:
    if profile.since is not None and profile.until is not None:
        return int(profile.since.timestamp() * 1e6), int(profile.until.timestamp() * 1e6)
    return _busiest_window(state_dir, profile.window_hours)


# --- helpers ---------------------------------------------------------------------------------


def _remove_stale_staging(output_dir: Path) -> None:
    for stale in output_dir.parent.glob(f"{output_dir.name}.staging-*"):
        try:
            pid = int(stale.name.rsplit("-", 1)[1])
        except ValueError:
            continue
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            shutil.rmtree(stale, ignore_errors=True)
        except PermissionError:
            continue


def _sizes(staging: Path, reports: list[SourceReport]) -> int:
    state = staging / "state" / "obi"
    by_owner: dict[str, int] = {}
    total = 0
    for path in staging.rglob("*"):
        if not path.is_file():
            continue
        size = path.stat().st_size
        total += size
        try:
            rel = path.relative_to(state).as_posix()
        except ValueError:
            if path.relative_to(staging).as_posix() == "config.toml":
                by_owner["config"] = by_owner.get("config", 0) + size
            continue
        for prefix, owner in _OWNERS:
            if rel.startswith(prefix):
                by_owner[owner] = by_owner.get(owner, 0) + size
                break
    for report in reports:
        report.bytes = by_owner.get(report.name, 0)
    return total


def _estimate(state_dir: Path, window: tuple[int, int], decimate: int) -> dict[str, int]:
    estimates: dict[str, int] = {}
    for name, table, column in (
        ("logs.db", "logs", "ts_us"),
        ("metrics.db", "samples", "ts_us"),
        ("audit.db", "events", "ts_us"),
    ):
        path = state_dir / name
        if path.exists():
            conn = open_source_db(path)
            try:
                estimates[name.removesuffix(".db")] = conn.execute(
                    f"SELECT count(*) FROM {table} WHERE {column} BETWEEN ? AND ?", window
                ).fetchone()[0]
            finally:
                conn.close()
    estimated = (
        estimates.get("logs", 0) * 450
        + estimates.get("metrics", 0) // max(decimate, 1) * 90
        + estimates.get("audit", 0) * 700
    )
    estimates["estimated_mb"] = estimated // _MB
    return estimates


def prepare(
    raw_config: dict[str, Any], profile: DemoProfile
) -> tuple[tuple[int, int], Harvest, PseudonymMap, TimeShift, Relocator]:
    """Window → harvest → mapping/shift/relocation (no writes; shared by dry-run and the run)."""

    state_dir = profile.state_dir
    window = resolve_window(profile, state_dir)
    h = harvest(raw_config, state_dir, window, profile.denylist_path, load_dictionary())
    mapping = PseudonymMap.load_or_create(profile.mapping_path)
    mapping.forbid(h.forbidden_tokens())
    mapping.ip.check_pools_disjoint(sorted(h.networks | h.ips))
    tz = profile.tz
    real_start = datetime.fromtimestamp(window[0] / 1e6, tz=UTC).astimezone(tz)
    shift = TimeShift.from_window(real_start, profile.epoch_start, tz)
    coords = h.site_coords or h.coords
    origin = centroid(coords) if coords else (profile.destination_lat, profile.destination_lon)
    relocator = Relocator(origin, (profile.destination_lat, profile.destination_lon),
                          profile.rotation_deg)  # fmt: skip
    mapping.geo = {
        "centroid": list(origin),
        "anchor": [profile.destination_lat, profile.destination_lon],
        "rotation_deg": profile.rotation_deg,
    }
    mapping.time = {"shift_weeks": shift.weeks}
    return window, h, mapping, shift, relocator


def public_values_of(dataset: Path) -> frozenset[str]:
    """The values a run declared public, rebuilt from a published dataset for a re-gate: the
    destination summit names in ``terrain.json`` and the manifest's own constants (source names,
    reasons, excluded-record keys) — both written by obi's code, never copied from live data."""

    values: set[str] = set()
    terrain = dataset / "state" / "obi" / "ar" / "terrain.json"
    if terrain.exists():
        values.update(geo._peak_names(json.loads(terrain.read_text(encoding="utf-8"))))
    manifest_path = dataset / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for source in manifest.get("sources", []):
            values.add(str(source.get("name", "")))
            if source.get("reason"):
                values.add(str(source["reason"]))
        values.update(str(k) for k in manifest.get("excluded_records", {}))
    return frozenset(v for v in values if v)


def _audit(audit: AuditTrail | None, outcome: AuditOutcome, state: str, params: dict[str, Any],
           detail: str | None = None) -> None:  # fmt: skip
    if audit is None:
        return
    audit.record(
        AuditRecord(
            timestamp=datetime.now(UTC),
            action="demo_snapshot",
            target="demo-dataset",
            reason="operator-initiated demo dataset snapshot (feature 134)",
            outcome=outcome,
            params={"state": state, **params},
            detail=detail,
        )
    )


# --- the run ---------------------------------------------------------------------------------


def run_snapshot(
    raw_config: dict[str, Any],
    profile: DemoProfile,
    *,
    dry_run: bool = False,
    allow_oversize: bool = False,
    audit: AuditTrail | None = None,
    fetchers: dict[str, object] | None = None,
    post_source_hook: Callable[[Path], None] | None = None,
) -> SnapshotResult:
    """Produce (or, with ``dry_run``, preview) one gated dataset at ``profile.output_dir``."""

    out = profile.output_dir
    state = "init"
    if out.exists():
        raise ProfileError(f"output {out} already exists; remove it first (never overwritten)")
    _remove_stale_staging(out)
    window, h, mapping, shift, relocator = prepare(raw_config, profile)
    state = "harvested"
    target = (
        shift.dt(datetime.fromtimestamp(window[0] / 1e6, tz=UTC)).astimezone(profile.tz),
        shift.dt(datetime.fromtimestamp(window[1] / 1e6, tz=UTC)).astimezone(profile.tz),
    )
    if dry_run:
        return SnapshotResult(
            state="dry_run",
            output_dir=None,
            window=target,
            reports=excluded.declare(profile.state_dir),
            harvested_tokens=h.token_count(),
            estimates=_estimate(profile.state_dir, window, profile.metrics_decimate),
        )

    staging = out.parent / f"{out.name}.staging-{os.getpid()}"
    out_state = staging / "state" / "obi"
    reports: list[SourceReport] = []
    counts: dict[str, Any] = {}
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        staging.mkdir(mode=0o700)
        out_state.mkdir(parents=True)
        rewriter = Rewriter(mapping, h, shift, relocator)
        ctx = SourceContext(
            state_dir=profile.state_dir,
            out_state=out_state,
            window=window,
            rewriter=rewriter,
            mapping=mapping,
            shift=shift,
            relocator=relocator,
            profile=profile,
            harvest=h,
            raw_config=raw_config,
            fetchers=dict(fetchers or {}),
        )
        for run in (inventory.run, metrics.run, icmp.run, logs.run, audit_source.run, configs.run,
                    jsonfiles.run_ospf, jsonfiles.run_dash, weather.run, mesh.run):  # fmt: skip
            reports.append(run(ctx))
        reports.extend(jsonfiles.run_snapshots(ctx))
        state = "staged"
        reports.append(geo.run(ctx))
        state = "terrain_regenerated"
        reports.append(config_toml.run(ctx))
        reports.extend(excluded.declare(profile.state_dir))
        if post_source_hook is not None:
            post_source_hook(staging)

        total = _sizes(staging, reports)
        manifest = build_manifest(
            region=profile.region_label,
            window=target,
            timezone=profile.timezone,
            reports=reports,
            excluded_records=ctx.excluded_records,
        )
        write_manifest(staging / "manifest.json", manifest)
        counts = {
            "total_bytes": total,
            "sources": {r.name: r.rows_out for r in reports if r.status == INCLUDED},
        }
        if total > profile.max_dataset_mb * _MB and not allow_oversize:
            sizes = ", ".join(f"{r.name}={r.bytes // _MB}MB" for r in reports if r.bytes)
            raise BudgetError(
                f"dataset is {total // _MB} MB, over the {profile.max_dataset_mb} MB budget "
                f"({sizes}); narrow the window or pass --allow-oversize"
            )
        state = "budget_ok"

        # The manifest's source names and reasons are this module's own constants, not data.
        ctx.public_values.update(r.name for r in reports)
        ctx.public_values.update(r.reason for r in reports if r.reason)
        ctx.public_values.update(ctx.excluded_records)
        result = LeakGate(
            h,
            strict_event_terms=profile.strict_event_terms,
            exempt_values=frozenset(ctx.public_values),
        ).scan(staging)
        counts["gate_fatal"] = len(result.fatal)
        counts["gate_warnings"] = len(result.warnings)
        if result.fatal:
            raise LeakGateError(format_report(result))
        state = "gate_passed"
        write_manifest(staging / "manifest.json", add_gate(manifest, result))

        os.rename(staging, out)
        state = "published"
        mapping.save(profile.mapping_path)
        _audit(audit, AuditOutcome.OK, "audited", counts)
        return SnapshotResult(
            state="audited",
            output_dir=out,
            window=target,
            reports=reports,
            gate=result,
            total_bytes=total,
            harvested_tokens=h.token_count(),
            excluded_records=dict(ctx.excluded_records),
        )
    except DemoError as exc:
        shutil.rmtree(staging, ignore_errors=True)
        _audit(audit, AuditOutcome.ERROR, f"failed:{state}", counts, detail=type(exc).__name__)
        raise
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        _audit(audit, AuditOutcome.ERROR, f"failed:{state}", counts, detail="unexpected error")
        raise
