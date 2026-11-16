"""The autonomous log watcher (feature 014).

One bounded read-only cycle per invocation (the cron target): read the syslog entries that
arrived since the last run and, when a cloud model is available, judge the new window for problems,
optionally doing a small amount of read-only investigation to characterize what happened. Findings
are written by the :mod:`obi.core.watchstore`; this module holds the orchestration and the models.

`obi watch` is a cloud-reasoning feature — a sibling of `obi chat` (constitution Principle VI
carve-out): it reads the new log window and asks the model what looks wrong. With no connectivity
it degrades cleanly — it records the condition and advances the watermark, producing no findings —
and never crashes the cron. The ``chat`` reasoning seams are imported lazily, so importing this
module never requires the cloud.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from obi.core import configstore, icmpstore, logstore, metricstore
from obi.core import health as health_core
from obi.core import inventory as inv
from obi.core.audit import AuditRecorder, AuditTrail, audit_scope, record_event
from obi.core.config import (
    Config,
    WatchConfigChangesSettings,
    WatchSettings,
    duration_to_seconds,
)
from obi.core.filter import name_excludes
from obi.core.maintenance import Maintenance
from obi.core.models import AuditOutcome, AuditRecord, LogEvent, SourceOutcome, _utcnow

if TYPE_CHECKING:
    from obi.core.llm import LLMClient
    from obi.core.watchstore import WatchStore

SEVERITIES = ("info", "warning", "critical")
CONFIDENCES = ("low", "medium", "high")
REPORT_TOOL = "report_findings"

_SLUG_RE = re.compile(r"[^a-z0-9]+")


# --- domain models ----------------------------------------------------------------------------


@dataclass
class WatchCursor:
    """How far the watcher has processed the store: the high-water ingest ``id`` (FR-002).

    Uses the monotonic ingest id, not the event timestamp, so a device's clock skew cannot poison
    the watermark (see :mod:`obi.core.logstore`).
    """

    id: int


@dataclass
class Evidence:
    """One supporting fact for a finding — a log line, command output, or metric."""

    kind: str  # log | command | metric | link | config | probe
    text: str
    at: datetime | None = None
    host: str | None = None
    ref: str | None = None


@dataclass
class WatchFinding:
    """A single suspected problem the watcher identified (FR-007). Written as JSON + markdown."""

    id: str
    created_at: datetime
    device: str
    window_start: datetime
    window_end: datetime
    severity: str  # info | warning | critical
    confidence: str  # low | medium | high
    summary: str
    description: str
    evidence: list[Evidence]
    source: str  # "llm" | "metrics" | "reachability" | "uisp-links" | "config" (origin of finding)
    device_address: str | None = None
    likely_cause: str | None = None
    suggested_remediation: str | None = None
    investigation: list[dict[str, Any]] = field(default_factory=list)
    # feature 027: an optional discriminator appended to the dedup key so metric findings
    # de-duplicate per interface+counter (not just device+severity). ``None`` for LLM findings, so
    # their key is byte-for-byte unchanged.
    dedup_extra: str | None = None

    def dedup_key(self) -> str:
        """Stable identity for alert-once dedup: one alert per device+severity (re-phrasings of the
        same problem collapse; a higher severity re-alerts). ``dedup_extra`` — set only by metric
        findings (feature 027) — further namespaces by interface+counter."""

        base = f"{self.source}|{self.device}|{self.severity}"
        return f"{base}|{self.dedup_extra}" if self.dedup_extra else base


@dataclass
class WatchRun:
    """Summary of one cycle; the latest is what ``obi watch status`` shows."""

    started_at: datetime
    finished_at: datetime
    new_event_count: int
    outcome: str  # no_op | findings | degraded | error
    tokens_input: int = 0
    tokens_output: int = 0
    llm_calls: int = 0
    cursor_before: WatchCursor | None = None
    cursor_after: WatchCursor | None = None
    finding_ids: list[str] = field(default_factory=list)
    conditions: list[str] = field(default_factory=list)
    # Slack delivery outcome for this cycle (feature 015); 0/0 when delivery is off (FR-015).
    alerts_delivered: int = 0
    alerts_failed: int = 0
    # feature 062: self-monitoring figures — how far behind the watcher is after this run.
    # ``scanned_events`` is the candidates the SQL window returned (post filter-before-LIMIT),
    # ``window_lines`` the count after collapse (A3), ``remaining_candidates`` the judgeable
    # events still beyond the new cursor, ``lag_seconds`` the age of the oldest such candidate,
    # ``ingest_age_seconds`` the age of the newest stored event (ingest freshness).
    scanned_events: int = 0
    window_lines: int = 0
    remaining_candidates: int = 0
    lag_seconds: int = 0
    ingest_age_seconds: int | None = None
    heartbeat_sent: bool = False


@dataclass
class SelfCheckState:
    """Durable self-monitoring state (feature 062, A4), persisted additively in ``state.json``.

    ``last_heartbeat_at`` gates the heartbeat cadence (``None`` ⇒ never sent ⇒ inaugural fires).
    The ``period_*`` accumulators roll up activity since the last heartbeat for the next summary.
    """

    last_heartbeat_at: datetime | None = None
    period_runs: int = 0
    period_events: int = 0
    period_findings: int = 0
    period_alerts: int = 0
    period_conditions: dict[str, int] = field(default_factory=dict)


# --- delivery (feature 015) -------------------------------------------------------------------


@dataclass
class DeliveryResult:
    """Outcome of one ``deliver`` call. Invariant: ``delivered + failed == len(findings)``."""

    delivered: int
    failed: int
    error: str | None = None


class Notifier(Protocol):
    """The seam the cycle calls to push qualifying findings out (e.g. to Slack).

    Core depends only on this Protocol — never on a concrete client (the Slack adapter lives in the
    interface layer, ``obi.slack.notifier``). Implementations MUST NOT raise: any failure comes back
    in the :class:`DeliveryResult` so a problem never breaks the read-only cycle (FR-007).
    """

    def deliver(self, findings: list[WatchFinding]) -> DeliveryResult: ...

    def deliver_status(self, text: str) -> DeliveryResult:
        """Deliver one plain status message — the self-monitoring heartbeat (feature 062, A4).

        Unlike :meth:`deliver` this is NOT a finding: it bypasses the severity floor and alert-once
        dedup by design (a periodic health note). One DM, no threaded report. MUST NOT raise.
        """
        ...


def _severity_rank(severity: str) -> int:
    """Order severities for the delivery floor: ``info`` < ``warning`` < ``critical``."""

    try:
        return SEVERITIES.index(severity)
    except ValueError:
        return 0


# --- serialization ----------------------------------------------------------------------------


def cursor_to_dict(cursor: WatchCursor) -> dict[str, int]:
    return {"id": cursor.id}


def cursor_from_dict(data: dict[str, Any]) -> WatchCursor:
    return WatchCursor(id=int(data["id"]))


def _display_iso(dt: datetime | None, tz: tzinfo | None) -> str | None:
    """ISO-8601 for a human-facing timestamp, normalized to ``tz`` when one is given.

    ``created_at`` is UTC (``_utcnow``) while syslog-derived window/evidence times are already
    tz-aware in ``log_tz``; converting every field to the one display zone lines them up on screen
    (069). The conversion is idempotent on a field already in ``tz`` and preserves the instant, so
    internal comparisons that keep using the raw ``datetime`` objects are untouched. ``tz=None``
    keeps the field exactly as stored (the default: unchanged behavior for every legacy caller).
    """

    if dt is None:
        return None
    return (dt.astimezone(tz) if tz is not None else dt).isoformat()


def finding_to_dict(finding: WatchFinding, *, display_tz: tzinfo | None = None) -> dict[str, Any]:
    return {
        "id": finding.id,
        "created_at": _display_iso(finding.created_at, display_tz),
        "device": finding.device,
        "device_address": finding.device_address,
        "window_start": _display_iso(finding.window_start, display_tz),
        "window_end": _display_iso(finding.window_end, display_tz),
        "severity": finding.severity,
        "confidence": finding.confidence,
        "summary": finding.summary,
        "description": finding.description,
        "source": finding.source,
        "likely_cause": finding.likely_cause,
        "suggested_remediation": finding.suggested_remediation,
        "dedup_extra": finding.dedup_extra,
        "evidence": [
            {
                "kind": ev.kind,
                "at": _display_iso(ev.at, display_tz),
                "host": ev.host,
                "text": ev.text,
                "ref": ev.ref,
            }
            for ev in finding.evidence
        ],
        "investigation": finding.investigation,
    }


def finding_from_dict(data: dict[str, Any]) -> WatchFinding:
    return WatchFinding(
        id=str(data["id"]),
        created_at=datetime.fromisoformat(data["created_at"]),
        device=str(data["device"]),
        device_address=data.get("device_address"),
        window_start=datetime.fromisoformat(data["window_start"]),
        window_end=datetime.fromisoformat(data["window_end"]),
        severity=str(data["severity"]),
        confidence=str(data["confidence"]),
        summary=str(data["summary"]),
        description=str(data.get("description", "")),
        source=str(data.get("source", "llm")),
        likely_cause=data.get("likely_cause"),
        suggested_remediation=data.get("suggested_remediation"),
        dedup_extra=data.get("dedup_extra"),
        evidence=[
            Evidence(
                kind=str(ev.get("kind", "log")),
                text=str(ev.get("text", "")),
                at=datetime.fromisoformat(ev["at"]) if ev.get("at") else None,
                host=ev.get("host"),
                ref=ev.get("ref"),
            )
            for ev in data.get("evidence", [])
        ],
        investigation=list(data.get("investigation", [])),
    )


def run_to_dict(run: WatchRun) -> dict[str, Any]:
    return {
        "started_at": run.started_at.isoformat(),
        "finished_at": run.finished_at.isoformat(),
        "new_event_count": run.new_event_count,
        "outcome": run.outcome,
        "tokens_input": run.tokens_input,
        "tokens_output": run.tokens_output,
        "llm_calls": run.llm_calls,
        "cursor_before": cursor_to_dict(run.cursor_before) if run.cursor_before else None,
        "cursor_after": cursor_to_dict(run.cursor_after) if run.cursor_after else None,
        "finding_ids": list(run.finding_ids),
        "conditions": list(run.conditions),
        "alerts_delivered": run.alerts_delivered,
        "alerts_failed": run.alerts_failed,
        "scanned_events": run.scanned_events,
        "window_lines": run.window_lines,
        "remaining_candidates": run.remaining_candidates,
        "lag_seconds": run.lag_seconds,
        "ingest_age_seconds": run.ingest_age_seconds,
        "heartbeat_sent": run.heartbeat_sent,
    }


def run_from_dict(data: dict[str, Any]) -> WatchRun:
    cb = data.get("cursor_before")
    ca = data.get("cursor_after")
    return WatchRun(
        started_at=datetime.fromisoformat(data["started_at"]),
        finished_at=datetime.fromisoformat(data["finished_at"]),
        new_event_count=int(data.get("new_event_count", 0)),
        outcome=str(data.get("outcome", "no_op")),
        tokens_input=int(data.get("tokens_input", 0)),
        tokens_output=int(data.get("tokens_output", 0)),
        llm_calls=int(data.get("llm_calls", 0)),
        cursor_before=cursor_from_dict(cb) if cb else None,
        cursor_after=cursor_from_dict(ca) if ca else None,
        finding_ids=list(data.get("finding_ids", [])),
        conditions=list(data.get("conditions", [])),
        alerts_delivered=int(data.get("alerts_delivered", 0)),
        alerts_failed=int(data.get("alerts_failed", 0)),
        scanned_events=int(data.get("scanned_events", 0)),
        window_lines=int(data.get("window_lines", 0)),
        remaining_candidates=int(data.get("remaining_candidates", 0)),
        lag_seconds=int(data.get("lag_seconds", 0)),
        ingest_age_seconds=(
            int(data["ingest_age_seconds"]) if data.get("ingest_age_seconds") is not None else None
        ),
        heartbeat_sent=bool(data.get("heartbeat_sent", False)),
    )


def selfcheck_to_dict(state: SelfCheckState) -> dict[str, Any]:
    return {
        "last_heartbeat_at": (
            state.last_heartbeat_at.isoformat() if state.last_heartbeat_at else None
        ),
        "period_runs": state.period_runs,
        "period_events": state.period_events,
        "period_findings": state.period_findings,
        "period_alerts": state.period_alerts,
        "period_conditions": dict(state.period_conditions),
    }


def selfcheck_from_dict(data: dict[str, Any]) -> SelfCheckState:
    raw_at = data.get("last_heartbeat_at")
    conditions_raw = data.get("period_conditions")
    return SelfCheckState(
        last_heartbeat_at=datetime.fromisoformat(raw_at) if raw_at else None,
        period_runs=int(data.get("period_runs", 0)),
        period_events=int(data.get("period_events", 0)),
        period_findings=int(data.get("period_findings", 0)),
        period_alerts=int(data.get("period_alerts", 0)),
        period_conditions=(
            {str(k): int(v) for k, v in conditions_raw.items()}
            if isinstance(conditions_raw, dict)
            else {}
        ),
    )


def render_markdown(finding: WatchFinding, *, display_tz: tzinfo | None = None) -> str:
    """Render a finding as a human-readable markdown report (the ``.md`` sibling of the JSON).

    ``display_tz`` normalizes the Window/Detected timestamps to one display zone (069); ``None``
    keeps them as stored (unchanged behavior).
    """

    addr = f" ({finding.device_address})" if finding.device_address else ""
    window = (
        f"{_display_iso(finding.window_start, display_tz)} → "
        f"{_display_iso(finding.window_end, display_tz)}"
    )
    lines = [
        f"# {finding.severity.upper()}: {finding.summary}",
        "",
        f"- **Device:** {finding.device}{addr}",
        f"- **Window:** {window}",
        f"- **Severity:** {finding.severity}  |  **Confidence:** {finding.confidence}  "
        f"|  **Source:** {finding.source}",
        f"- **Detected:** {_display_iso(finding.created_at, display_tz)}",
        "",
        "## What happened",
        "",
        finding.description or finding.summary,
        "",
        "## Evidence",
        "",
    ]
    lines.extend(f"- [{ev.kind}] {ev.text}" for ev in finding.evidence)
    if finding.likely_cause:
        lines += ["", "## Possible cause (not verified)", "", finding.likely_cause]
    if finding.suggested_remediation:
        lines += [
            "",
            "## Suggested manual remediation (NOT performed by obi)",
            "",
            finding.suggested_remediation,
        ]
    lines += [
        "",
        "---",
        "_obi watch is read-only: it identifies and reports problems; it never changes device "
        "state._",
        "",
    ]
    return "\n".join(lines)


# --- the report_findings tool the model calls to return structured findings -------------------

_REPORT_TOOL_SCHEMA: dict[str, Any] = {
    "name": REPORT_TOOL,
    "description": (
        "Report the problems you identified in the log window (call this exactly once; pass an "
        "empty findings list if nothing is wrong)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "device": {"type": "string", "description": "affected device identity"},
                        "severity": {"type": "string", "enum": list(SEVERITIES)},
                        "confidence": {"type": "string", "enum": list(CONFIDENCES)},
                        "summary": {"type": "string", "description": "one-line what-happened"},
                        "description": {"type": "string"},
                        "evidence": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "the specific log line(s) supporting this finding",
                        },
                        "likely_cause": {"type": "string"},
                        "suggested_remediation": {"type": "string"},
                    },
                    "required": ["device", "severity", "confidence", "summary", "description"],
                },
            }
        },
        "required": ["findings"],
    },
}


# --- helpers ----------------------------------------------------------------------------------


def _finding_id(now: datetime, device: str, index: int) -> str:
    slug = _SLUG_RE.sub("-", device.lower()).strip("-") or "device"
    return f"{now.strftime('%Y%m%dT%H%M%S')}-{slug}-{index}"


@dataclass
class WindowEntry:
    """One line the judge sees: a representative event plus how many identical repeats it stands
    for (feature 062, A3). ``count == 1`` is an ordinary line; ``count > 1`` collapses a repeat
    storm (a device stuck logging one error) into a single annotated line. ``first_ts``/``last_ts``
    are the min/max timestamps across the collapsed group (the displayed span)."""

    event: LogEvent  # the first occurrence in ingest order — the representative
    first_id: int
    last_id: int
    count: int
    first_ts: datetime
    last_ts: datetime


def collapse_window(candidates: list[tuple[int, LogEvent]]) -> list[WindowEntry]:
    """Collapse identical candidates into one representative each (FR-007).

    Group key = host + severity + topics + whitespace-normalized message, so byte-identical repeats
    (the motivating floods: a storage error firing every few minutes, login/logout lines) collapse
    with zero risk of merging distinct problems — a different host, message, topic, or severity is
    never folded in. First-occurrence (oldest-first) order is preserved."""

    entries: list[WindowEntry] = []
    index: dict[tuple[str, str, str, str], WindowEntry] = {}
    for event_id, event in candidates:
        key = (event.host, event.severity or "", event.topics, " ".join(event.message.split()))
        existing = index.get(key)
        if existing is None:
            entry = WindowEntry(
                event=event,
                first_id=event_id,
                last_id=event_id,
                count=1,
                first_ts=event.ts,
                last_ts=event.ts,
            )
            index[key] = entry
            entries.append(entry)
        else:
            existing.count += 1
            existing.last_id = event_id
            if event.ts > existing.last_ts:
                existing.last_ts = event.ts
            if event.ts < existing.first_ts:
                existing.first_ts = event.ts
    return entries


def _maintenance_for(config: Config) -> Maintenance:
    """The effective maintenance set for one watch cycle (feature 135): config ∪ UISP flags.

    Built once per cycle by ``_run_cycle_scoped`` and threaded into every pass; a pass called
    on its own builds it here, so no entry point can skip maintenance. A UISP read failure
    degrades to the config declaration alone (fail toward alarming for UISP-only items).
    """

    from obi.core import icmp
    from obi.core.config import IcmpSettings

    ignore_names = list(config.uisp.ignore_names) if config.uisp is not None else []
    try:
        flagged, cached, _note = icmp.load_uisp_flagged(
            config.icmp or IcmpSettings(), ignore_names=ignore_names
        )
    except Exception:  # noqa: BLE001
        flagged, cached = set(), False
    return Maintenance.build(config.maintenance, flagged, uisp_cached=cached)


def _window_exclusions(
    config: Config, maintenance: Maintenance | None = None
) -> logstore.WindowExclusions:
    """The store-query filter values equivalent to :func:`_filter_window` (A1).

    ``_routeros_hosts`` returns ``None`` (gate off), an empty set (inventory missing ⇒ admit only
    unifi/uisp), or the lowercased RouterOS host set — passed through verbatim (FR-001)."""

    hosts = _routeros_hosts(config)
    uisp_names = tuple(config.uisp.ignore_names) if config.uisp is not None else ()
    matcher = maintenance if maintenance is not None else _maintenance_for(config)
    maint_sites, maint_hosts = matcher.log_host_terms()
    return logstore.WindowExclusions(
        ignore_topics=tuple(config.watch.ignore_topics),
        ignore_collectors=tuple(config.watch.ignore_collectors),
        allowed_hosts=frozenset(hosts) if hosts is not None else None,
        uisp_ignore_names=uisp_names,
        maintenance_sites=maint_sites,
        maintenance_hosts=maint_hosts,
    )


def _render_window(entries: list[WindowEntry]) -> str:
    total = sum(entry.count for entry in entries)
    collapsed = sum(1 for entry in entries if entry.count > 1)
    header = f"New syslog lines ({total}"
    if collapsed:
        header += f"; {len(entries)} unique after collapsing repeated lines"
    header += "), oldest first:"
    lines = [header]
    for entry in entries:
        event = entry.event
        ts = entry.first_ts.strftime("%Y-%m-%d %H:%M:%S")
        sev = event.severity or "-"
        line = f"{ts} {event.host} [{event.topics}] {sev}: {event.message}"
        if entry.count > 1:
            span_a = entry.first_ts.strftime("%H:%M:%S")
            span_b = entry.last_ts.strftime("%H:%M:%S")
            line += f"  [repeated {entry.count}× between {span_a} and {span_b}]"
        lines.append(line)
    return "\n".join(lines)


def _assistant_content(blocks: list[Any]) -> list[dict[str, Any]]:
    from obi.core.llm import TextBlock, ToolUseBlock

    content: list[dict[str, Any]] = []
    for block in blocks:
        if isinstance(block, TextBlock):
            content.append({"type": "text", "text": block.text})
        elif isinstance(block, ToolUseBlock):
            content.append(
                {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
            )
    return content


def _routeros_hosts(config: Config) -> set[str] | None:
    """Lowercased host names (inventory identities + addresses) that count as RouterOS devices,
    or ``None`` when ``routeros_only`` is off (no host filtering)."""

    if not config.watch.routeros_only:
        return None
    inventory = inv.load(config.inventory_path)
    if inventory is None:
        return set()
    hosts: set[str] = set()
    for device in inventory.queryable_devices():
        if device.identity:
            hosts.add(device.identity.lower())
        if device.address:
            hosts.add(device.address.lower())
    return hosts


def _matches_ignore(topics: str, patterns: list[str]) -> bool:
    """True if any pattern's comma-separated tokens are all present in the event's topics."""

    event_tokens = {token.strip().lower() for token in topics.split(",") if token.strip()}
    for pattern in patterns:
        pat_tokens = {token.strip().lower() for token in pattern.split(",") if token.strip()}
        if pat_tokens and pat_tokens <= event_tokens:
            return True
    return False


def _filter_window(
    events: list[tuple[int, LogEvent]],
    config: Config,
    maintenance: Maintenance | None = None,
) -> list[tuple[int, LogEvent]]:
    """Drop events outside scope before the model sees them.

    The ``routeros_only`` gate restricts the window to obi's curated log sources: RouterOS hosts
    (by inventory identity OR address), UniFi controller events (``collectors`` includes
    ``"unifi"``, feature 016) **and** UISP controller events/outages (``collectors`` includes
    ``"uisp"``, feature 019); other non-curated hosts are dropped. ``ignore_topics`` then applies to
    every admitted row. The config key keeps the name ``routeros_only`` for back-compat.

    Feature 035: a UISP-collector row whose host/site name matches ``[uisp] ignore_names`` is
    dropped too — the UISP-scoped ignore. The collector gate keeps it UISP-only: a RouterOS/UniFi
    row that coincidentally shares the substring is NOT dropped. Ingest is untouched; only what the
    watcher judges is filtered.

    Feature 039: ``[watch] ignore_collectors`` drops every row from a named log collector (e.g.
    ``"unifi"``) before anything else — a collector-level exclusion that ``ignore_topics`` cannot
    express, because a collector's rows carry many varied topics. It is applied ahead of the
    ``routeros_only`` gate, so it removes even a collector that gate would otherwise admit
    (unifi/uisp). Matching is case-insensitive on the event's ``collectors`` list; ingest is
    untouched.

    Feature 135: a row whose host is in maintenance (a declared site token, device, UISP site
    name, or UISP-flagged identity — :meth:`Maintenance.log_host`) is dropped: the operator
    declared it off, so its noise is not news."""

    routeros_hosts = _routeros_hosts(config)
    matcher = maintenance if maintenance is not None else _maintenance_for(config)
    ignore = config.watch.ignore_topics
    ignore_collectors = {c.strip().lower() for c in config.watch.ignore_collectors if c.strip()}
    uisp_ignore_names = config.uisp.ignore_names if config.uisp is not None else []
    out: list[tuple[int, LogEvent]] = []
    for event_id, event in events:
        # feature 039: collector-level exclusion — drop whole collectors (e.g. "unifi") regardless
        # of topics, before the routeros_only gate that would otherwise admit unifi/uisp rows.
        if ignore_collectors and any(c.lower() in ignore_collectors for c in event.collectors):
            continue
        # routeros_only restricts to curated sources: RouterOS hosts OR UniFi OR UISP rows.
        if (
            routeros_hosts is not None
            and event.host.lower() not in routeros_hosts
            and "unifi" not in event.collectors
            and "uisp" not in event.collectors
        ):
            continue
        if ignore and _matches_ignore(event.topics, ignore):
            continue
        # feature 035: UISP-scoped name ignore — drop UISP rows for out-of-scope sites/devices.
        if (
            uisp_ignore_names
            and "uisp" in event.collectors
            and name_excludes(event.host, uisp_ignore_names)
        ):
            continue
        if matcher.log_host(event.host) is not None:
            continue
        out.append((event_id, event))
    return out


def _findings_from_raw(
    raw: list[Any], entries: list[WindowEntry], config: Config, *, now: datetime
) -> list[WatchFinding]:
    """Build :class:`WatchFinding` objects from the model's ``report_findings`` payload."""

    if entries:
        window_start = min(entry.first_ts for entry in entries)
        window_end = max(entry.last_ts for entry in entries)
    else:
        window_start = window_end = now

    addr_by_identity: dict[str, str] = {}
    inventory = inv.load(config.inventory_path)
    if inventory is not None:
        for entry in inventory.entries:
            if entry.identity:
                addr_by_identity[entry.identity.lower()] = entry.address

    out: list[WatchFinding] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        device = str(item.get("device", "")).strip() or "(unknown)"
        severity = str(item.get("severity", "warning")).lower()
        if severity not in SEVERITIES:
            severity = "warning"
        confidence = str(item.get("confidence", "medium")).lower()
        if confidence not in CONFIDENCES:
            confidence = "medium"
        summary = str(item.get("summary", "")).strip() or device
        description = str(item.get("description", "")).strip()
        evidence = [
            Evidence(kind="log", text=str(text))
            for text in (item.get("evidence") or [])
            if str(text).strip()
        ]
        if not evidence:
            evidence = [Evidence(kind="log", text=summary)]
        likely_cause = str(item.get("likely_cause", "")).strip() or None
        remediation = str(item.get("suggested_remediation", "")).strip() or None
        out.append(
            WatchFinding(
                id="",
                created_at=now,
                device=device,
                device_address=addr_by_identity.get(device.lower()),
                window_start=window_start,
                window_end=window_end,
                severity=severity,
                confidence=confidence,
                summary=summary,
                description=description,
                evidence=evidence,
                source="llm",
            )
        )
        out[-1].likely_cause = likely_cause
        out[-1].suggested_remediation = remediation
    return out


def _judge(
    config: Config,
    client: LLMClient,
    entries: list[WindowEntry],
    *,
    audit: AuditTrail,
    now: datetime,
) -> tuple[list[WatchFinding], int, int, int]:
    """Run the bounded judge/investigate loop. Returns (findings, tokens_in, tokens_out, calls)."""

    from obi.core.drivers.mikrotik import MikrotikDriver
    from obi.core.drivers.siklu import SikluDriver
    from obi.core.drivers.snmp import SnmpMetricsSource
    from obi.core.drivers.sqlite_logs import SqliteLogSource
    from obi.core.drivers.sqlite_metrics import SqliteMetricsHistorySource
    from obi.core.drivers.uisp import UispController
    from obi.core.drivers.victoria import VictoriaMetricsSource
    from obi.core.prompt import watch_system_prompt
    from obi.core.toolset import (
        BUDGETED_LIVE_TOOL_NAMES,
        ConfigBackupSource,
        LiveCallBudget,
        ToolContext,
        build_dispatch,
        build_watch_tools,
    )

    # feature 028: bound the autonomous investigation's live device calls per run (FR-015). A budget
    # of 0 means the watcher gets no live tools at all (chat/voice keep them).
    # feature 036: wire the per-vendor sources so the watcher's investigation reaches the same
    # read-only tools a human has in chat — UISP device health/links, Siklu radios, stored config —
    # built exactly as chat/session.build_runtime does, each guarded on its config section. Wiring
    # uisp_source also makes the already-listed uisp_links (034) functional in the loop.
    live_budget = config.watch.investigation_live_call_budget
    ctx = ToolContext(
        config=config,
        driver=MikrotikDriver(config.routeros) if config.routeros is not None else None,
        audit=audit,
        uisp_source=UispController(config.uisp) if config.uisp is not None else None,
        metrics_source=SnmpMetricsSource(config.snmp) if config.snmp is not None else None,
        log_source=SqliteLogSource(config.logs) if config.logs is not None else None,
        # feature 090: stored metric history — a LOCAL read, so it is deliberately not
        # budgeted; the watcher can quantify "traffic collapsed when those errors started"
        # without spending one of its live device calls.
        metrics_history_source=(
            SqliteMetricsHistorySource(config.snmp.history)
            if config.snmp is not None and config.snmp.history is not None
            else None
        ),
        siklu_driver=SikluDriver(config.siklu) if config.siklu is not None else None,
        config_source=ConfigBackupSource(config.configs) if config.configs is not None else None,
        # feature 108: the external VictoriaMetrics store — a REMOTE HTTP read, so its tools
        # are in BUDGETED_LIVE_TOOL_NAMES and each call consumes the live budget (unlike the
        # local metrics_history_source above).
        victoria_source=(
            VictoriaMetricsSource(config.victoria) if config.victoria is not None else None
        ),
        question="autonomous watch cycle",
        live_budget=LiveCallBudget(live_budget) if live_budget > 0 else None,
    )
    tools = build_watch_tools(config)
    if live_budget <= 0:
        # feature 028/036: at budget 0 ctx.live_budget is None (the per-call guard can't fire), so
        # strip every budgeted live tool from the offered set. config_query (local read) stays.
        tools = [tool for tool in tools if tool.name not in BUDGETED_LIVE_TOOL_NAMES]
    dispatch = build_dispatch(ctx, tools)
    tool_schemas = [tool.schema() for tool in tools] + [_REPORT_TOOL_SCHEMA]

    model = config.watch.model or config.chat.model
    max_tokens = config.watch.max_tokens
    system = watch_system_prompt()
    messages: list[dict[str, Any]] = [{"role": "user", "content": _render_window(entries)}]

    tokens_in = tokens_out = calls = 0
    raw_findings: list[Any] = []
    for _ in range(config.watch.investigation_max_steps + 1):
        response = client.create_message(
            system=system, tools=tool_schemas, messages=messages, model=model, max_tokens=max_tokens
        )
        calls += 1
        tokens_in += response.usage.input_tokens
        tokens_out += response.usage.output_tokens
        messages.append({"role": "assistant", "content": _assistant_content(response.content)})

        uses = response.tool_uses()
        report = next((use for use in uses if use.name == REPORT_TOOL), None)
        if report is not None:
            raw_findings = list(report.input.get("findings", []) or [])
            break
        if response.stop_reason != "tool_use" or not uses:
            break  # the model ended without reporting → no findings
        results: list[dict[str, Any]] = []
        for use in uses:
            handler = dispatch.get(use.name)
            content = (
                handler(use.input) if handler is not None else f"error: unknown tool {use.name}"
            )
            results.append({"type": "tool_result", "tool_use_id": use.id, "content": content})
        messages.append({"role": "user", "content": results})

    findings = _findings_from_raw(raw_findings, entries, config, now=now)
    return findings, tokens_in, tokens_out, calls


def _audit_run(
    audit: AuditTrail, db_label: str, run: WatchRun, *, detail: str | None, suppressed: int = 0
) -> None:
    audit.record(
        AuditRecord(
            timestamp=run.started_at,
            action="watch_run",
            target=f"watch:{db_label}",
            reason="autonomous watch cycle",
            outcome=AuditOutcome.OK,
            params={
                "outcome": run.outcome,
                "new_events": run.new_event_count,
                "reported": len(run.finding_ids),
                "suppressed": suppressed,
                "llm_calls": run.llm_calls,
                "tokens_input": run.tokens_input,
                "tokens_output": run.tokens_output,
                "conditions": run.conditions,
            },
            detail=detail,
        )
    )


def _dedup(
    findings: list[WatchFinding],
    open_findings: dict[str, datetime],
    watch_cfg: WatchSettings,
    *,
    now: datetime,
) -> tuple[list[WatchFinding], dict[str, datetime]]:
    """Alert-once dedup. Returns (findings to report, updated open-set).

    A finding is reported when its key is not already open, or its last alert is older than the TTL.
    A key not seen this cycle is pruned once its TTL lapses, so a recurrence re-alerts.
    """

    ttl = timedelta(hours=watch_cfg.dedup_ttl_hours)
    new_open = dict(open_findings)
    current_keys: set[str] = set()
    reported: list[WatchFinding] = []
    for finding in findings:
        key = finding.dedup_key()
        current_keys.add(key)
        previous = open_findings.get(key)
        is_new = (not watch_cfg.dedup) or previous is None or (now - previous) >= ttl
        new_open[key] = now
        if is_new:
            reported.append(finding)

    for key, seen in list(new_open.items()):
        if key not in current_keys and (now - seen) >= ttl:
            del new_open[key]
    return reported, new_open


# --- metrics-evaluation pass (feature 027) ----------------------------------------------------


def _format_window(seconds: int) -> str:
    """Human label for the evaluation window, e.g. 3600 → ``1h``, 1800 → ``30m``."""

    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size and seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def _evaluate_metrics(
    config: Config, *, now: datetime, maintenance: Maintenance | None = None
) -> list[WatchFinding]:
    """Deterministic, OFFLINE pass (feature 027): a finding per interface error counter that climbed
    past the threshold over the window. Reads the feature-021 metrics store read-only.

    Returns ``[]`` when disabled, when the store is missing/empty/unreadable, or when nothing
    qualifies — never raises for a missing/malformed store (FR-012). Needs no cloud model (FR-007).
    """

    settings = config.watch.metrics
    if settings is None or not settings.enabled:
        return []
    db_path = settings.db_path.expanduser()
    if not db_path.exists():
        return []

    window_start = now - timedelta(seconds=settings.window_seconds)
    since_us = int(window_start.timestamp() * 1_000_000)
    until_us = int(now.timestamp() * 1_000_000)
    try:
        conn = metricstore.connect_readonly(db_path)
        try:
            increases = metricstore.counter_increases(
                conn,
                metrics=settings.counters,
                since_us=since_us,
                until_us=until_us,
                min_increase=settings.threshold,
            )
        finally:
            conn.close()
    except sqlite3.Error:
        return []  # store locked, mid-write, or lacks the samples table — degrade, never crash

    window_label = _format_window(settings.window_seconds)
    ignore = [pat.lower() for pat in settings.ignore_interfaces]
    findings: list[WatchFinding] = []
    matcher = maintenance if maintenance is not None else _maintenance_for(config)
    for inc in increases:
        if any(pat in inc.ifname.lower() for pat in ignore):
            continue  # skip ignored interfaces (e.g. WireGuard tunnels — benign out-errors)
        if matcher.device(inc.device) is not None:
            continue  # 135: deliberately off
        findings.append(
            WatchFinding(
                id="",
                created_at=now,
                device=inc.device,
                device_address=inc.address or None,
                window_start=window_start,
                window_end=now,
                severity="warning",
                confidence="high",
                summary=(
                    f"{inc.device} {inc.ifname}: +{inc.increase:,} {inc.metric} "
                    f"in the last {window_label}"
                ),
                description=(
                    f"{inc.metric} on {inc.ifname} rose from {inc.start_value:,} to "
                    f"{inc.end_value:,} (+{inc.increase:,}) across {inc.readings} readings in the "
                    f"last {window_label}."
                ),
                evidence=[
                    Evidence(
                        kind="metric",
                        text=(
                            f"{inc.metric} {inc.start_value:,} → {inc.end_value:,} "
                            f"(+{inc.increase:,})"
                        ),
                        at=now,
                        host=inc.device,
                    )
                ],
                source="metrics",
                dedup_extra=f"{inc.ifname}:{inc.metric}",
            )
        )
    return findings


# --- device-reachability pass (feature 030) ---------------------------------------------------


def _format_age(seconds: float) -> str:
    """Compact human age: 665 → ``11m``, 4380 → ``1h 13m``, 90000 → ``1d 1h`` (0 → ``0s``)."""

    s = int(seconds)
    if s < 60:
        return f"{s}s"
    minutes = s // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 24:
        rem_m = minutes % 60
        return f"{hours}h {rem_m}m" if rem_m else f"{hours}h"
    days = hours // 24
    rem_h = hours % 24
    return f"{days}d {rem_h}h" if rem_h else f"{days}d"


def _evaluate_reachability(
    config: Config, *, now: datetime, maintenance: Maintenance | None = None
) -> list[WatchFinding]:
    """Deterministic, OFFLINE pass (feature 030): flag devices that stopped reporting SNMP metrics.

    For each device in the feature-021 metrics store, compare the age of its newest reading against
    ``stale_after_cycles`` × its observed cadence — an **exact** comparison, so a single missed
    cycle is tolerated and two or more consecutive misses alert. When at least ``mass_outage_ratio``
    of the watched devices are stale at once (the collector/host stopped, not one device), coalesce
    into a single finding instead of one per device.

    Returns ``[]`` when disabled, when the store is missing/empty/unreadable, or when nothing
    qualifies — never raises for a missing/malformed store (FR-012). Needs no cloud model and no
    device/network I/O (FR-007/FR-008).
    """

    settings = config.watch.reachability
    if settings is None or not settings.enabled:
        return []
    db_path = settings.db_path.expanduser()
    if not db_path.exists():
        return []

    now_us = int(now.timestamp() * 1_000_000)
    try:
        conn = metricstore.connect_readonly(db_path)
        try:
            # 096/D4: the Linux collector VMs' probe rows share this store — exclude them
            # so a broken SSH probe can never fabricate an SNMP "unreachable" finding.
            freshness = metricstore.device_freshness(
                conn, now_us=now_us, exclude_metric_prefixes=("linux:",)
            )
        finally:
            conn.close()
    except sqlite3.Error:
        return []  # store locked, mid-write, or lacks the samples table — degrade, never crash

    ignore = [pat.lower() for pat in settings.ignore_devices]
    matcher = maintenance if maintenance is not None else _maintenance_for(config)
    watched = [
        f
        for f in freshness
        if f.cadence_us is not None  # need a cadence baseline (thin history ⇒ skip)
        and not any(pat in f.device.lower() for pat in ignore)
        and matcher.device(f.device) is None  # 135: exactly like an ignored device
    ]
    if not watched:
        return []

    stale = [
        f
        for f in watched
        if f.cadence_us is not None
        and (now_us - f.newest_us) > settings.stale_after_cycles * f.cadence_us
    ]
    if not stale:
        return []

    if len(stale) / len(watched) >= settings.mass_outage_ratio:
        return [_coalesced_reachability_finding(stale, len(watched), now=now, now_us=now_us)]
    return [_reachability_finding(f, now=now, now_us=now_us) for f in stale]


def _reachability_finding(
    f: metricstore.DeviceFreshness, *, now: datetime, now_us: int
) -> WatchFinding:
    """Map one stale device to a per-device reachability finding (``source="reachability"``)."""

    cadence_us = f.cadence_us or 0
    age_s = (now_us - f.newest_us) / 1_000_000
    cadence_s = cadence_us / 1_000_000
    missed = int(age_s // cadence_s) if cadence_s else 0
    last_seen = datetime.fromtimestamp(f.newest_us / 1_000_000, tz=now.tzinfo or UTC)
    age_label = _format_age(age_s)
    cadence_label = _format_age(cadence_s)
    addr = f" ({f.address})" if f.address else ""
    return WatchFinding(
        id="",
        created_at=now,
        device=f.device,
        device_address=f.address,
        window_start=last_seen,
        window_end=now,
        severity="warning",
        confidence="high",
        summary=(
            f"{f.device}: no SNMP metrics for {age_label} ({missed} missed cycles); "
            f"last reading {last_seen.strftime('%H:%M')}"
        ),
        description=(
            f"{f.device}{addr} has produced no SNMP readings since {last_seen.isoformat()} — "
            f"{age_label} ago, ~{missed} missed collection cycles at its ~{cadence_label} cadence. "
            f"It is not responding to SNMP (metrics collection)."
        ),
        evidence=[
            Evidence(
                kind="metric",
                text=f"last reading {last_seen.isoformat()} ({age_label} ago)",
                at=now,
                host=f.device,
            )
        ],
        source="reachability",
    )


def _coalesced_reachability_finding(
    stale: list[metricstore.DeviceFreshness], watched_count: int, *, now: datetime, now_us: int
) -> WatchFinding:
    """Collapse a mass outage (≥ ``mass_outage_ratio`` stale) into one finding (FR-014)."""

    names = sorted(f.device for f in stale)
    oldest_us = min(f.newest_us for f in stale)
    oldest_seen = datetime.fromtimestamp(oldest_us / 1_000_000, tz=now.tzinfo or UTC)
    return WatchFinding(
        id="",
        created_at=now,
        device="collection",
        device_address=None,
        window_start=oldest_seen,
        window_end=now,
        severity="warning",
        confidence="high",
        summary=(
            f"Metrics collection appears to have stopped: "
            f"{len(stale)}/{watched_count} devices silent"
        ),
        description=(
            f"{len(stale)} of {watched_count} watched devices have no recent SNMP readings — this "
            f"usually means the collector/host stopped, not each device. "
            f"Silent: {', '.join(names)}."
        ),
        evidence=[
            Evidence(kind="metric", text=f"{name}: no recent SNMP readings", at=now, host=name)
            for name in names
        ],
        source="reachability",
        dedup_extra="collection-stopped",
    )


# --- UISP wireless link pass (feature 034) -----------------------------------------------------

_UISP_LINKS_BASELINE = "uisp-links-baseline.json"


def _load_links_baseline(path: Path) -> dict[str, dict[str, Any]]:
    """The persisted per-link previous observation; ``{}`` (first-run semantics) on any problem."""

    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): v for k, v in data.items() if isinstance(v, dict)}


def _store_links_baseline(path: Path, baseline: dict[str, dict[str, Any]]) -> None:
    """Atomic write (tmp + replace), mirroring ``WatchStore.save_state``."""

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(baseline, indent=2, ensure_ascii=False, sort_keys=True) + "\n")
    os.replace(tmp, path)


def _link_matches_ignore(link: Any, patterns: list[str]) -> bool:
    """Case-insensitive substring match against either endpoint name or the label."""

    if not patterns:
        return False
    haystacks = [
        (link.end_a.name or "").lower(),
        (link.end_b.name or "").lower(),
        link.label.lower(),
    ]
    return any(pat in hay for pat in patterns for hay in haystacks)


def _link_down_finding(link: Any, prev_state: str | None, *, now: datetime) -> WatchFinding:
    context = ", ".join(
        part
        for part in (
            f"SSID {link.ssid}" if link.ssid else None,
            f"{link.frequency_mhz:g} MHz" if link.frequency_mhz is not None else None,
            f"{link.distance_m:g} m apart" if link.distance_m is not None else None,
        )
        if part
    )
    return WatchFinding(
        id="",
        created_at=now,
        device=link.label,
        device_address=None,
        window_start=now,
        window_end=now,
        severity="warning",
        confidence="high",
        summary=(
            f"{link.label}: wireless link down "
            f"(was {prev_state or 'active'}, now {link.state or 'unknown'})"
        ),
        description=(
            f"The UISP controller reported the wireless link {link.label} as "
            f"{link.state or 'unknown'}; at the previous watch observation it was "
            f"{prev_state or 'active'}. "
            + (f"Link: {context}. " if context else "")
            + "A device going dark also raises UISP outage alerts (feature 019); this finding "
            "tracks the link itself."
        ),
        evidence=[
            Evidence(
                kind="link",
                text=f"state {prev_state or 'active'} → {link.state or 'unknown'}",
                at=now,
                host=link.label,
            )
        ],
        source="uisp-links",
        dedup_extra=f"{link.id}:down",
    )


def _link_signal_finding(
    link: Any, prev_signal: float, drop: float, threshold: float, *, now: datetime
) -> WatchFinding:
    return WatchFinding(
        id="",
        created_at=now,
        device=link.label,
        device_address=None,
        window_start=now,
        window_end=now,
        severity="warning",
        confidence="high",
        summary=(
            f"{link.label}: link signal fell {drop:.1f} dB "
            f"({prev_signal:g} → {link.signal_dbm:g} dBm)"
        ),
        description=(
            f"The wireless link {link.label} reports signal {link.signal_dbm:g} dBm, "
            f"{drop:.1f} dB below the {prev_signal:g} dBm baseline (threshold "
            f"{threshold:g} dB) since the previous watch observation. The baseline is held "
            f"while the link stays degraded, so this alerts once and re-arms on recovery."
        ),
        evidence=[
            Evidence(
                kind="link",
                text=f"signal {prev_signal:g} → {link.signal_dbm:g} dBm (−{drop:.1f} dB)",
                at=now,
                host=link.label,
            )
        ],
        source="uisp-links",
        dedup_extra=f"{link.id}:signal",
    )


def _evaluate_uisp_links(
    config: Config,
    audit: AuditTrail,
    *,
    now: datetime,
    dry_run: bool = False,
    maintenance: Maintenance | None = None,
) -> list[WatchFinding]:
    """Deterministic pass (feature 034): flag wireless links that went down or lost signal.

    Reads the live controller link snapshot (two read-only GETs via ``engine.uisp_links``,
    which writes the pass's one audit record) and compares it against the persisted previous
    observation (``uisp-links-baseline.json`` in the watch dir):

    - a link whose baseline state was active and is now present but not active → down finding;
    - a link whose link-level signal fell more than ``signal_drop_db`` below the baseline →
      degradation finding. While degraded the baseline signal is **retained** (not re-based),
      so slow drift accumulates and recovery re-arms the alert; otherwise the baseline tracks.

    First run seeds the baseline silently; a link absent from the snapshot leaves the baseline
    without a finding; ``dry_run`` evaluates but writes nothing. An unreachable controller
    raises (the guarded ``run_cycle`` seam records ``uisp_links_error``; the baseline file is
    untouched). Detection itself needs no cloud model.
    """

    settings = config.watch.uisp_links
    if settings is None or not settings.enabled:
        return []
    uisp_cfg = config.uisp
    if uisp_cfg is None:  # load_config forbids this combination; degrade rather than crash
        return []

    from obi.core.drivers.uisp import UispController
    from obi.core.engine import uisp_links

    report = uisp_links(
        UispController(uisp_cfg),
        audit=audit,
        controller=uisp_cfg.base_url,
        name_filter=uisp_cfg.name_filter,
        ignore_names=uisp_cfg.ignore_names,  # feature 035: UISP-scoped ignore (union ignore_links)
        timeout=uisp_cfg.timeout,
        reason="watch uisp-links pass",
    )
    if report.outcome is not SourceOutcome.REACHABLE:
        detail = f": {report.detail}" if report.detail else ""
        raise RuntimeError(f"uisp controller {report.outcome.value}{detail}")

    # engine.uisp_links already dropped [uisp] ignore_names links; the pass-local ignore_links
    # (alert-only tuning) is applied here on top — the union of the two.
    ignore = [pat.lower() for pat in settings.ignore_links]
    matcher = maintenance if maintenance is not None else _maintenance_for(config)
    links = [
        link
        for link in report.links
        if not _link_matches_ignore(link, ignore)
        # 135: either endpoint (or the label) in maintenance ⇒ exactly like an ignored link
        and matcher.link(link.end_a.name, link.end_b.name, link.label) is None
    ]

    baseline_path = config.watch.watch_dir / _UISP_LINKS_BASELINE
    baseline = _load_links_baseline(baseline_path)
    first_run = not baseline

    findings: list[WatchFinding] = []
    new_baseline: dict[str, dict[str, Any]] = {}
    for link in links:
        prev = baseline.get(link.id)
        prev_state = str(prev["state"]) if prev and prev.get("state") is not None else None
        prev_signal_raw = prev.get("signal_dbm") if prev else None
        prev_signal = (
            float(prev_signal_raw)
            if isinstance(prev_signal_raw, (int, float)) and not isinstance(prev_signal_raw, bool)
            else None
        )

        keep_signal = link.signal_dbm
        if prev is not None:
            prev_active = (prev_state or "").strip().lower() == "active"
            if prev_active and not link.is_active:
                findings.append(_link_down_finding(link, prev_state, now=now))
            if prev_signal is not None and link.signal_dbm is not None:
                drop = prev_signal - link.signal_dbm
                if drop > settings.signal_drop_db:
                    findings.append(
                        _link_signal_finding(
                            link, prev_signal, drop, settings.signal_drop_db, now=now
                        )
                    )
                    # hold the baseline while degraded (re-arms on recovery)
                    keep_signal = prev_signal
        new_baseline[link.id] = {
            "label": link.label,
            "state": link.state,
            "signal_dbm": keep_signal,
        }

    if not dry_run:
        _store_links_baseline(baseline_path, new_baseline)
    return [] if first_run else findings


# --- config-change pass (feature 070) ---------------------------------------------------------

_CONFIG_BASELINE = "config-baseline.json"


def _load_config_baseline(path: Path) -> dict[str, str]:
    """The persisted per-device last-seen commit sha; ``{}`` (first-run) on any problem."""

    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): v for k, v in data.items() if isinstance(v, str)}


def _store_config_baseline(path: Path, baseline: dict[str, str]) -> None:
    """Atomic write (tmp + replace), mirroring ``_store_links_baseline``."""

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(baseline, indent=2, ensure_ascii=False, sort_keys=True) + "\n")
    os.replace(tmp, path)


def _bounded_diff(diff_text: str, device: str, max_lines: int) -> tuple[str, int, int]:
    """Trim a unified diff to the operator-facing essentials and bound it (FR-003).

    Drops git's preamble (``diff --git``/``index``/``---``/``+++`` lines), counts the ± content
    lines (returned as ``(insertions, deletions)``), and truncates the body to ``max_lines`` with a
    marker pointing at ``obi configs diff <device>`` for the rest. An empty diff (an unresolvable
    ref) yields a clear "diff unavailable" note, so the change is never silently dropped.
    """

    lines = [
        line
        for line in diff_text.splitlines()
        if not line.startswith(("diff --git", "index ", "--- ", "+++ "))
    ]
    insertions = sum(1 for line in lines if line.startswith("+") and not line.startswith("+++"))
    deletions = sum(1 for line in lines if line.startswith("-") and not line.startswith("---"))
    if not lines:
        note = f"(diff unavailable — run `obi configs diff {device}` for the full change)"
        return note, insertions, deletions
    kept = lines[:max_lines]
    if len(lines) > max_lines:
        kept.append(
            f"… diff truncated ({max_lines} of {len(lines)} lines); "
            f"run `obi configs diff {device}` for the full diff"
        )
    return "\n".join(kept), insertions, deletions


def _config_change_finding(
    device: str,
    prev_sha: str,
    cur_sha: str,
    diff_text: str,
    settings: WatchConfigChangesSettings,
    *,
    now: datetime,
) -> WatchFinding:
    """Map one changed device to a config-change finding (``source="config"``, FR-002/FR-004).

    Carries the **already-redacted** stored diff (feature 026 redacts before commit), bounded to
    ``settings.max_diff_lines``. ``dedup_extra`` is the new commit sha, so each committed change
    alerts once and a later change re-alerts (FR-006).
    """

    body, insertions, deletions = _bounded_diff(diff_text, device, settings.max_diff_lines)
    short_prev, short_cur = prev_sha[:7], cur_sha[:7]
    return WatchFinding(
        id="",
        created_at=now,
        device=device,
        device_address=None,
        window_start=now,
        window_end=now,
        severity=settings.severity,
        confidence="high",
        summary=f"{device}: config changed (+{insertions}/−{deletions} lines)",
        description=(
            f"The stored config for {device} advanced to a new committed version "
            f"({short_prev} → {short_cur}) since the previous watch observation. "
            f"The diff below is the redacted stored content (feature 026 scrubs secrets before "
            f"commit).\n\n{body}"
        ),
        evidence=[
            Evidence(
                kind="config",
                text=f"{short_prev} → {short_cur} (+{insertions}/−{deletions})",
                at=now,
                host=device,
            )
        ],
        source="config",
        dedup_extra=short_cur,
    )


def _is_reorder_only_advance(repo: Path, identity: str, prev: str, current: str) -> bool:
    """True iff a config advance is export-ordering noise, not an operator change (feature 099).

    Compares the two committed versions' *normalized* line multisets: equal-but-reordered
    (RouterOS 7 re-emitting an entry at a different position) or normalized-identical (the stored
    delta vanishes under current normalization — e.g. a pre-099 version still carrying transient
    ``# Warning:`` lines advancing to its warning-free successor). ``normalize_export`` is
    idempotent, so this is a no-op for content stored after 099. Suppression needs positive
    proof: an unreadable side (file absent at the ref — renamed identity) returns False and the
    pass alerts as before.
    """

    prev_text = configstore.content_at(repo, identity, prev)
    cur_text = configstore.content_at(repo, identity, current)
    if prev_text is None or cur_text is None:
        return False
    prev_norm = configstore.normalize_export(prev_text)
    cur_norm = configstore.normalize_export(cur_text)
    return prev_norm == cur_norm or configstore.is_line_permutation(prev_norm, cur_norm)


def _evaluate_config_changes(
    config: Config, audit: AuditTrail, *, now: datetime, dry_run: bool = False
) -> list[WatchFinding]:
    """Deterministic, OFFLINE pass (feature 070): flag devices whose stored config changed.

    Reads the feature-026 git config store (``settings.repo_path``) read-only: for each device,
    compare its current HEAD commit (``configstore.coverage``) against a persisted per-device
    baseline. A device whose commit advanced since last seen raises a finding carrying the
    ``configstore.diff`` between the baseline and current commits — already redacted, bounded.
    Exception (feature 099): an advance that is pure export-ordering noise (same normalized lines,
    different order) is suppressed — audited instead of alerted, baseline advanced all the same.

    First run seeds the baseline silently (no backlog flood); a device newly appearing is baselined,
    not alerted; a device that vanished from coverage drops out without a finding. ``dry_run``
    evaluates but writes no baseline. Returns ``[]`` when disabled, when the repo is
    missing/empty/unreadable, or when nothing changed — never raises for a missing/malformed store.
    Needs no cloud model and no device/network I/O (FR-008/FR-009).
    """

    settings = config.watch.config_changes
    if settings is None or not settings.enabled:
        return []
    repo = settings.repo_path.expanduser()
    if not (repo / ".git").exists():
        return []  # store not created yet (026 never ingested) — degrade, never crash

    baseline_path = config.watch.watch_dir / _CONFIG_BASELINE
    baseline = _load_config_baseline(baseline_path)
    first_run = not baseline

    ignore = [pat.lower() for pat in settings.ignore_devices]
    findings: list[WatchFinding] = []
    new_baseline: dict[str, str] = {}
    for sd in configstore.coverage(repo):
        if any(pat in sd.identity.lower() for pat in ignore):
            continue  # ignored: don't track, don't alert
        current = sd.last_commit
        if current is None:
            continue  # no commit yet for this file — nothing to track
        prev = baseline.get(sd.identity)
        if prev is not None and prev != current:
            if _is_reorder_only_advance(repo, sd.identity, prev, current):
                audit.record(
                    AuditRecord(
                        timestamp=now,
                        action="watch_config_reorder_suppressed",
                        target=sd.identity,
                        reason=(
                            "config advance is a pure line permutation — RouterOS export "
                            "ordering noise, not an operator change (feature 099)"
                        ),
                        outcome=AuditOutcome.OK,
                        params={"prev": prev, "current": current},
                    )
                )
            else:
                diff_text = configstore.diff(repo, sd.identity, prev, current)  # already redacted
                findings.append(
                    _config_change_finding(sd.identity, prev, current, diff_text, settings, now=now)
                )
        new_baseline[sd.identity] = current  # advance (also seeds a first sighting)

    if not dry_run:
        _store_config_baseline(baseline_path, new_baseline)
    return [] if first_run else findings


# --- config capture-freshness pass (feature 087) -----------------------------------------------

_CONFIG_CAPTURES_BASELINE = "config-captures-baseline.json"


def _capture_stale_finding(
    identity: str,
    last_success: datetime,
    last_outcome: str | None,
    severity: str,
    stale_after: str,
    *,
    now: datetime,
) -> WatchFinding:
    """Map one device whose config capture went stale to a finding (``source="configs"``)."""

    age_label = _format_age(max(0.0, (now - last_success).total_seconds()))
    outcome_note = f" Last attempt outcome: {last_outcome}." if last_outcome else ""
    return WatchFinding(
        id="",
        created_at=now,
        device=identity,
        device_address=None,
        window_start=last_success,
        window_end=now,
        severity=severity,
        confidence="high",
        summary=(
            f"{identity}: config backup capture stale — last successful capture {age_label} ago"
        ),
        description=(
            f"obi has not successfully captured {identity}'s configuration for {age_label} "
            f"(threshold {stale_after}; last success {last_success.isoformat()}), while the "
            f"5-minute backup cron kept trying.{outcome_note} The device may be down, or it is "
            f"up but SSH access broke (rotated credential, removed `ssh` policy, firewall). "
            f"Check `obi configs status` and the device's SSH access for the configured "
            f"backup user (feature 087)."
        ),
        evidence=[
            Evidence(
                kind="store",
                text=(
                    f"capture-state: last success {last_success.isoformat()}, "
                    f"last outcome {last_outcome or 'unknown'}"
                ),
                at=now,
                host=identity,
            )
        ],
        source="configs",
        dedup_extra=f"{identity}:capture-stale",
    )


def _coalesced_capture_stale_finding(
    devices: list[str], tracked_count: int, severity: str, stale_after: str, *, now: datetime
) -> WatchFinding:
    """Collapse a mass capture-staleness (≥ ratio at once) into one finding."""

    names = sorted(devices)
    return WatchFinding(
        id="",
        created_at=now,
        device="configs",
        device_address=None,
        window_start=now,
        window_end=now,
        severity=severity,
        confidence="high",
        summary=(
            f"config backup mass-stale: {len(names)}/{tracked_count} devices have had no "
            f"successful capture in {stale_after}"
        ),
        description=(
            f"{len(names)} of {tracked_count} tracked devices crossed the capture-staleness "
            f"threshold ({stale_after}) in the same watch cycle — this usually means the backup "
            f"pipeline itself broke (host asleep, fleet credential rotated, [configs] SSH key), "
            f"not each device. Stale: {', '.join(names)}."
        ),
        evidence=[
            Evidence(kind="store", text=f"{name}: config capture stale", at=now, host=name)
            for name in names
        ],
        source="configs",
        dedup_extra="capture-mass-stale",
    )


def _evaluate_config_captures(
    config: Config,
    *,
    now: datetime,
    dry_run: bool = False,
    maintenance: Maintenance | None = None,
) -> list[WatchFinding]:
    """Deterministic, OFFLINE pass (feature 087): alert when config capture goes stale.

    Reads the feature-085 capture-state sidecar (written by every ``obi configs ingest``) and
    compares each ROSTER device's capture freshness — last successful capture within
    ``stale_after`` — against the persisted previous observation
    (``config-captures-baseline.json`` in the watch dir):

    - a device the baseline recorded fresh whose last success is now older than the threshold
      → a capture-stale finding (dead device, rotated credential, lost ``ssh`` policy);
    - recovery (stale → fresh) updates the baseline silently;
    - the first run seeds the baseline silently, so devices already stale when the pass is
      enabled never flood.

    Only roster devices count (a renamed device's lingering sidecar entry never alerts), and a
    device with no successful capture on record is skipped (it was never fresh — ``configs
    status`` shows it as "never captured"). ≥ ``mass_outage_ratio`` transitions in one run
    coalesce into a single finding (mirrors 030/072). Returns ``[]`` when disabled, when the
    sidecar is missing/empty, or on the first run — never raises for a malformed store. Needs
    no cloud model and no device I/O.
    """

    settings = config.watch.config_captures
    if settings is None or not settings.enabled:
        return []
    captures = configstore.read_capture_state(settings.repo_path)
    if not captures:
        return []

    roster: set[str] = set()
    try:
        inventory = inv.load(config.inventory_path)
        entries = inventory.entries if inventory is not None else []
        roster = {e.identity for e in entries if e.identity}
    except Exception:  # noqa: BLE001 — roster unreadable: track every sidecar identity instead
        roster = set()
    if not roster:
        roster = set(captures)  # no/empty roster: track every sidecar identity instead

    stale_seconds = duration_to_seconds(
        settings.stale_after, 86400, context="[watch.config_captures] stale_after"
    )
    threshold = now - timedelta(seconds=stale_seconds)
    ignore = [pat.lower() for pat in settings.ignore_devices]
    matcher = maintenance if maintenance is not None else _maintenance_for(config)

    baseline_path = config.watch.watch_dir / _CONFIG_CAPTURES_BASELINE
    baseline = _load_icmp_baseline(baseline_path)  # same {identity: bool} shape + semantics
    first_run = not baseline

    new_baseline: dict[str, bool] = {}
    transitions: list[tuple[str, datetime, str | None]] = []
    for identity, entry in sorted(captures.items()):
        if identity not in roster:
            continue
        if any(pat in identity.lower() for pat in ignore):
            continue
        if matcher.device(identity) is not None:
            continue  # 135: torn-down devices cannot be captured — by design
        raw = entry.get("last_success_at")
        last_success = None
        if isinstance(raw, str):
            try:
                last_success = datetime.fromisoformat(raw)
            except ValueError:
                last_success = None
        if last_success is None:
            continue  # never captured successfully — never fresh, nothing to transition from
        fresh = last_success >= threshold
        outcome = entry.get("last_outcome")
        if baseline.get(identity) is True and not fresh:
            transitions.append(
                (identity, last_success, str(outcome) if outcome is not None else None)
            )
        new_baseline[identity] = fresh

    findings: list[WatchFinding] = []
    if transitions:
        if len(transitions) >= settings.mass_outage_ratio * max(1, len(new_baseline)):
            findings.append(
                _coalesced_capture_stale_finding(
                    [t[0] for t in transitions],
                    len(new_baseline),
                    settings.severity,
                    settings.stale_after,
                    now=now,
                )
            )
        else:
            findings.extend(
                _capture_stale_finding(
                    identity,
                    last_success,
                    outcome,
                    settings.severity,
                    settings.stale_after,
                    now=now,
                )
                for identity, last_success, outcome in transitions
            )

    if not dry_run:
        _store_icmp_baseline(baseline_path, new_baseline)  # same atomic bool-map writer
    return [] if first_run else findings


# --- ICMP reachability pass (feature 072) -----------------------------------------------------

_ICMP_BASELINE = "icmp-baseline.json"


def _load_icmp_baseline(path: Path) -> dict[str, bool]:
    """The persisted per-device last-reported up/down; ``{}`` (first-run) on any problem."""

    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): bool(v) for k, v in data.items() if isinstance(v, bool)}


def _store_icmp_baseline(path: Path, baseline: dict[str, bool]) -> None:
    """Atomic write (tmp + replace), mirroring ``_store_links_baseline``."""

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(baseline, indent=2, ensure_ascii=False, sort_keys=True) + "\n")
    os.replace(tmp, path)


def _icmp_down_finding(
    state: icmpstore.DeviceState, severity: str, *, now: datetime
) -> WatchFinding:
    """Map one device that transitioned up→down to a per-device finding (``source="icmp"``)."""

    down_since = datetime.fromtimestamp(state.last_change_us / 1_000_000, tz=now.tzinfo or UTC)
    age_label = _format_age(max(0.0, now.timestamp() - state.last_change_us / 1_000_000))
    addr = f" ({state.address})" if state.address else ""
    return WatchFinding(
        id="",
        created_at=now,
        device=state.device,
        device_address=state.address or None,
        window_start=down_since,
        window_end=now,
        severity=severity,
        confidence="high",
        summary=(
            f"{state.device}: unreachable by ICMP for {age_label} "
            f"(down since {down_since.strftime('%H:%M')})"
        ),
        description=(
            f"{state.device}{addr} stopped answering ICMP pings from the obi host "
            f"{age_label} ago (down since {down_since.isoformat()}). At the previous watch "
            f"observation it was reachable. This is a direct host-to-device reachability signal "
            f"(feature 072), independent of SNMP."
        ),
        evidence=[
            Evidence(
                kind="probe",
                text=f"ICMP unreachable for {age_label} (since {down_since.isoformat()})",
                at=now,
                host=state.device,
            )
        ],
        source="icmp",
        dedup_extra=f"{state.device}:down",
    )


def _coalesced_icmp_finding(
    devices: list[str], watched_count: int, severity: str, *, now: datetime
) -> WatchFinding:
    """Collapse a mass outage (≥ ``mass_outage_ratio`` down at once) into one finding."""

    names = sorted(devices)
    return WatchFinding(
        id="",
        created_at=now,
        device="reachability",
        device_address=None,
        window_start=now,
        window_end=now,
        severity=severity,
        confidence="high",
        summary=(
            f"ICMP mass outage: {len(names)}/{watched_count} devices went unreachable at once"
        ),
        description=(
            f"{len(names)} of {watched_count} watched devices stopped answering ICMP in the same "
            f"cycle — this usually means a host-side or site-wide path failure, not each device. "
            f"Unreachable: {', '.join(names)}."
        ),
        evidence=[
            Evidence(kind="probe", text=f"{name}: ICMP unreachable", at=now, host=name)
            for name in names
        ],
        source="icmp",
        dedup_extra="icmp-mass-down",
    )


def _evaluate_icmp(config: Config, *, now: datetime, dry_run: bool = False) -> list[WatchFinding]:
    """Deterministic, OFFLINE pass (feature 072): alert on ICMP up→down transitions.

    Reads the current reachability state the ``*/5`` ``obi icmp run`` collector wrote to the
    feature-072 state store, and compares each device against the persisted previous observation
    (``icmp-baseline.json`` in the watch dir):

    - a device the baseline recorded ``up`` and is now ``down`` → a down finding;
    - recovery (baseline down, now up) updates the baseline with **no** message (silent clear);
    - the first run seeds the baseline silently, so devices already down when monitoring begins
      (e.g. off-season playa devices) never alert.

    When at least ``mass_outage_ratio`` of the watched devices transition down in the same run, the
    per-device findings collapse into one coalesced finding. Returns ``[]`` when disabled, when the
    store is missing/locked/empty, or on the first run — never raises for a missing/malformed store
    (the guarded ``run_cycle`` seam also catches). Needs no cloud model and no device I/O.
    """

    settings = config.watch.icmp
    if settings is None or not settings.enabled:
        return []
    db_path = settings.db_path.expanduser()
    if not db_path.exists():
        return []

    try:
        conn = icmpstore.connect_readonly(db_path)
        try:
            states = icmpstore.load_states(conn)
        finally:
            conn.close()
    except sqlite3.Error:
        return []  # store locked, mid-write, or lacks the table — degrade, never crash

    if not states:
        return []

    baseline_path = config.watch.watch_dir / _ICMP_BASELINE
    baseline = _load_icmp_baseline(baseline_path)
    first_run = not baseline

    new_baseline: dict[str, bool] = {}
    down_transitions: list[icmpstore.DeviceState] = []
    for state in states:
        prev_up = baseline.get(state.device)
        if prev_up is True and not state.up:
            down_transitions.append(state)
        new_baseline[state.device] = state.up

    findings: list[WatchFinding] = []
    if down_transitions:
        if len(down_transitions) >= settings.mass_outage_ratio * len(states):
            findings.append(
                _coalesced_icmp_finding(
                    [s.device for s in down_transitions], len(states), settings.severity, now=now
                )
            )
        else:
            findings.extend(
                _icmp_down_finding(s, settings.severity, now=now) for s in down_transitions
            )

    if not dry_run:
        _store_icmp_baseline(baseline_path, new_baseline)
    return [] if first_run else findings


# --- hardware-health pass (feature 096) --------------------------------------------------------

_HEALTH_BASELINE = "health-baseline.json"
# Freshness gate (FR-016): a series older than 3 × the 5-min collection cadence is skipped —
# staleness is the reachability passes' business (030/072), never a health alert.
_HEALTH_FRESH_AFTER_S = 900


def _health_breach_finding(
    breach: health_core.Breach, severity: str, *, now: datetime
) -> WatchFinding:
    """Map one healthy→breached series transition to a finding (``source="health"``)."""

    sensor = breach.label or breach.instance or breach.metric
    value_text = f"{breach.value:g} {breach.unit}".strip()
    return WatchFinding(
        id="",
        created_at=now,
        device=breach.device,
        device_address=None,
        window_start=now,
        window_end=now,
        severity=severity,
        confidence="high",
        summary=f"{breach.device}: {sensor} at {value_text} (limit {breach.limit})",
        description=(
            f"{breach.device}'s {breach.klass} reading {sensor} ({breach.metric}) measures "
            f"{value_text}, outside its configured healthy range ({breach.limit}). At the "
            f"previous watch observation this series was healthy — first observed unhealthy "
            f"this cycle, {now.isoformat()}. Deterministic feature-096 threshold pass over "
            f"the local metrics store; thresholds live in [watch.health]."
        ),
        evidence=[
            Evidence(
                kind="metric",
                text=f"{breach.metric}[{sensor}] = {value_text} (limit {breach.limit})",
                at=now,
                host=breach.device,
            )
        ],
        source="health",
        dedup_extra=f"{breach.device}:{sensor}:{breach.klass}",
    )


def _coalesced_health_finding(
    transitions: list[health_core.Breach], tracked_count: int, severity: str, *, now: datetime
) -> WatchFinding:
    """Many series breaching in one cycle (site heat event, power problem) → ONE finding."""

    devices = sorted({b.device for b in transitions})
    listed = ", ".join(devices[:8]) + (", …" if len(devices) > 8 else "")
    return WatchFinding(
        id="",
        created_at=now,
        device="health",
        window_start=now,
        window_end=now,
        severity=severity,
        confidence="high",
        summary=(
            f"Fleet-wide health event: {len(transitions)} of {tracked_count} tracked series "
            f"breached in one cycle ({listed})"
        ),
        description=(
            f"{len(transitions)} health series across {len(devices)} device(s) transitioned "
            f"from healthy to breaching their thresholds in the same watch cycle — an "
            f"environment-scale event (site heat, power trouble) rather than one component. "
            f"Devices: {', '.join(devices)}. Individual findings are coalesced to avoid a "
            f"page storm; `obi health status` has the per-sensor detail."
        ),
        evidence=[
            Evidence(
                kind="metric",
                text=(
                    f"{b.device}: {b.label or b.instance or b.metric} = "
                    f"{b.value:g} {b.unit} (limit {b.limit})".strip()
                ),
                at=now,
                host=b.device,
            )
            for b in transitions[:10]
        ],
        source="health",
        dedup_extra="health-mass-breach",
    )


def _evaluate_health(
    config: Config,
    *,
    now: datetime,
    dry_run: bool = False,
    maintenance: Maintenance | None = None,
) -> list[WatchFinding]:
    """Deterministic, OFFLINE pass (feature 096): alert on healthy→breached transitions of
    hardware-health series — temperature, PSU/fan status, optics, sustained CPU/memory
    pressure, disk — over the feature-021 metrics store's health data.

    Shape and guarantees mirror 072/087: opt-in via ``[watch.health]``; first run seeds the
    baseline silently (an already-degraded sensor never alerts until it recovers and breaks
    again); recovery is a silent clear; ≥ ``mass_outage_ratio`` of tracked series breaching
    at once coalesce into one finding; ``dry_run`` writes no baseline. A device with no
    fresh sample is skipped entirely (FR-016 — staleness belongs to 030/072) and its
    baseline knowledge is carried forward. CPU/memory breaches must hold across
    ``sustained_samples`` consecutive readings (FR-014). No cloud model, no device I/O.
    """

    settings = config.watch.health
    if settings is None or not settings.enabled:
        return []
    db_path = settings.db_path.expanduser()
    if not db_path.exists():
        return []

    now_us = int(now.timestamp() * 1_000_000)
    thresholds = health_core.thresholds_from_settings(settings)
    matcher = maintenance if maintenance is not None else _maintenance_for(config)

    def _sensor_ignored(r: health_core.HealthReading) -> bool:
        hay = f"{r.label or ''} {r.metric}".lower()
        return any(sub.lower() in hay for sub in settings.ignore_sensors)

    confirmed: list[health_core.Breach] = []
    tracked_keys: list[str] = []
    try:
        conn = metricstore.connect_readonly(db_path)
        try:
            fleet = health_core.fleet_status(
                conn, now_us=now_us, stale_after_s=_HEALTH_FRESH_AFTER_S
            )
            mem_totals = {
                dev.device: r.value
                for dev in fleet
                for r in dev.readings
                if r.metric == "memTotalKb" and r.value
            }
            candidates: list[health_core.Breach] = []
            for dev in fleet:
                if dev.stale:
                    continue  # FR-016: no fresh sample ⇒ not this pass's business
                if any(sub.lower() in dev.device.lower() for sub in settings.ignore_devices):
                    continue
                if matcher.device(dev.device) is not None:
                    continue  # 135: deliberately off
                if settings.ignore_sensors:
                    dev = replace(dev, readings=[r for r in dev.readings if not _sensor_ignored(r)])
                breaches, tracked = health_core.evaluate_device(dev, thresholds)
                candidates.extend(breaches)
                tracked_keys.extend(tracked)
            for b in candidates:
                if b.klass in health_core.SUSTAINED_CLASSES:
                    raw = metricstore.recent_values(
                        conn,
                        device=b.device,
                        metric=b.metric,
                        ifindex=b.instance,
                        n=settings.sustained_samples,
                    )
                    if b.metric == "memUsedKb":
                        total = mem_totals.get(b.device)
                        if not total:
                            continue
                        values = [
                            100.0 * metricstore.display_value(b.metric, v) / total for v in raw
                        ]
                        limit = thresholds.memory_max_percent
                    else:
                        values = [metricstore.display_value(b.metric, v) for v in raw]
                        limit = (
                            thresholds.cpu_max_percent
                            if b.klass == "cpu"
                            else thresholds.memory_max_percent
                        )
                    if not health_core.sustained(values, limit=limit, n=settings.sustained_samples):
                        continue  # a spike, not pressure — the series stays healthy (FR-014)
                confirmed.append(b)
        finally:
            conn.close()
    except sqlite3.Error:
        return []  # store locked or mid-write — degrade, never crash the cron

    baseline_path = config.watch.watch_dir / _HEALTH_BASELINE
    # Same {key: bool} shape as the 072/087 baselines, so their I/O helpers are reused.
    baseline = _load_icmp_baseline(baseline_path)
    first_run = not baseline

    breached_by_key = {b.series_key: b for b in confirmed}
    new_baseline = dict(baseline)  # carry forward series not evaluated this cycle
    transitions: list[health_core.Breach] = []
    for key in tracked_keys:
        healthy_now = key not in breached_by_key
        if baseline.get(key) is True and not healthy_now:
            transitions.append(breached_by_key[key])
        new_baseline[key] = healthy_now

    findings: list[WatchFinding] = []
    if transitions:
        # A "mass" event needs plurality — one breach on a small tracked set is not a storm.
        if len(transitions) > 1 and len(transitions) >= settings.mass_outage_ratio * max(
            1, len(new_baseline)
        ):
            findings.append(
                _coalesced_health_finding(
                    transitions, len(new_baseline), settings.severity, now=now
                )
            )
        else:
            findings = [_health_breach_finding(b, settings.severity, now=now) for b in transitions]

    if not dry_run:
        _store_icmp_baseline(baseline_path, new_baseline)
    return [] if first_run else findings


# --- self-monitoring pass (feature 062, A4) ---------------------------------------------------


def _backlog_finding(remaining: int, oldest_age_s: float | None, *, now: datetime) -> WatchFinding:
    """The lag alarm: the watcher has unjudged candidates older/more than the thresholds."""

    age_label = _format_age(oldest_age_s) if oldest_age_s is not None else "unknown age"
    window_start = now - timedelta(seconds=oldest_age_s) if oldest_age_s is not None else now
    return WatchFinding(
        id="",
        created_at=now,
        device="watcher",
        device_address=None,
        window_start=window_start,
        window_end=now,
        severity="warning",
        confidence="high",
        summary=(
            f"obi watch is falling behind: {remaining:,} unjudged log events, "
            f"oldest {age_label} old"
        ),
        description=(
            f"The watcher has {remaining:,} judgeable log events it has not reached yet; the "
            f"oldest is {age_label} old. New problems will not be reported until it catches up. "
            f"This usually means log ingest is outpacing the judge window, or the cron stopped "
            f"between runs. Check `obi watch status` and the ingest crons."
        ),
        evidence=[
            Evidence(
                kind="log",
                text=f"{remaining:,} candidates beyond the cursor; oldest {age_label} old",
                at=now,
                host="watcher",
            )
        ],
        source="watchdog",
        dedup_extra="backlog",
    )


def _pipeline_silent_finding(ingest_age_s: float, *, now: datetime) -> WatchFinding:
    """The ingest-stall alarm: nothing has been stored for longer than the threshold, so the
    watcher is blind for a different reason than a backlog (FR-012)."""

    age_label = _format_age(ingest_age_s)
    return WatchFinding(
        id="",
        created_at=now,
        device="watcher",
        device_address=None,
        window_start=now - timedelta(seconds=ingest_age_s),
        window_end=now,
        severity="warning",
        confidence="high",
        summary=f"No new logs ingested for {age_label} — the log pipeline may be silent",
        description=(
            f"No log events have been stored for {age_label}. The watcher has nothing to judge, "
            f"which blinds it just as a backlog would. This usually points at the ingest side "
            f"(the rsync/collector crons or a controller poll), not the watcher itself. Check the "
            f"log-sync crons and collector reachability."
        ),
        evidence=[
            Evidence(
                kind="log",
                text=f"newest stored event is {age_label} old",
                at=now,
                host="watcher",
            )
        ],
        source="watchdog",
        dedup_extra="pipeline-silent",
    )


def _evaluate_selfcheck(
    watch_cfg: WatchSettings,
    *,
    remaining_candidates: int,
    oldest_candidate_age_s: float | None,
    ingest_age_s: float | None,
    now: datetime,
) -> list[WatchFinding]:
    """Deterministic self-monitoring (feature 062, A4): a ``watchdog`` finding when the watcher is
    behind (backlog) or the log pipeline has gone silent. No cloud model. Rides the finding
    pipeline (dedup TTL + Slack floor). ``0`` thresholds disable a check (FR-011/FR-012/FR-016).

    The count threshold is clock-skew-immune; the age thresholds are clamped at 0 by the caller so
    a future-stamped device row cannot produce a foolish age (D6)."""

    minutes = watch_cfg.lag_alarm_minutes
    events = watch_cfg.lag_alarm_events
    if remaining_candidates > 0:
        time_over = (
            minutes > 0
            and oldest_candidate_age_s is not None
            and oldest_candidate_age_s > minutes * 60
        )
        count_over = events > 0 and remaining_candidates > events
        if time_over or count_over:
            return [_backlog_finding(remaining_candidates, oldest_candidate_age_s, now=now)]
    elif minutes > 0 and ingest_age_s is not None and ingest_age_s > minutes * 60:
        return [_pipeline_silent_finding(ingest_age_s, now=now)]
    return []


def _render_heartbeat(
    state: SelfCheckState,
    *,
    now: datetime,
    remaining_candidates: int,
    lag_seconds: int,
    healthy: bool,
) -> str:
    """The periodic health DM (feature 062, A4) — plain text, no model. Never a false 'healthy':
    a lagging or degraded period is rendered as such (FR-013)."""

    since = (
        state.last_heartbeat_at.strftime("%Y-%m-%d %H:%M") if state.last_heartbeat_at else "startup"
    )
    header = ":white_check_mark: obi watch healthy" if healthy else ":warning: obi watch degraded"
    lines = [
        f"{header} — heartbeat",
        (
            f"Since {since}: {state.period_runs} run(s), {state.period_events:,} event(s) judged, "
            f"{state.period_findings} finding(s), {state.period_alerts} alert(s) delivered."
        ),
    ]
    if remaining_candidates > 0:
        lines.append(
            f"Backlog: {remaining_candidates:,} unjudged event(s), "
            f"oldest {_format_age(lag_seconds)} old."
        )
    else:
        lines.append("Backlog: none — caught up to the newest log.")
    if state.period_conditions:
        conds = ", ".join(f"{name}×{n}" for name, n in sorted(state.period_conditions.items()))
        lines.append(f"Degraded conditions this period: {conds}.")
    return "\n".join(lines)


# --- the cycle --------------------------------------------------------------------------------


def run_cycle(
    config: Config,
    store: WatchStore,
    *,
    audit: AuditTrail,
    now: datetime,
    llm_client: LLMClient | None = None,
    notifier: Notifier | None = None,
    since_now: bool = False,
    dry_run: bool = False,
    recorder: AuditRecorder | None = None,
) -> WatchRun:
    """Run one read-only watcher cycle and return its :class:`WatchRun` summary.

    Feature 042: when a ``recorder`` is provided the whole cycle runs inside one capture
    scope (``watch-<timestamp>-<suffix>``), so the judge's model calls, the investigation
    loop's tool calls and device commands, every legacy intent record, and each report all
    land in the audit event store under one reconstructable session.
    """

    # Lazy import: the core stays importable without the chat layer (the judge already imports
    # chat lazily inside _judge for the same reason).
    from obi.core.llm import AuditedLLMClient

    session_id = f"watch-{now.astimezone(UTC):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"
    with audit_scope(recorder, "watch", session_id):
        wrapped = AuditedLLMClient(llm_client) if llm_client is not None else None
        return _run_cycle_scoped(
            config,
            store,
            audit=audit,
            now=now,
            llm_client=wrapped,
            notifier=notifier,
            since_now=since_now,
            dry_run=dry_run,
        )


def _run_cycle_scoped(
    config: Config,
    store: WatchStore,
    *,
    audit: AuditTrail,
    now: datetime,
    llm_client: LLMClient | None = None,
    notifier: Notifier | None = None,
    since_now: bool = False,
    dry_run: bool = False,
) -> WatchRun:
    """The cycle body (see :func:`run_cycle`).

    Reads events ingested since the stored watermark (an ingest id) and, when ``llm_client`` is
    provided, judges the new window for problems. Alert-once dedup suppresses a finding already
    open for the same device. Advances the watermark only on success and only past entries it
    analyzed (FR-002, FR-012). ``dry_run`` writes nothing and advances nothing. The cycle never
    raises on a handled condition (store/LLM unavailable): it records the condition and returns a
    ``degraded`` run. With no cloud it does nothing but advance the watermark — `obi watch` is a
    cloud-reasoning feature (Principle VI carve-out).
    """

    if config.logs is None:
        raise ValueError("obi watch requires a [logs] section")
    db_label = str(config.logs.db_path)
    watch_cfg = config.watch
    conditions: list[str] = []
    cursor_before = store.load_cursor()
    open_findings = store.load_open()
    selfcheck_state = store.load_selfcheck()
    # 135: ONE maintenance set for the whole cycle (config ∪ UISP flags), threaded everywhere.
    maintenance = _maintenance_for(config)
    exclusions = _window_exclusions(config, maintenance)

    # feature 062 (A1): the judge window is drawn by a CANDIDATE query — exclusions applied in SQL
    # before the LIMIT — so ignorable noise can no longer consume the budget and hold the cursor
    # back. ``upto`` is the newest id snapshotted at scan start; scanned range is (cursor, upto].
    candidates: list[tuple[int, LogEvent]] = []
    detail: str | None = None
    store_ok = False
    new_cursor_id: int | None = None
    upto_id: int | None = None
    ingest_ts_us: int | None = None
    remaining_candidates = 0
    oldest_candidate_us: int | None = None
    try:
        conn = logstore.connect_readonly(config.logs.db_path)
        try:
            store_ok = True
            newest = logstore.newest_event(conn)
            if newest is not None:
                upto_id, ingest_ts_us = newest
            if since_now or cursor_before is None:
                new_cursor_id = upto_id  # jump the watermark to now; analyze nothing this run
            else:
                start_id = cursor_before.id
                if upto_id is not None and upto_id > start_id:
                    candidates = logstore.query_watch_candidates(
                        conn,
                        after_id=start_id,
                        upto_id=upto_id,
                        limit=watch_cfg.max_window_events,
                        exclusions=exclusions,
                    )
                    # Cursor advance (D3): fewer candidates than the budget ⇒ the whole range was
                    # examined, jump to upto (noise can never stall the cursor, FR-004); budget hit
                    # ⇒ advance only to the last candidate, the remainder is next run's (FR-002).
                    if len(candidates) < watch_cfg.max_window_events:
                        new_cursor_id = upto_id
                    else:
                        new_cursor_id = candidates[-1][0]
                else:
                    new_cursor_id = start_id  # nothing new; hold the watermark
                # Measure how far behind we remain AFTER the advance (A4, FR-010); the store-error
                # path skips this so an unmeasurable store never raises a false lag alarm (D6).
                remaining_candidates, oldest_candidate_us = logstore.watch_window_stats(
                    conn,
                    after_id=new_cursor_id,
                    upto_id=upto_id if upto_id is not None else new_cursor_id,
                    exclusions=exclusions,
                )
        finally:
            conn.close()
    except sqlite3.Error as exc:
        # Log store missing, locked, or mid-rsync: the LOG pass degrades (record it, leave the
        # watermark unadvanced with new_cursor_id=None), but the cycle CONTINUES so the independent,
        # offline metrics pass (feature 027) still runs (FR-009/FR-012).
        conditions.append("store_unavailable")
        detail = str(exc)
        candidates = []
        store_ok = False
        new_cursor_id = None

    # Backstop filter (D2): the SQL already excluded noise; this catches any residual SQL↔Python
    # drift so a divergence can only waste budget, never widen what the judge sees (FR-005). Then
    # collapse identical repeats into one annotated line each before the judge reads them (A3).
    window = _filter_window(candidates, config, maintenance)
    entries = collapse_window(window)

    # LLM judgement — the watcher's one detector; runs only when a client is available (FR-013).
    llm_findings: list[WatchFinding] = []
    tokens_in = tokens_out = llm_calls = 0
    if entries:
        if llm_client is None:
            conditions.append("llm_unavailable")
        else:
            from obi.core.llm import ChatUnavailable

            try:
                llm_findings, tokens_in, tokens_out, llm_calls = _judge(
                    config, llm_client, entries, audit=audit, now=now
                )
                detail = "log window sent to cloud model"
            except ChatUnavailable as exc:
                conditions.append("llm_unavailable")
                detail = str(exc)
            except Exception as exc:  # noqa: BLE001 - a cycle must never crash the cron (FR-012)
                conditions.append("llm_error")
                detail = str(exc)

    # Metrics-evaluation pass (feature 027): a deterministic, offline detector that runs beside the
    # LLM pass, independent of it (no cloud, no log window) — so a metric problem is caught even
    # when the model is unreachable. Guarded so it never crashes the cron (FR-009/FR-012).
    metrics_findings: list[WatchFinding] = []
    try:
        metrics_findings = _evaluate_metrics(config, now=now, maintenance=maintenance)
    except Exception as exc:  # noqa: BLE001 - a cycle must never crash the cron
        conditions.append("metrics_error")
        detail = f"{detail}; metrics: {exc}" if detail else f"metrics: {exc}"

    # Device-reachability pass (feature 030): a deterministic, offline detector beside the metrics
    # pass — flags a device that stopped reporting SNMP entirely. Independent of the LLM/log and
    # metrics passes, and guarded so it never crashes the cron (FR-009/FR-012).
    reachability_findings: list[WatchFinding] = []
    try:
        reachability_findings = _evaluate_reachability(config, now=now, maintenance=maintenance)
    except Exception as exc:  # noqa: BLE001 - a cycle must never crash the cron
        conditions.append("reachability_error")
        detail = f"{detail}; reachability: {exc}" if detail else f"reachability: {exc}"

    # UISP wireless-link pass (feature 034): a deterministic detector beside the others. Unlike
    # them it reads the controller live (two read-only GETs); an unreachable controller lands
    # here — condition recorded, baseline preserved, other passes unaffected (FR-011).
    uisp_link_findings: list[WatchFinding] = []
    try:
        uisp_link_findings = _evaluate_uisp_links(
            config, audit, now=now, dry_run=dry_run, maintenance=maintenance
        )
    except Exception as exc:  # noqa: BLE001 - a cycle must never crash the cron
        conditions.append("uisp_links_error")
        detail = f"{detail}; uisp_links: {exc}" if detail else f"uisp_links: {exc}"

    # Config-change pass (feature 070): a deterministic, offline detector beside the others — flags
    # a device whose stored config (feature-026 git store) advanced to a new committed version.
    # Reads only the local repo (no device/network I/O); independent of every other pass and guarded
    # so it never crashes the cron (FR-009/FR-010).
    config_findings: list[WatchFinding] = []
    try:
        config_findings = _evaluate_config_changes(config, audit, now=now, dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001 - a cycle must never crash the cron
        conditions.append("config_error")
        detail = f"{detail}; config: {exc}" if detail else f"config: {exc}"

    # ICMP reachability pass (feature 072): a deterministic, offline detector beside the others —
    # alerts on up→down transitions in the feature-072 ICMP state store (populated by the */5 `obi
    # icmp run` collector). Reads only the local store (no device/network I/O), independent of every
    # other pass, and guarded so it never crashes the cron.
    icmp_findings: list[WatchFinding] = []
    try:
        icmp_findings = _evaluate_icmp(config, now=now, dry_run=dry_run)
        # Feature 095 (FR-013): while the dash pager is enabled it pages the SAME store's
        # up→down transitions within ~a minute — an hourly duplicate here would double-page
        # every outage. The pass still RUNS (its baseline stays fresh, so disabling the dash
        # pager resumes 072 cleanly with no stale-baseline alert burst); only its findings are
        # suppressed, with a condition line whenever that actually mattered.
        if icmp_findings and config.dash is not None and config.dash.pager.enabled:
            conditions.append("icmp pass superseded by dash pager")
            icmp_findings = []
    except Exception as exc:  # noqa: BLE001 - a cycle must never crash the cron
        conditions.append("icmp_error")
        detail = f"{detail}; icmp: {exc}" if detail else f"icmp: {exc}"

    # Config capture-freshness pass (feature 087): alerts when obi stops being able to READ a
    # device's config (085 sidecar goes stale) — the backup-health twin of the 070 change pass.
    # Reads only the sidecar + roster; guarded like its siblings.
    capture_findings: list[WatchFinding] = []
    try:
        capture_findings = _evaluate_config_captures(
            config, now=now, dry_run=dry_run, maintenance=maintenance
        )
    except Exception as exc:  # noqa: BLE001 - a cycle must never crash the cron
        conditions.append("config_captures_error")
        detail = f"{detail}; config_captures: {exc}" if detail else f"config_captures: {exc}"

    # Hardware-health pass (feature 096): alerts when a health series (temps/PSU/fan/optics/
    # CPU/memory/disk from the 021 store's health profiles + Linux probe) transitions from
    # healthy to breaching its threshold. Reads only the local store; guarded like its siblings.
    health_findings: list[WatchFinding] = []
    try:
        health_findings = _evaluate_health(
            config, now=now, dry_run=dry_run, maintenance=maintenance
        )
    except Exception as exc:  # noqa: BLE001 - a cycle must never crash the cron
        conditions.append("health_error")
        detail = f"{detail}; health: {exc}" if detail else f"health: {exc}"

    # Self-monitoring pass (feature 062, A4): a deterministic detector that flags the watcher's OWN
    # lag (or a silent log pipeline) so it can never go blind unnoticed. Only when the store was
    # readable — an unmeasurable store must not raise a false alarm (D6). No cloud model.
    oldest_age_s: float | None = None
    ingest_age_s: float | None = None
    selfcheck_findings: list[WatchFinding] = []
    if store_ok:
        if oldest_candidate_us is not None:
            oldest_age_s = max(0.0, now.timestamp() - oldest_candidate_us / 1_000_000)
        if ingest_ts_us is not None:
            ingest_age_s = max(0.0, now.timestamp() - ingest_ts_us / 1_000_000)
        try:
            selfcheck_findings = _evaluate_selfcheck(
                watch_cfg,
                remaining_candidates=remaining_candidates,
                oldest_candidate_age_s=oldest_age_s,
                ingest_age_s=ingest_age_s,
                now=now,
            )
        except Exception as exc:  # noqa: BLE001 - a cycle must never crash the cron
            conditions.append("selfcheck_error")
            detail = f"{detail}; selfcheck: {exc}" if detail else f"selfcheck: {exc}"

    findings = (
        llm_findings
        + metrics_findings
        + reachability_findings
        + uisp_link_findings
        + config_findings
        + icmp_findings
        + capture_findings
        + health_findings
        + selfcheck_findings
    )
    for index, finding in enumerate(findings, start=1):
        finding.id = _finding_id(now, finding.device, index)

    # Alert-once dedup: report only findings not already open (or whose TTL has lapsed).
    reported, open_after = _dedup(findings, open_findings, watch_cfg, now=now)
    suppressed = len(findings) - len(reported)

    if not dry_run:
        # 069: render every human-facing timestamp in the operator's log timezone (config.logs is
        # guaranteed non-None here — the cycle raised at entry otherwise).
        display_tz = config.logs.tzinfo()
        for finding in reported:
            store.write_finding(finding, display_tz=display_tz)
            # feature 042: the on-disk report is a human communication artifact — audit it.
            record_event(
                "report",
                summary=f"watch finding: {finding.summary}",
                payload={
                    "channel": "local",
                    "report_id": finding.id,
                    "severity": finding.severity,
                    "title": finding.summary,
                    "body": render_markdown(finding, display_tz=display_tz),
                },
                target=finding.device or None,
            )

    # Slack delivery (feature 015): push the qualifying findings (post-dedup ∩ at/above the floor)
    # to the operator. Best-effort and failure-isolated — a failure is recorded as a condition (so
    # the run reads as ``degraded``, exit still 0) and never blocks the cycle or the on-disk report
    # (FR-007). Reports are written above regardless of the floor; delivery is the only gated step.
    alerts_delivered = 0
    alerts_failed = 0
    if notifier is not None and not dry_run and reported:
        floor = _severity_rank(watch_cfg.slack_min_severity)
        qualifying = [f for f in reported if _severity_rank(f.severity) >= floor]
        if qualifying:
            try:
                result = notifier.deliver(qualifying)
                alerts_delivered = result.delivered
                alerts_failed = result.failed
                if result.failed:
                    conditions.append(f"slack delivery failed: {result.error}")
            except Exception as exc:
                # A Notifier must not raise (it returns failures), but never let it crash the cron.
                alerts_failed = len(qualifying)
                conditions.append(f"slack delivery failed: {type(exc).__name__}")

    # Advance the watermark per the A1 cursor-advance decision (D3); a store error leaves it
    # unchanged. ``new_cursor_id`` was chosen inside the store scope above.
    cursor_after: WatchCursor | None = (
        WatchCursor(id=new_cursor_id) if new_cursor_id is not None else cursor_before
    )

    # Heartbeat (feature 062, A4): accumulate this run and, on cadence, DM a health summary so
    # silence reads as confirmed-good rather than a blind watcher. No model call; needs Slack
    # delivery. Dry-run is inert (FR-015): no accumulation, no send, no state mutation.
    heartbeat_sent = False
    if not dry_run:
        selfcheck_state.period_runs += 1
        selfcheck_state.period_events += len(window)
        selfcheck_state.period_findings += len(reported)
        selfcheck_state.period_alerts += alerts_delivered
        for cond in conditions:
            key = cond.split(":", 1)[0].strip()
            selfcheck_state.period_conditions[key] = (
                selfcheck_state.period_conditions.get(key, 0) + 1
            )
        due = watch_cfg.heartbeat_hours > 0 and (
            selfcheck_state.last_heartbeat_at is None
            or (now - selfcheck_state.last_heartbeat_at)
            >= timedelta(hours=watch_cfg.heartbeat_hours)
        )
        if due and notifier is not None:
            healthy = remaining_candidates == 0 and not selfcheck_state.period_conditions
            text = _render_heartbeat(
                selfcheck_state,
                now=now,
                remaining_candidates=remaining_candidates,
                lag_seconds=int(oldest_age_s or 0),
                healthy=healthy,
            )
            try:
                status = notifier.deliver_status(text)
                if status.failed:
                    conditions.append(f"heartbeat delivery failed: {status.error}")
                else:
                    heartbeat_sent = True
                    selfcheck_state.last_heartbeat_at = now
                    selfcheck_state.period_runs = 0
                    selfcheck_state.period_events = 0
                    selfcheck_state.period_findings = 0
                    selfcheck_state.period_alerts = 0
                    selfcheck_state.period_conditions = {}
            except Exception as exc:  # noqa: BLE001 - a Notifier must not raise; never crash cron
                conditions.append(f"heartbeat delivery failed: {type(exc).__name__}")

    outcome = "degraded" if conditions else ("findings" if reported else "no_op")
    run = WatchRun(
        started_at=now,
        finished_at=_utcnow(),
        new_event_count=len(window),
        outcome=outcome,
        tokens_input=tokens_in,
        tokens_output=tokens_out,
        llm_calls=llm_calls,
        cursor_before=cursor_before,
        cursor_after=cursor_after,
        finding_ids=[finding.id for finding in reported],
        conditions=conditions,
        alerts_delivered=alerts_delivered,
        alerts_failed=alerts_failed,
        scanned_events=len(candidates),
        window_lines=len(entries),
        remaining_candidates=remaining_candidates,
        lag_seconds=int(oldest_age_s or 0),
        ingest_age_seconds=int(ingest_age_s) if ingest_age_s is not None else None,
        heartbeat_sent=heartbeat_sent,
    )
    _audit_run(audit, db_label, run, detail=detail, suppressed=suppressed)
    if not dry_run:
        store.save_state(cursor_after, run, open_findings=open_after, selfcheck=selfcheck_state)
        store.prune(watch_cfg.retention_days, now=now)
    return run
