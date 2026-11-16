"""Status dashboard core (feature 095): the minute cycle's model, evaluator, and state files.

One green/yellow/red/stale board answering "is everything OK right now?". The cycle
(``obi dash run``, a ``* * * * *`` cron) collects — the 072 ICMP sweep in-process, a netwatch
table print per router, two reference pings from the border router, plus the existing 5-minute
stores — and hands everything to :func:`evaluate`, a **pure** function that folds the inputs
into five worst-of tiles and persists ``board.json``. The web route and ``obi dash show`` render
that file verbatim; the pager (``pager.py`` half of this module) diffs the red-set against a
held baseline. No LLM anywhere; every device interaction is a read-only diagnostic through
existing seams (Constitution I/VI — this surface stays fully offline-capable on the LAN).

State ordering is the load-bearing rule: ``stale`` outranks ``green`` (a data gap is never "all
good", FR-009) but never reaches ``yellow``/``red`` (a gap is not a measured failure) and never
pages. Collection lives in the cycle, judgment lives here — evaluators take already-gathered
inputs so every rule is testable offline (Constitution II).
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager, nullcontext, suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from obi.core.audit import record_event
from obi.core.config import Config, DashInternetLink, DashSettings, duration_to_seconds
from obi.core.icmpstore import DeviceState
from obi.core.maintenance import Maintenance, MaintenanceSummary

#: The injectable ICMP prober (the 072 contract): address → (reachable, avg_rtt_us | None).
ProbeFn = Callable[[str], tuple[bool, int | None]]

# Mirrored (deliberately, to avoid an import cycle) by config._DASH_SIGNALS; a test pins them
# equal. Fixed board order — renderers rely on it.
# 121 appended "mesh" and "lighting". They are DISPLAY-ONLY: config._DASH_PAGEABLE_SIGNALS is
# the (smaller) set the pager may act on, and naming one of these in [dash.pager] signals is a
# config error. See specs/121-mesh-hardening/contracts/dash-signals.md.
SIGNALS = ("fleet", "netwatch", "ping", "internet", "pipeline", "mesh", "lighting")

#: Signals emitted ONLY when their subsystem is configured. The other five always render —
#: as a stale placeholder if a cycle gathered nothing, because for those "no data" is a gap
#: worth showing. For these two it is not: a machine with no mesh radio has no mesh to report.
OPTIONAL_SIGNALS = ("mesh", "lighting")

#: 128: the signals whose health the tower's status band reflects — the pager's set, and
#: deliberately NOT every signal: a display-only mesh/lighting red pages nobody and must not
#: blink the tower. Mirrors config._DASH_PAGEABLE_SIGNALS (inlined there to avoid the
#: config→dash import cycle); a test pins the two tuples equal.
PAGEABLE_SIGNALS = ("fleet", "netwatch", "ping", "internet", "pipeline")

#: Rollup order, weakest first. ``stale`` sits between green and yellow: it taints an all-green
#: rollup (you cannot read "all good" off missing data) without ever counting as a failure.
STATES = ("green", "stale", "yellow", "red")

_STATE_RANK = {state: rank for rank, state in enumerate(STATES)}

#: A tile input older than 3× its cadence + 60 s renders stale (the ``collector_health`` rule).
STALE_MULTIPLIER = 3
STALE_GRACE_S = 60.0

BOARD_FILE = "board.json"
NETWATCH_STATE_FILE = "netwatch-state.json"
PAGER_STATE_FILE = "pager-state.json"


def worst(states: Iterable[str], *, default: str = "green") -> str:
    """The worst state in ``states`` per :data:`STATES` ordering (``default`` when empty)."""

    result = default
    for state in states:
        if _STATE_RANK.get(state, 0) > _STATE_RANK.get(result, 0):
            result = state
    return result


def _fmt_duration(seconds: float) -> str:
    """Compact single-unit duration for item details: ``42s`` / ``14m`` / ``3h`` / ``2d``."""

    seconds = max(0.0, seconds)
    if seconds < 90:
        return f"{round(seconds)}s"
    if seconds < 90 * 60:
        return f"{round(seconds / 60)}m"
    if seconds < 36 * 3600:
        return f"{round(seconds / 3600)}h"
    return f"{round(seconds / 86400)}d"


# --- the board model ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Item:
    """One evaluated unit inside a signal (a device, a netwatch pair, an uplink, a feed)."""

    key: str
    label: str
    state: str
    detail: str = ""
    metrics: dict[str, float | int | None] = field(default_factory=dict)
    since_us: int | None = None
    # 135: the maintenance label ("uisp", "config", "config: <reason>") when this item is
    # deliberately off — shown, but out of every rollup, the pager and the status band.
    maintenance: str | None = None

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "key": self.key,
            "label": self.label,
            "state": self.state,
            "detail": self.detail,
            "metrics": self.metrics,
            "since_us": self.since_us,
        }
        if self.maintenance is not None:
            payload["maintenance"] = self.maintenance
        return payload


@dataclass(frozen=True)
class Tile:
    """One signal's rollup: worst of its items, with the freshest input's age at eval time."""

    signal: str
    state: str
    items: list[Item]
    cadence_s: int
    data_age_s: float | None = None
    # 135: set when the whole signal is in maintenance (mesh/lighting): the tile is shown with
    # its own state, but the board's overall state ignores it.
    maintenance: str | None = None

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "signal": self.signal,
            "state": self.state,
            "items": [item.to_json() for item in self.items],
            "cadence_s": self.cadence_s,
            "data_age_s": self.data_age_s,
        }
        if self.maintenance is not None:
            payload["maintenance"] = self.maintenance
        return payload


@dataclass(frozen=True)
class Board:
    """The evaluated board: worst-of tiles in :data:`SIGNALS` order plus cycle notes."""

    generated_at_us: int
    state: str
    tiles: list[Tile]
    notes: list[str] = field(default_factory=list)
    # 135: the effective maintenance set this cycle (None when nothing is in maintenance).
    maintenance: MaintenanceSummary | None = None

    @property
    def generated_at(self) -> str:
        return datetime.fromtimestamp(self.generated_at_us / 1_000_000, tz=UTC).isoformat()

    def tile(self, signal: str) -> Tile | None:
        for tile in self.tiles:
            if tile.signal == signal:
                return tile
        return None

    def to_json(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "generated_at_us": self.generated_at_us,
            "state": self.state,
            "tiles": [tile.to_json() for tile in self.tiles],
            "notes": self.notes,
            "maintenance": self.maintenance.to_json() if self.maintenance is not None else None,
        }


def board_from_json(payload: dict[str, Any]) -> Board:
    """Rebuild a :class:`Board` from ``board.json`` (forward-compatible: unknown keys ignored)."""

    tiles = [
        Tile(
            signal=str(t.get("signal", "")),
            state=str(t.get("state", "stale")),
            items=[
                Item(
                    key=str(i.get("key", "")),
                    label=str(i.get("label", "")),
                    state=str(i.get("state", "stale")),
                    detail=str(i.get("detail", "")),
                    metrics=dict(i.get("metrics") or {}),
                    since_us=int(i["since_us"]) if i.get("since_us") is not None else None,
                    maintenance=str(i["maintenance"]) if i.get("maintenance") else None,
                )
                for i in t.get("items", [])
                if isinstance(i, dict)
            ],
            cadence_s=int(t.get("cadence_s", 60)),
            data_age_s=float(t["data_age_s"]) if t.get("data_age_s") is not None else None,
            maintenance=str(t["maintenance"]) if t.get("maintenance") else None,
        )
        for t in payload.get("tiles", [])
        if isinstance(t, dict)
    ]
    return Board(
        generated_at_us=int(payload.get("generated_at_us", 0)),
        state=str(payload.get("state", "stale")),
        tiles=tiles,
        notes=[str(n) for n in payload.get("notes", [])],
        maintenance=MaintenanceSummary.from_json(payload.get("maintenance")),
    )


# --- state-file IO (atomic; absent files are a state, never an error) --------------------------


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Write ``payload`` as JSON via tmp + ``os.replace`` (the watch-baseline idiom)."""

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def load_json(path: Path) -> dict[str, Any] | None:
    """Load a JSON object from ``path``; ``None`` when absent/unreadable/not-an-object."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def write_board(state_dir: Path, board: Board) -> None:
    write_json_atomic(state_dir / BOARD_FILE, board.to_json())


def load_board(state_dir: Path) -> Board | None:
    payload = load_json(state_dir / BOARD_FILE)
    return board_from_json(payload) if payload is not None else None


# --- evaluator inputs (gathered by the cycle, judged here) -------------------------------------


@dataclass(frozen=True)
class UplinkReading:
    """One uplink's latest measured rates (bits/s) and how old that sample is."""

    link: DashInternetLink
    in_bps: float | None
    out_bps: float | None
    sample_age_s: float | None
    error: str | None = None


@dataclass(frozen=True)
class TrackedNode:
    """One allowlisted mesh node as the attached radio last knew it (feature 121).

    ``last_heard_age_s`` is ``None`` when the node is configured but absent from the radio's
    node table — the eviction case feature 117 was built around, and a real signal in itself.
    """

    node_id: str
    label: str
    last_heard_age_s: float | None = None
    snr_db: float | None = None
    hops_away: int | None = None


@dataclass(frozen=True)
class MeshHealth:
    """One cycle's passive read of the mesh subsystem. Gathering this NEVER transmits."""

    reachable: bool
    detail: str = ""
    nodes: list[TrackedNode] = field(default_factory=list)
    #: Age of the newest fix in the 117 position store; ``None`` = nothing recorded yet.
    newest_fix_age_s: float | None = None
    #: True when that newest fix carried no device fix time and is aged by observation.
    newest_fix_dated_by_observation: bool = False
    #: 123: the daemon has the port on loan (operator standby) — data is last-known, and
    #: the tile must say so (yellow) rather than pretend the radio is being heard live.
    standby: bool = False


@dataclass(frozen=True)
class LightingController:
    """One allowlisted WLED controller's reachability and current look (feature 121)."""

    name: str
    reachable: bool
    detail: str = ""
    on: bool | None = None
    brightness: int | None = None
    # 128: the verbatim /json/state body, kept so the status-band reconciler can judge
    # drift off THIS cycle's read instead of issuing a second GET. Never serialized.
    raw: dict[str, Any] | None = None


@dataclass(frozen=True)
class LightingHealth:
    """One cycle's read of the lighting controllers. Read-only — never sets a light."""

    controllers: list[LightingController] = field(default_factory=list)


@dataclass(frozen=True)
class DashInputs:
    """Everything one cycle gathered. ``None`` means "no data" — that tile renders stale.

    Per-item ages ride the inputs themselves; ``notes`` are cycle conditions
    (a store that would not open, a snapshot that was absent) surfaced on the board verbatim.
    """

    fleet: list[DeviceState] | None = None
    # Controller-flagged maintenance identities (UISP meta.maintenance): shown yellow on the
    # fleet tile but excluded from its rollup — planned work must not turn the wall red.
    fleet_maintenance: set[str] = field(default_factory=set)
    # 135: the effective matcher (config ∪ UISP flags). When set it supersedes
    # ``fleet_maintenance``; when None, evaluate() builds one from ``fleet_maintenance``.
    maintenance: Maintenance | None = None
    netwatch: list[RouterNetwatch] | None = None
    ping: list[RefProbe] | None = None
    internet: list[UplinkReading] | None = None
    pipeline: list[PipelineFeed] | None = None
    # 121 display-only signals. ``None`` here means NOT CONFIGURED, and the evaluator emits no
    # tile at all — deliberately different from the other signals, where ``None`` means
    # "configured but this cycle gathered nothing" and renders a stale placeholder. A machine
    # with no mesh radio does not have a mesh gap; it has no mesh.
    mesh: MeshHealth | None = None
    lighting: LightingHealth | None = None
    notes: list[str] = field(default_factory=list)
    # The previous cycle's carried state (netwatch counters, ping debounce) as loaded from
    # netwatch-state.json by the cycle — evaluate() itself never touches the filesystem.
    carried: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class NetwatchTarget:
    """One mapped ``/tool/netwatch`` row (contracts/routeros-netwatch-read.md).

    Every wire field is optional (observed live: ``disabled`` and the script keys come and go
    row-to-row; absent ``disabled`` means enabled). ``done_tests``/``failed_tests`` are
    cumulative and RESET when netwatch restarts. ``rtt_avg_ms``/``loss_pct`` are captured
    opportunistically — the fleet's ``simple`` probes never carry them, an operator-upgraded
    ``icmp`` probe does.
    """

    name: str
    host: str
    up: bool
    probe_type: str = "simple"
    since: str | None = None
    disabled: bool = False
    done_tests: int | None = None
    failed_tests: int | None = None
    rtt_avg_ms: float | None = None
    loss_pct: float | None = None


@dataclass(frozen=True)
class RouterNetwatch:
    """One router's netwatch read: its targets, or the error that made them unknown."""

    router: str
    targets: list[NetwatchTarget] = field(default_factory=list)
    error: str | None = None


@dataclass(frozen=True)
class RefProbe:
    """One reference ping result from the border router (the 028 seam's aggregate view)."""

    address: str
    label: str
    rtt_avg_ms: float | None
    loss_pct: float | None
    error: str | None = None


@dataclass(frozen=True)
class PipelineFeed:
    """One pipeline-health item: a collector feed's age vs its own staleness rule.

    ``state`` set ⇒ a pre-judged item (config-capture staleness, pager delivery) that bypasses
    the age rule; ``None`` ⇒ judged from ``age_s``/``stale_after_s`` (quiet past the rule ⇒
    yellow; past 3× the rule ⇒ red; never run ⇒ yellow with a cron hint).
    """

    name: str
    age_s: float | None
    stale_after_s: float
    detail: str = ""
    state: str | None = None


@dataclass(frozen=True)
class EvalResult:
    """What one evaluation produced: the board plus the next cycle's carried state."""

    board: Board
    state: dict[str, Any]


# --- per-signal evaluators ---------------------------------------------------------------------


def _stale_tile(signal: str, cadence_s: int, hint: str) -> Tile:
    """The honest no-data tile: never green, names what is missing (FR-009/FR-016)."""

    item = Item(key="no-data", label="no data", state="stale", detail=hint)
    return Tile(signal=signal, state="stale", items=[item], cadence_s=cadence_s)


def _tile_state(items: list[Item], *, age_s: float | None, cadence_s: int) -> str:
    """Worst of the items, floored at ``stale`` when the tile's input has aged out.

    A stale input never hides a measured red (old bad news beats false comfort) but does
    taint a green tile — you cannot read "all good" off data the collector stopped writing.
    """

    state = worst([item.state for item in items])
    if age_s is not None and age_s > cadence_s * STALE_MULTIPLIER + STALE_GRACE_S:
        state = worst([state, "stale"])
    return state


def _as_matcher(maintenance: Maintenance | set[str] | frozenset[str] | None) -> Maintenance:
    """A bare identity set (the pre-135 UISP-flag seam) becomes a flags-only matcher."""

    if isinstance(maintenance, Maintenance):
        return maintenance
    return Maintenance.build(None, maintenance or ())


def _mark_maintenance(item: Item, label: str) -> Item:
    """The one maintenance treatment (095/128, generalised by 135) — pure.

    Visible and labeled, yellow whatever the probe says — except a probing gap still reads
    stale (that is about us, not it). Callers keep the returned item OUT of every rollup.
    """

    return replace(
        item,
        state="stale" if item.state == "stale" else "yellow",
        # The 095/128 prefix is kept verbatim (link widgets use it too); the source/reason
        # rides ``Item.maintenance`` and the board's maintenance summary.
        detail=f"maintenance · {item.detail}" if item.detail else "maintenance",
        metrics={**item.metrics, "maintenance": 1},
        maintenance=label,
    )


def _eval_fleet(
    states: list[DeviceState],
    settings: DashSettings,
    *,
    now_us: int,
    maintenance: Maintenance | set[str] | frozenset[str] = frozenset(),
) -> Tile:
    """Fleet reachability: red = confirmed down, yellow = recovered within the flap window.

    A device in ``maintenance`` (the controller's operator-set flag) renders yellow whatever
    the probe says — visible, labeled — but is EXCLUDED from the tile rollup: planned work
    must never turn the wall red, and yellow never pages (US3), so it never DMs either.
    """

    flap_window_s = duration_to_seconds(settings.flap_window, 600, context="[dash] flap_window")
    matcher = _as_matcher(maintenance)
    items: list[Item] = []
    rollup_states: list[str] = []
    newest_us: int | None = None
    for device in states:
        newest_us = device.updated_us if newest_us is None else max(newest_us, device.updated_us)
        age_s = (now_us - device.updated_us) / 1_000_000
        changed_ago_s = (now_us - device.last_change_us) / 1_000_000
        rtt_ms = device.last_rtt_us / 1000 if device.last_rtt_us is not None else None
        label = matcher.device(device.device)
        metrics: dict[str, float | int | None] = {"rtt_ms": rtt_ms, "age_s": round(age_s, 1)}
        if age_s > 60 * STALE_MULTIPLIER + STALE_GRACE_S:
            state, detail = "stale", f"no probe for {_fmt_duration(age_s)}"
        elif not device.up:
            state = "red"
            detail = f"down {_fmt_duration(changed_ago_s)} ({device.address})"
        elif changed_ago_s <= flap_window_s:
            state = "yellow"
            detail = f"recovered {_fmt_duration(changed_ago_s)} ago"
        else:
            state = "green"
            detail = f"up · rtt {rtt_ms:.1f} ms" if rtt_ms is not None else "up"
        item = Item(
            key=device.device,
            label=device.device,
            state=state,
            detail=detail,
            metrics=metrics,
            since_us=device.last_change_us,
        )
        if label is not None:
            item = _mark_maintenance(item, label)
        else:
            rollup_states.append(state)
        items.append(item)
    tile_age_s = (now_us - newest_us) / 1_000_000 if newest_us is not None else None
    tile_state = worst(rollup_states)
    if tile_age_s is not None and tile_age_s > 60 * STALE_MULTIPLIER + STALE_GRACE_S:
        tile_state = worst([tile_state, "stale"])
    return Tile(
        signal="fleet",
        state=tile_state,
        items=items,
        cadence_s=60,
        data_age_s=round(tile_age_s, 1) if tile_age_s is not None else None,
    )


def _eval_internet(readings: list[UplinkReading], settings: DashSettings, *, now_us: int) -> Tile:
    """Uplink utilization vs configured capacity, BOTH directions — labeled 5-minute data.

    The detail names in and out against the provisioned rate ("in 612/2500 Mb/s (24%) ·
    out 98/2500 Mb/s (4%)"); the state judges the busier direction, so an upload-saturated
    asymmetric circuit yellows exactly like a download-saturated one."""

    items: list[Item] = []
    freshest: float | None = None
    for reading in readings:
        link = reading.link
        label = link.label or link.interface
        key = f"{link.device}|{link.interface}"
        capacity_bps = link.capacity_mbps * 1_000_000
        if reading.error is not None or reading.in_bps is None or reading.out_bps is None:
            items.append(
                Item(
                    key=key,
                    label=label,
                    state="stale",
                    detail=reading.error or "no rate sample",
                    metrics={"sample_age_s": reading.sample_age_s},
                )
            )
            continue
        if reading.sample_age_s is not None:
            freshest = (
                reading.sample_age_s if freshest is None else min(freshest, reading.sample_age_s)
            )
        in_pct = reading.in_bps / capacity_bps * 100
        out_pct = reading.out_bps / capacity_bps * 100
        pct = max(in_pct, out_pct)
        stale_after = 300 * STALE_MULTIPLIER + STALE_GRACE_S
        if reading.sample_age_s is None or reading.sample_age_s > stale_after:
            state = "stale"
        elif pct >= settings.internet_down_pct:
            state = "red"
        elif pct >= settings.internet_warn_pct:
            state = "yellow"
        else:
            state = "green"
        detail = (
            f"in {reading.in_bps / 1e6:.0f}/{link.capacity_mbps:.0f} Mb/s ({in_pct:.0f}%)"
            f" · out {reading.out_bps / 1e6:.0f}/{link.capacity_mbps:.0f} Mb/s ({out_pct:.0f}%)"
            + (f" · {_fmt_duration(reading.sample_age_s)} old" if reading.sample_age_s else "")
        )
        items.append(
            Item(
                key=key,
                label=label,
                state=state,
                detail=detail,
                metrics={
                    "in_bps": round(reading.in_bps),
                    "out_bps": round(reading.out_bps),
                    "in_pct": round(in_pct, 1),
                    "out_pct": round(out_pct, 1),
                    "pct": round(pct, 1),
                    "capacity_mbps": link.capacity_mbps,
                    "sample_age_s": reading.sample_age_s,
                },
            )
        )
    return Tile(
        signal="internet",
        state=_tile_state(items, age_s=freshest, cadence_s=300),
        items=items,
        cadence_s=300,
        data_age_s=freshest,
    )


def evaluate(inputs: DashInputs, settings: DashSettings, *, now_us: int) -> EvalResult:
    """Fold one cycle's inputs into the board (pure — all I/O stays in the cycle).

    A ``None`` input renders that tile stale with a hint naming the fixing command; the
    internet tile with zero configured links renders green-muted (unconfigured is a choice,
    not a gap). Carried state (netwatch counters, ping debounce) round-trips through ``state``.
    """

    tiles: list[Tile] = []
    matcher = (
        inputs.maintenance
        if inputs.maintenance is not None
        else _as_matcher(inputs.fleet_maintenance)
    )

    if inputs.fleet is None:
        tiles.append(_stale_tile("fleet", 60, "no reachability data — run: obi dash run (cron)"))
    else:
        tiles.append(_eval_fleet(inputs.fleet, settings, now_us=now_us, maintenance=matcher))

    def _prev(signal: str) -> dict[str, Any]:
        section = inputs.carried.get(signal)
        return section if isinstance(section, dict) else {}

    state: dict[str, Any] = {}
    if inputs.netwatch is None:
        tiles.append(_stale_tile("netwatch", 60, "no netwatch poll — run: obi dash run (cron)"))
        state["netwatch"] = _prev("netwatch")  # keep memory across a skipped poll
    else:
        netwatch_tile, netwatch_state = _eval_netwatch(
            inputs.netwatch,
            settings,
            now_us=now_us,
            prev=_prev("netwatch"),
            maintenance=matcher,
        )
        tiles.append(netwatch_tile)
        state["netwatch"] = netwatch_state

    if inputs.ping is None:
        tiles.append(_stale_tile("ping", 60, "no reference probe — run: obi dash run (cron)"))
        state["ping"] = _prev("ping")
    else:
        ping_tile, ping_state = _eval_ping(inputs.ping, settings, now_us=now_us, prev=_prev("ping"))
        tiles.append(ping_tile)
        state["ping"] = ping_state

    if not settings.internet_links:
        item = Item(
            key="none", label="no uplinks", state="green", detail="no internet_links configured"
        )
        tiles.append(Tile(signal="internet", state="green", items=[item], cadence_s=300))
    elif inputs.internet is None:
        tiles.append(_stale_tile("internet", 300, "no metrics store — check: obi metrics status"))
    else:
        tiles.append(_eval_internet(inputs.internet, settings, now_us=now_us))

    if inputs.pipeline is None:
        tiles.append(_stale_tile("pipeline", 60, "no pipeline health — run: obi dash run (cron)"))
    else:
        tiles.append(_eval_pipeline(inputs.pipeline, now_us=now_us))

    # 121: absent, not stale, when the subsystem is unconfigured — there is no gap to report
    # on a machine with no mesh radio or no lighting. Both tiles are display-only.
    if inputs.mesh is not None:
        tiles.append(_signal_maintenance(_eval_mesh(inputs.mesh, settings), matcher))
    if inputs.lighting is not None:
        tiles.append(_signal_maintenance(_eval_lighting(inputs.lighting), matcher))

    board = Board(
        generated_at_us=now_us,
        # 135: a whole signal in maintenance is shown but never decides the overall state.
        state=worst([tile.state for tile in tiles if tile.maintenance is None]),
        tiles=tiles,
        notes=list(inputs.notes),
    )
    return EvalResult(board=board, state=state)


def _signal_maintenance(tile: Tile, matcher: Maintenance) -> Tile:
    """Mark a whole display-only signal (mesh, lighting) as in maintenance — pure (135 FR-009)."""

    label = matcher.signal(tile.signal)
    if label is None:
        return tile
    items = [_mark_maintenance(item, label) for item in tile.items]
    return replace(tile, items=items, state=worst([i.state for i in items]), maintenance=label)


def desired_status(board: Board) -> str | None:
    """The status-band color one board asks for (128 research D1) — pure.

    Red iff any PAGEABLE tile carries a red ITEM (the same item-level set ``run_pager``
    keys on — never ``board.state``, which display-only tiles can redden); yellow iff no
    red but a pageable tile is yellow or stale (degraded observability is degradation);
    green otherwise. ``None`` when the board holds no pageable tiles at all — nothing
    evaluable, so the reconciler must leave the bands alone rather than guess.
    """

    pageable = [tile for tile in board.tiles if tile.signal in PAGEABLE_SIGNALS]
    if not pageable:
        return None
    # 135: maintenance items never count (they render yellow and sit outside the tile
    # rollup already; the explicit check keeps the band honest if that ever changes).
    if any(
        item.state == "red" and item.maintenance is None for tile in pageable for item in tile.items
    ):
        return "red"
    if any(tile.state in ("yellow", "stale") for tile in pageable):
        return "yellow"
    return "green"


# --- 121: the two display-only signals ---------------------------------------------------
#
# Neither can page (config._DASH_PAGEABLE_SIGNALS) and the mesh collector cannot transmit.
# Contract: specs/121-mesh-hardening/contracts/dash-signals.md.


def _age_phrase(seconds: float) -> str:
    """A compact age for tile detail: "3m", "4h", "2d"."""

    if seconds < 90:
        return "just now"
    if seconds < 5400:
        return f"{int(seconds // 60)}m ago"
    if seconds < 172_800:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86_400)}d ago"


def _eval_mesh(health: MeshHealth, settings: DashSettings) -> Tile:
    """The MESH tile: is the radio attached, and is the tracked node still being heard?

    Pure. Every input was gathered passively — the collector reads the node table and the 117
    position store, never the locate engine, so evaluating this tile costs no airtime.
    """

    heard_yellow_s = duration_to_seconds(
        settings.mesh.heard_yellow_after, 7200, context="[dash.mesh] heard_yellow_after"
    )
    heard_red_s = duration_to_seconds(
        settings.mesh.heard_red_after, 43200, context="[dash.mesh] heard_red_after"
    )
    fix_yellow_s = duration_to_seconds(
        settings.mesh.fix_yellow_after, 21600, context="[dash.mesh] fix_yellow_after"
    )

    items: list[Item] = []
    if not health.reachable:
        # The 2026-08-31 shape. Red rather than stale: the radio is a physical thing that is
        # either attached or is not, and "not" is a state someone must act on.
        items.append(
            Item(
                key="radio",
                label="mesh radio",
                state="red",
                detail=health.detail or "not reachable",
            )
        )
    else:
        # 123: standby is deliberate and self-expiring, so it is yellow, not red — but it
        # is not green either: the tile must never pretend a loaned-out radio is listening.
        items.append(
            Item(
                key="radio",
                label="mesh radio",
                state="yellow" if health.standby else "green",
                detail=health.detail
                or ("radio on loan (standby)" if health.standby else "attached"),
            )
        )
        for node in health.nodes:
            if node.last_heard_age_s is None:
                items.append(
                    Item(
                        key=node.node_id,
                        label=node.label,
                        state="yellow",
                        detail="not in the radio's node table",
                    )
                )
                continue
            age = node.last_heard_age_s
            state = "green"
            if age > heard_red_s:
                state = "red"
            elif age > heard_yellow_s:
                state = "yellow"
            detail = f"last heard {_age_phrase(age)}"
            if node.snr_db is not None:
                detail += f", SNR {node.snr_db:g}"
            items.append(
                Item(
                    key=node.node_id,
                    label=node.label,
                    state=state,
                    detail=detail,
                    metrics={"last_heard_age_s": round(age), "snr_db": node.snr_db},
                )
            )

    if health.newest_fix_age_s is None:
        items.append(
            Item(
                key="history",
                label="recorded fixes",
                state="green",
                detail="none recorded yet",
            )
        )
    else:
        age = health.newest_fix_age_s
        # "~" mirrors `obi mesh track`: the device gave no fix time, so this is when WE saw it.
        marker = "~" if health.newest_fix_dated_by_observation else ""
        items.append(
            Item(
                key="history",
                label="newest fix",
                state="yellow" if age > fix_yellow_s else "green",
                detail=f"{marker}{_age_phrase(age)}",
                metrics={"newest_fix_age_s": round(age)},
            )
        )
    # cadence_s reports the SERIAL read interval, not the dash cycle: that is how often the
    # radio half of this tile can actually change, and renderers show it to the operator.
    return Tile(
        signal="mesh",
        state=worst([i.state for i in items]),
        items=items,
        cadence_s=round(
            duration_to_seconds(
                settings.mesh.read_interval, 300, context="[dash.mesh] read_interval"
            )
        ),
    )


def _eval_lighting(health: LightingHealth) -> Tile:
    """The LIGHTING tile: can obi still reach the portal controllers?

    Reachable-but-off is GREEN with detail. The portal being dark is a decision someone made,
    not a fault to chase — only unreachability is a problem here.

    An unreachable controller is **yellow, not red** (operator decision 2026-09-01, on the
    day this shipped). DASH is the network ops wall and its rollup feeds ``dash_status``, the
    primary "how's the network" answer on the radio and in Slack — a decorative LED
    controller returning 502 must not make obi tell a caller the network is red. Yellow still
    surfaces it; it just does not outrank the network.
    """

    items: list[Item] = []
    for controller in health.controllers:
        if not controller.reachable:
            items.append(
                Item(
                    key=controller.name,
                    label=controller.name,
                    state="yellow",
                    detail=controller.detail or "unreachable",
                )
            )
            continue
        if controller.on is None:
            detail = "reachable"
        else:
            detail = "on" if controller.on else "off"
            if controller.on and controller.brightness is not None:
                detail += f", brightness {controller.brightness}"
        items.append(
            Item(
                key=controller.name,
                label=controller.name,
                state="green",
                detail=detail,
                metrics={"brightness": controller.brightness},
            )
        )
    if not items:
        items.append(
            Item(key="none", label="no controllers", state="green", detail="none configured")
        )
    return Tile(signal="lighting", state=worst([i.state for i in items]), items=items, cadence_s=60)


def load_carried_state(state_dir: Path) -> dict[str, Any]:
    """The previous cycle's carried state from ``netwatch-state.json`` (absent ⇒ empty)."""

    return load_json(state_dir / NETWATCH_STATE_FILE) or {}


def write_carried_state(state_dir: Path, state: dict[str, Any]) -> None:
    """Persist the carried state written back by :func:`evaluate` (atomic)."""

    write_json_atomic(state_dir / NETWATCH_STATE_FILE, state)


# --- netwatch: the wire mapper + evaluator (US2) -----------------------------------------------

# RouterOS renders time quantities as unit-suffixed strings ("2ms320us", "1s20ms"); type=icmp
# netwatch stats arrive that way. Numeric values are taken as milliseconds as-is.
_MS_UNITS = {"us": 0.001, "ms": 1.0, "s": 1000.0, "m": 60_000.0, "h": 3_600_000.0}
_MS_TOKEN = re.compile(r"(\d+(?:\.\d+)?)(us|ms|s|m|h)")


def _opportunistic_ms(value: object) -> float | None:
    """A best-effort milliseconds read of a RouterOS time value; ``None`` when unreadable."""

    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().lower()
    matches = _MS_TOKEN.findall(text)
    if matches and "".join(f"{a}{u}" for a, u in matches) == text.replace(" ", ""):
        return sum(float(amount) * _MS_UNITS[unit] for amount, unit in matches)
    try:
        return float(text)
    except ValueError:
        return None


def _opportunistic_pct(value: object) -> float | None:
    """A best-effort percentage read (``12``, ``12.5``, ``"12%"``); ``None`` when unreadable."""

    if value is None or isinstance(value, bool):
        return None
    try:
        return float(str(value).strip().rstrip("%"))
    except ValueError:
        return None


def _opportunistic_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, (float, str)):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def map_netwatch(record: dict[str, Any]) -> NetwatchTarget:
    """Map one raw ``/tool/netwatch`` row (contracts/routeros-netwatch-read.md).

    Every key is optional on the wire (observed live: ``disabled``/script keys come and go
    row-to-row; absent ``disabled`` = enabled). ``type=simple`` rows carry only the counters;
    rtt/loss are captured opportunistically so an operator-upgraded ``icmp`` probe lights up
    with real latency, no code change.
    """

    status = str(record.get("status") or "").lower()
    host = str(record.get("host") or "")
    return NetwatchTarget(
        name=str(record.get("name") or host),
        host=host,
        up=status == "up",
        probe_type=str(record.get("type") or "simple"),
        since=str(record["since"]) if record.get("since") else None,
        disabled=bool(record.get("disabled", False)),
        done_tests=_opportunistic_int(record.get("done-tests")),
        failed_tests=_opportunistic_int(record.get("failed-tests")),
        rtt_avg_ms=_opportunistic_ms(record.get("rtt-avg")),
        loss_pct=_opportunistic_pct(record.get("loss-percent")),
    )


#: Loss over the window is judged only after this many completed tests — a handful of samples
#: makes one lost probe read as tens of percent (analysis finding U2's minimum-sample rule).
NETWATCH_MIN_TESTS = 10


def _eval_netwatch(
    routers: list[RouterNetwatch],
    settings: DashSettings,
    *,
    now_us: int,
    prev: dict[str, Any],
    maintenance: Maintenance | set[str] | frozenset[str] = frozenset(),
) -> tuple[Tile, dict[str, Any]]:
    """Per-pair up/down + windowed counter-delta loss (+ opportunistic rtt/loss thresholds).

    Counter memory rides ``prev``/the returned state: a short ring of ``(at_us, done, failed)``
    samples per pair, cleared on a counter DECREASE (netwatch restart — observed live: a config
    edit resets ``done-tests``/``since``). ``since`` is the router-local clock — display only.

    128: a pair whose target matches a ``maintenance`` identity (the controller's operator-set
    flag, matched against the netwatch comment or host) gets the fleet tile's exact treatment
    — rendered yellow, labeled, excluded from the tile rollup — so planned work never turns
    the wall red, never pages, and never reddens the tower's status band (ignore-if-down).
    """

    loss_window_s = duration_to_seconds(
        settings.netwatch_loss_window, 300, context="[dash] netwatch_loss_window"
    )
    matcher = _as_matcher(maintenance)
    items: list[Item] = []
    rollup_items: list[Item] = []

    def _add(item: Item, *, in_maintenance: str | None = None) -> None:
        if in_maintenance is not None:
            # A down/lossy probe in maintenance reads yellow, never red (128's treatment).
            items.append(_mark_maintenance(item, in_maintenance))
        else:
            items.append(item)
            rollup_items.append(item)

    state: dict[str, Any] = {}
    for router in sorted(routers, key=lambda r: r.router):
        if router.error is not None:
            _add(
                Item(
                    key=router.router,
                    label=router.router,
                    state="stale",
                    detail=f"netwatch poll failed: {router.error}",
                ),
                # 135 FR-008: a torn-down router's failed poll must not stale the tile.
                in_maintenance=matcher.link(router.router),
            )
            continue
        for target in router.targets:
            if target.disabled:
                continue
            key = f"{router.router}|{target.host}"
            label = f"{router.router}→{target.name or target.host}"
            # 135: a probe FROM or TOWARD a maintenance site, or the ptp-less alias of a
            # UISP-flagged radio (``siteA-siteB-11`` ⇐ ``siteA-ptp-siteB-11``).
            in_maintenance = matcher.link(router.router, target.name, target.host)

            prev_entry = prev.get(key)
            samples = list(prev_entry.get("samples", [])) if isinstance(prev_entry, dict) else []
            samples = [
                s
                for s in samples
                if isinstance(s, dict) and all(k in s for k in ("at_us", "done", "failed"))
            ]
            if target.done_tests is not None and target.failed_tests is not None:
                if samples and (
                    target.done_tests < samples[-1]["done"]
                    or target.failed_tests < samples[-1]["failed"]
                ):
                    samples = []  # counter reset (netwatch restarted) — the window starts over
                samples.append(
                    {"at_us": now_us, "done": target.done_tests, "failed": target.failed_tests}
                )
                samples = [s for s in samples if now_us - s["at_us"] <= loss_window_s * 1_000_000]
            state[key] = {"samples": samples}

            window_loss: float | None = None
            window_tests = 0
            if len(samples) >= 2:
                window_tests = samples[-1]["done"] - samples[0]["done"]
                failed = samples[-1]["failed"] - samples[0]["failed"]
                if window_tests > 0:
                    window_loss = max(0.0, failed / window_tests * 100)

            metrics: dict[str, float | int | None] = {
                "loss_pct": target.loss_pct if target.loss_pct is not None else window_loss,
                "rtt_ms": target.rtt_avg_ms,
                "window_tests": window_tests,
            }
            if not target.up:
                since = f" (since {target.since})" if target.since else ""
                _add(
                    Item(key=key, label=label, state="red", detail=f"down{since}", metrics=metrics),
                    in_maintenance=in_maintenance,
                )
                continue

            reasons: list[str] = []
            if target.loss_pct is not None and target.loss_pct >= settings.netwatch_loss_warn_pct:
                reasons.append(f"loss {target.loss_pct:.0f}%")
            elif (
                window_loss is not None
                and window_tests >= NETWATCH_MIN_TESTS
                and window_loss >= settings.netwatch_loss_warn_pct
            ):
                reasons.append(f"loss {window_loss:.0f}% ({settings.netwatch_loss_window})")
            if target.rtt_avg_ms is not None and target.rtt_avg_ms >= settings.netwatch_rtt_warn_ms:
                reasons.append(f"rtt {target.rtt_avg_ms:.0f} ms")
            if reasons:
                _add(
                    Item(
                        key=key,
                        label=label,
                        state="yellow",
                        detail=" · ".join(reasons),
                        metrics=metrics,
                    ),
                    in_maintenance=in_maintenance,
                )
            else:
                detail = (
                    f"up · rtt {target.rtt_avg_ms:.0f} ms"
                    if target.rtt_avg_ms is not None
                    else "up"
                )
                _add(
                    Item(key=key, label=label, state="green", detail=detail, metrics=metrics),
                    in_maintenance=in_maintenance,
                )
    tile = Tile(
        signal="netwatch",
        state=_tile_state(rollup_items, age_s=0.0, cadence_s=60),
        items=items,
        cadence_s=60,
        data_age_s=0.0,
    )
    return tile, state


# --- ping: reference latency from the border router (US2; renamed from "internet") ------------

_REF_LABELS = {"8.8.8.8": "google", "1.1.1.1": "cloudflare"}


def ref_label(address: str) -> str:
    return _REF_LABELS.get(address, address)


def _eval_ping(
    probes: list[RefProbe],
    settings: DashSettings,
    *,
    now_us: int,
    prev: dict[str, Any],
) -> tuple[Tile, dict[str, Any]]:
    """Reference latency from the border router: red only when EVERY measurable ref has been
    at full loss for ``ping_down_after`` consecutive cycles (the debounce counter rides the
    carried state); one lost ref, threshold rtt, or threshold loss is yellow. An unmeasurable
    probe (router unreachable) is stale — never an inference that the internet is down."""

    measurable = [p for p in probes if p.error is None and p.loss_pct is not None]
    full_loss = [p for p in measurable if p.loss_pct is not None and p.loss_pct >= 100.0]
    prev_cycles = prev.get("full_loss_cycles")
    prev_cycles = prev_cycles if isinstance(prev_cycles, int) else 0
    if measurable and len(full_loss) == len(measurable):
        cycles = prev_cycles + 1
    elif measurable:
        cycles = 0
    else:
        cycles = prev_cycles  # no measurement this cycle — carry the debounce, judge nothing
    all_down = bool(measurable) and cycles >= settings.ping_down_after

    items: list[Item] = []
    for probe in probes:
        metrics: dict[str, float | int | None] = {
            "rtt_ms": probe.rtt_avg_ms,
            "loss_pct": probe.loss_pct,
        }
        if probe.error is not None or probe.loss_pct is None:
            items.append(
                Item(
                    key=probe.address,
                    label=probe.label,
                    state="stale",
                    detail=f"probe failed: {probe.error or 'no result'}",
                    metrics=metrics,
                )
            )
            continue
        if probe.loss_pct >= 100.0:
            if all_down:
                items.append(
                    Item(
                        key=probe.address,
                        label=probe.label,
                        state="red",
                        detail=f"no replies ({cycles} cycles)",
                        metrics=metrics,
                    )
                )
            else:
                items.append(
                    Item(
                        key=probe.address,
                        label=probe.label,
                        state="yellow",
                        detail="no replies",
                        metrics=metrics,
                    )
                )
            continue
        reasons: list[str] = []
        if probe.loss_pct >= settings.ping_loss_warn_pct:
            reasons.append(f"loss {probe.loss_pct:.0f}%")
        if probe.rtt_avg_ms is not None and probe.rtt_avg_ms >= settings.ping_rtt_warn_ms:
            reasons.append(
                f"rtt {probe.rtt_avg_ms:.0f} ms (warn ≥ {settings.ping_rtt_warn_ms:.0f})"
            )
        if reasons:
            items.append(
                Item(
                    key=probe.address,
                    label=probe.label,
                    state="yellow",
                    detail=" · ".join(reasons),
                    metrics=metrics,
                )
            )
        else:
            rtt = f"rtt {probe.rtt_avg_ms:.0f} ms" if probe.rtt_avg_ms is not None else "replies"
            items.append(
                Item(
                    key=probe.address,
                    label=probe.label,
                    state="green",
                    detail=f"{rtt} · loss {probe.loss_pct:.0f}%",
                    metrics=metrics,
                )
            )
    tile = Tile(
        signal="ping",
        state=_tile_state(items, age_s=0.0, cadence_s=60),
        items=items,
        cadence_s=60,
        data_age_s=0.0,
    )
    return tile, {"full_loss_cycles": cycles}


def _eval_pipeline(feeds: list[PipelineFeed], *, now_us: int) -> Tile:
    """ "Is obi itself seeing the network?" — collector freshness + the pre-judged extras.

    The distinction this tile exists for: every OTHER tile marks stale when its feed dies;
    this one names WHICH feed died, so "the network is down" and "obi is blind" read apart at
    a glance (US4). Judged from log-mtime ages against each collector's own cadence rule.
    """

    items: list[Item] = []
    for feed in feeds:
        metrics: dict[str, float | int | None] = {
            "age_s": feed.age_s,
            "stale_after_s": feed.stale_after_s,
        }
        if feed.state is not None:
            items.append(
                Item(
                    key=feed.name,
                    label=feed.name,
                    state=feed.state,
                    detail=feed.detail,
                    metrics=metrics,
                )
            )
        elif feed.age_s is None:
            items.append(
                Item(
                    key=feed.name,
                    label=feed.name,
                    state="yellow",
                    detail="never run — is the cron installed?",
                    metrics=metrics,
                )
            )
        elif feed.age_s > 3 * feed.stale_after_s:
            items.append(
                Item(
                    key=feed.name,
                    label=feed.name,
                    state="red",
                    detail=f"quiet {_fmt_duration(feed.age_s)}"
                    f" (expected ≤ {_fmt_duration(feed.stale_after_s)})",
                    metrics=metrics,
                )
            )
        elif feed.age_s > feed.stale_after_s:
            items.append(
                Item(
                    key=feed.name,
                    label=feed.name,
                    state="yellow",
                    detail=f"quiet {_fmt_duration(feed.age_s)}",
                    metrics=metrics,
                )
            )
        else:
            items.append(
                Item(
                    key=feed.name,
                    label=feed.name,
                    state="green",
                    detail=f"last run {_fmt_duration(feed.age_s)} ago",
                    metrics=metrics,
                )
            )
    return Tile(
        signal="pipeline",
        state=_tile_state(items, age_s=0.0, cadence_s=60),
        items=items,
        cadence_s=60,
        data_age_s=0.0,
    )


def collect_pipeline_feeds(
    config: Config,
    settings: DashSettings,
    *,
    now_us: int,
    maintenance: Maintenance | None = None,
) -> list[PipelineFeed]:
    """Gather the pipeline tile's inputs: collector log ages + the two pre-judged extras."""

    from obi.core import configstore, service
    from obi.core.config import default_config_repo_dir

    feeds: list[PipelineFeed] = []
    for health in service.collector_health(now=now_us / 1_000_000):
        feeds.append(
            PipelineFeed(
                name=health.name,
                age_s=health.age_s,
                stale_after_s=health.cadence_s * STALE_MULTIPLIER + STALE_GRACE_S,
            )
        )

    # Config-capture staleness (the 085 sidecar; window per [watch.config_captures] when set).
    captures_cfg = config.watch.config_captures
    repo_path = captures_cfg.repo_path if captures_cfg is not None else default_config_repo_dir()
    stale_after = duration_to_seconds(
        captures_cfg.stale_after if captures_cfg is not None else "1d",
        86400,
        context="[watch.config_captures] stale_after",
    )
    sidecar = configstore.read_capture_state(repo_path)
    stale_count = 0
    tracked = 0
    in_maintenance = 0
    for identity, entry in sidecar.items():
        if maintenance is not None and maintenance.device(identity) is not None:
            in_maintenance += 1  # 135 FR-010: torn-down devices cannot be captured — by design
            continue
        raw = entry.get("last_success_at")
        if not raw:
            continue  # never captured ⇒ configs status shows it; not a freshness transition
        try:
            captured = datetime.fromisoformat(str(raw))
        except ValueError:
            continue
        tracked += 1
        if now_us / 1_000_000 - captured.timestamp() > stale_after:
            stale_count += 1
    if tracked:
        feeds.append(
            PipelineFeed(
                name="configs-capture",
                age_s=None,
                stale_after_s=stale_after,
                state="yellow" if stale_count else "green",
                detail=f"{stale_count}/{tracked} capture-stale"
                + (f" ({in_maintenance} in maintenance)" if in_maintenance else "")
                + (" — check: obi configs status" if stale_count else ""),
            )
        )

    # The pager's own delivery health (US3 writes it into the pager state file).
    pager_state = load_json(settings.state_dir / PAGER_STATE_FILE)
    if pager_state is not None and pager_state.get("delivery_error"):
        feeds.append(
            PipelineFeed(
                name="pager",
                age_s=None,
                stale_after_s=60,
                state="yellow",
                detail=f"delivery failing: {pager_state['delivery_error']}",
            )
        )
    return feeds


# --- the pager: red transitions → channel-agnostic events, restart-safe ------------------------

#: The delivery seam. ``run_pager`` emits structured :class:`PagerEvent` decisions; every
#: channel owns its own rendering (the CLI adapts SlackNotifier.deliver_status via
#: :func:`format_slack_page`). Return ``(delivered, error)`` for the PRIMARY channel — it
#: drives the retry loop. Contract: specs/106-dash-link-incidents/contracts/pager-events.md.
DeliverFn = Callable[["PagerEvent"], "tuple[bool, str | None]"]


@dataclass(frozen=True)
class PagerMember:
    """One board item cited as evidence inside a pager event."""

    signal: str
    key: str  # the pager state key, "signal|item_key"
    label: str
    detail: str


@dataclass(frozen=True)
class PagerEvent:
    """One channel-agnostic pager decision (feature 106).

    ``event_id`` is deterministic and stable across delivery retries — secondary channels
    MUST dedup on it (a Slack outage re-emits the same decision every cycle). Recovery ids
    are their alert's id + ``:rec`` so channels can pair alert and all-clear.
    """

    event_id: str
    kind: str  # "red" | "recovery"
    scope: str  # "item" | "incident" | "mass"
    at_us: int
    members: tuple[PagerMember, ...]
    signal: str | None = None  # item + mass scopes; None for cross-signal incidents
    sites: tuple[str, str] | None = None  # incident scope, sorted alphabetically
    band: str | None = None  # incident scope; None for band-less groups
    total: int | None = None  # mass red: evaluated items in the signal
    down_s: float | None = None  # recoveries: first red → last clear


@dataclass(frozen=True)
class PagerOutcome:
    """One pager pass: events sent, deliveries failed, owed retries, held incident members."""

    sent: list[PagerEvent]
    failed: int
    pending: int
    held: int = 0
    retired: list[str] = field(default_factory=list)  # 135: incidents closed by maintenance


def _pager_key(signal: str, item_key: str) -> str:
    return f"{signal}|{item_key}"


def _coalesced(entries: list[dict[str, Any]], *, ratio: float, total: int) -> bool:
    """The mass-event rule: at least 2 items AND ≥ ratio of the signal's evaluated items."""

    return len(entries) >= 2 and total > 0 and len(entries) >= ratio * total


_PAGER_SOURCES = {"fleet": "host"}  # fleet items page by what they are; others by signal name


def _pager_label(signal: str, label: str) -> str:
    """Per-item page prefix: ``host: siteA-ptp-siteB-60`` / ``netwatch: siteB-gw:
    siteB-siteA-60``."""

    if signal == "netwatch":
        label = label.replace("→", ": ", 1)
    return f"{_PAGER_SOURCES.get(signal, signal)}: {label}"


# --- link grouping (feature 106): site-pair + band from the naming convention ------------------


@dataclass(frozen=True)
class LinkGroup:
    """One link identity: unordered site pair + optional band, e.g. key ``bear~noc~60``."""

    key: str
    site_a: str  # sorted; site_a < site_b
    site_b: str
    band: str | None


_BAND_RE = re.compile(r"\d{2,3}")


def pager_sites(identities: Iterable[str], extra: Iterable[str] = ()) -> frozenset[str]:
    """Known-site tokens: first hyphen segment of every identity carrying a ``gw`` token.

    ``siteb-gw`` → siteb, ``core-gw-1`` → core, ``sitea-internet-gw`` → sitea; APs/switches
    contribute nothing. ``extra`` supplies sites with no gateway (config ``extra_sites``).
    """

    sites = {s.strip().lower() for s in extra if s.strip()}
    for identity in identities:
        tokens = identity.strip().lower().split("-")
        if "gw" in tokens[1:]:
            sites.add(tokens[0])
    return frozenset(sites)


def link_group(label: str, sites: frozenset[str]) -> LinkGroup | None:
    """The link identity of a board label, or ``None`` when it has none.

    Exactly two distinct known-site tokens are required (``core-gw-1``, ping refs, uplinks
    and pipeline feeds all fail the gate and keep per-item behavior); the band is a trailing
    2–3 digit token (``60``/``05``) when present. Both netwatch directions and the fleet
    radio of one physical link tokenize to the same group key.
    """

    if not sites:
        return None
    tokens = [t for t in re.split(r"[^0-9a-z]+", label.lower()) if t]
    found = sorted({t for t in tokens if t in sites})
    if len(found) != 2:
        return None
    band = tokens[-1] if tokens and _BAND_RE.fullmatch(tokens[-1]) else None
    return LinkGroup(
        key=f"{found[0]}~{found[1]}~{band or '-'}",
        site_a=found[0],
        site_b=found[1],
        band=band,
    )


# --- Slack rendering (the reference channel) ---------------------------------------------------


def _member_line(member: PagerMember) -> str:
    line = _pager_label(member.signal, member.label)
    return f"{line} — {member.detail}" if member.detail else line


def format_slack_page(event: PagerEvent) -> str:
    """The canonical Slack rendering of a pager event (byte-pinned by unit tests).

    Item and mass renderings are the pre-106 formats verbatim; incidents name the link
    (sites alphabetical, band in parens when known).
    """

    if event.scope == "mass":
        assert event.signal is not None
        if event.kind == "recovery":
            return f"✅ dash: {event.signal} — {len(event.members)} recovered"
        labels = ", ".join(m.label for m in event.members[:3])
        more = "…" if len(event.members) > 3 else ""
        return f"🔴 dash: {event.signal} — {len(event.members)}/{event.total} red ({labels}{more})"
    if event.scope == "incident":
        assert event.sites is not None
        link = f"link {event.sites[0]}↔{event.sites[1]}"
        if event.band:
            link += f" ({event.band})"
        if event.kind == "recovery":
            return f"✅ dash: {link} recovered (down {_fmt_duration(event.down_s or 0.0)})"
        if len(event.members) == 1:
            return f"🔴 dash: {link}: {_member_line(event.members[0])}"
        lines = " · ".join(_member_line(m) for m in event.members[:4])
        more = f" +{len(event.members) - 4} more" if len(event.members) > 4 else ""
        return f"🔴 dash: {link} — {len(event.members)} red: {lines}{more}"
    member = event.members[0]
    if event.kind == "recovery":
        label = _pager_label(member.signal, member.label)
        return f"✅ dash: {label} recovered (down {_fmt_duration(event.down_s or 0.0)})"
    return f"🔴 dash: {_member_line(member)}"


def run_pager(
    board: Board,
    settings: DashSettings,
    *,
    now_us: int,
    deliver: DeliverFn,
    sites: frozenset[str] = frozenset(),
) -> PagerOutcome:
    """Diff the board's red set against the held baseline and page the transitions.

    The rules (095 US3 + 106, contracts/pager-events.md):
    - Only ``red`` pages; yellow and stale never do. Only signals in ``pager.signals`` count.
    - First ever run (no state file) seeds silently — every current red enters the baseline
      marked ``seeded`` and is never paged (no transition was observed).
    - One page per continuous red; a failed delivery leaves the item pending, retried next
      cycle (self-healing when Slack returns). Recovery events go only to items actually
      paged, and also retry until delivered.
    - Precedence: per-signal mass coalesce (≥ ``mass_ratio`` of the signal's evaluated
      items, min 2) → link incidents (all reds sharing a link identity page once; later
      members fold silently; one recovery when the whole group clears) → per-item.
    - ``announce_hold`` > 0 delays a NEW incident's first page; a group that fully clears
      within the hold never pages at all. Held ≠ pending (held members are not owed).
    - A signal whose tile is a data-gap placeholder carries its entries untouched (a gap is
      not a recovery — and it blocks an incident's recovery); an item that stops being
      EVALUATED ages out silently (renamed/removed devices never sit red forever).
    """

    pager = settings.pager
    hold_us = (
        duration_to_seconds(pager.announce_hold, 0, context="[dash.pager] announce_hold")
        * 1_000_000
    )
    grouping_on = pager.link_grouping and bool(sites)
    state_path = settings.state_dir / PAGER_STATE_FILE
    raw = load_json(state_path)
    first_run = raw is None
    entries: dict[str, dict[str, Any]] = dict(raw.get("items", {})) if raw else {}

    sent: list[PagerEvent] = []
    retired: list[str] = []  # 135: paged entries dropped because their item entered maintenance
    failed = 0
    delivery_error: str | None = None

    def _send(event: PagerEvent) -> bool:
        nonlocal failed, delivery_error
        ok, error = deliver(event)
        if ok:
            sent.append(event)
        else:
            failed += 1
            delivery_error = error or "delivery failed"
        return ok

    def _member(key: str) -> PagerMember:
        entry = entries[key]
        return PagerMember(
            signal=str(entry.get("signal", "")),
            key=key,
            label=str(entry.get("label", "")),
            detail=str(entry.get("detail") or ""),
        )

    def _since(key: str) -> int:
        return int(entries[key].get("red_since_us") or now_us)

    def _observed(key: str) -> int:
        entry = entries[key]
        return int(entry.get("observed_us") or entry.get("red_since_us") or now_us)

    # Pass 1 — per-signal entry maintenance (unchanged 095 mechanics; emission moved out).
    to_page: dict[str, list[str]] = {}  # signal → unpaged/unseeded red entry keys
    to_recover: dict[str, list[str]] = {}  # signal → cleared-but-paged entry keys
    totals: dict[str, int] = {}
    for tile in board.tiles:
        if tile.signal not in pager.signals:
            continue
        real_items = [item for item in tile.items if item.key != "no-data"]
        if not real_items:
            continue  # a data-gap placeholder: carry the signal's entries untouched
        # 135: a maintenance item is not evaluated for paging at all — never red, never in a
        # mass-ratio total, and an incident it was part of is RETIRED silently (it did not
        # recover; it was declared off). The gap check above runs first on purpose: a tile
        # whose every item is in maintenance is evaluated (and empty), not a data gap.
        in_maintenance = {
            _pager_key(tile.signal, item.key) for item in real_items if item.maintenance is not None
        }
        live_items = [item for item in real_items if item.maintenance is None]
        evaluated = {_pager_key(tile.signal, item.key) for item in live_items}
        reds = {
            _pager_key(tile.signal, item.key): item for item in live_items if item.state == "red"
        }
        totals[tile.signal] = len(live_items)

        # Age out entries no longer evaluated at all (renamed/removed items) — silently.
        for key in [k for k in entries if k.startswith(f"{tile.signal}|")]:
            if key not in evaluated:
                dropped = entries.pop(key)
                if key in in_maintenance and dropped.get("paged_at_us") is not None:
                    retired.append(key)

        # Transitions in, and the retry backlog.
        page_keys: list[str] = []
        for key, item in reds.items():
            entry = entries.get(key)
            if entry is None:
                entry = {
                    "signal": tile.signal,
                    "label": item.label,
                    "detail": item.detail,
                    "red_since_us": item.since_us or now_us,
                    "observed_us": now_us,
                    "paged_at_us": None,
                    "seeded": first_run,
                }
                entries[key] = entry
            else:
                entry["label"] = item.label
                entry["detail"] = item.detail
                entry.pop("cleared_at_us", None)  # re-red while recovery pended: one incident
            if not entry.get("seeded") and entry.get("paged_at_us") is None:
                page_keys.append(key)
        to_page[tile.signal] = page_keys

        # Clears out (paged entries only; seeded/unpaged clear silently).
        recover_keys: list[str] = []
        for key in [k for k in entries if k.startswith(f"{tile.signal}|")]:
            if key in reds:
                continue
            entry = entries[key]
            if entry.get("paged_at_us") is None:
                entries.pop(key)  # never paged (seeded, held, or cleared before delivery)
                continue
            entry.setdefault("cleared_at_us", now_us)
            recover_keys.append(key)
        to_recover[tile.signal] = recover_keys

    held_keys: set[str] = set()

    if not first_run:
        # Pass 2 — mass coalesce per signal (precedence 1; a site-wide event outranks links).
        # A mass attempt CONSUMES its keys either way: a failed delivery retries as the same
        # mass event next cycle, never as individual pages.
        for tile in board.tiles:
            signal = tile.signal
            if signal not in totals:
                continue
            total = totals[signal]
            page_keys = to_page[signal]
            if _coalesced([entries[k] for k in page_keys], ratio=pager.mass_ratio, total=total):
                event = PagerEvent(
                    event_id=f"mass:{signal}:{min(_since(k) for k in page_keys)}",
                    kind="red",
                    scope="mass",
                    at_us=now_us,
                    members=tuple(_member(k) for k in page_keys),
                    signal=signal,
                    total=total,
                )
                if _send(event):
                    for k in page_keys:
                        entries[k]["paged_at_us"] = now_us
                to_page[signal] = []
            recover_keys = to_recover[signal]
            if _coalesced([entries[k] for k in recover_keys], ratio=pager.mass_ratio, total=total):
                event = PagerEvent(
                    event_id=f"mass:{signal}:{min(_since(k) for k in recover_keys)}:rec",
                    kind="recovery",
                    scope="mass",
                    at_us=now_us,
                    members=tuple(_member(k) for k in recover_keys),
                    signal=signal,
                )
                if _send(event):
                    for k in recover_keys:
                        entries.pop(k, None)
                to_recover[signal] = []

        remaining_page = {k for keys in to_page.values() for k in keys}
        remaining_recover = {k for keys in to_recover.values() for k in keys}

        # Pass 3 — link incidents (precedence 2, cross-signal, sorted group key).
        if grouping_on:
            groups: dict[str, list[str]] = {}
            group_by_key: dict[str, LinkGroup] = {}
            for key, entry in entries.items():
                group = link_group(str(entry.get("label", "")), sites)
                if group is not None:
                    groups.setdefault(group.key, []).append(key)
                    group_by_key[key] = group
            for gkey in sorted(groups):
                member_keys = sorted(groups[gkey])
                group = group_by_key[member_keys[0]]
                paged = [k for k in member_keys if entries[k].get("paged_at_us") is not None]
                announceable = [k for k in member_keys if k in remaining_page]
                red_members = [k for k in member_keys if "cleared_at_us" not in entries[k]]
                if paged and announceable:
                    # Fold: the incident already paged (or was consumed by a mass event) —
                    # later symptoms join silently, marked for observability.
                    for k in announceable:
                        entries[k]["paged_at_us"] = now_us
                        entries[k]["folded"] = True
                        remaining_page.discard(k)
                elif announceable:
                    if now_us < min(_observed(k) for k in announceable) + hold_us:
                        held_keys.update(announceable)  # holding for the full picture
                        remaining_page.difference_update(announceable)
                    else:
                        opened_vals = [
                            int(entries[k]["incident_opened_us"])
                            for k in member_keys
                            if entries[k].get("incident_opened_us")
                        ]
                        opened = min(opened_vals) if opened_vals else now_us
                        for k in red_members:
                            entries[k]["incident_opened_us"] = opened  # pins the event id
                        event = PagerEvent(
                            event_id=f"link:{gkey}:{opened}",
                            kind="red",
                            scope="incident",
                            at_us=now_us,
                            members=tuple(_member(k) for k in red_members),
                            sites=(group.site_a, group.site_b),
                            band=group.band,
                        )
                        if _send(event):
                            for k in red_members:
                                entries[k]["paged_at_us"] = now_us
                        remaining_page.difference_update(announceable)
                # One recovery when EVERY member has cleared (partial clears wait silently;
                # a gap-carried member has no cleared_at_us and correctly blocks this).
                if paged and all("cleared_at_us" in entries[k] for k in member_keys):
                    opened_vals = [
                        int(entries[k]["incident_opened_us"])
                        for k in member_keys
                        if entries[k].get("incident_opened_us")
                    ]
                    opened = min(opened_vals) if opened_vals else min(map(_since, member_keys))
                    down_s = max(
                        0.0,
                        (
                            max(int(entries[k]["cleared_at_us"]) for k in member_keys)
                            - min(map(_since, member_keys))
                        )
                        / 1_000_000,
                    )
                    event = PagerEvent(
                        event_id=f"link:{gkey}:{opened}:rec",
                        kind="recovery",
                        scope="incident",
                        at_us=now_us,
                        members=tuple(_member(k) for k in member_keys),
                        sites=(group.site_a, group.site_b),
                        band=group.band,
                        down_s=down_s,
                    )
                    if _send(event):
                        for k in member_keys:
                            entries.pop(k, None)
                remaining_recover.difference_update(member_keys)

        # Pass 4 — ungrouped items, byte-for-byte the 095 per-item behavior.
        for key in sorted(remaining_page):
            entry = entries[key]
            event = PagerEvent(
                event_id=f"item:{key}:{_since(key)}",
                kind="red",
                scope="item",
                at_us=now_us,
                members=(_member(key),),
                signal=str(entry.get("signal", "")),
            )
            if _send(event):
                entry["paged_at_us"] = now_us
        for key in sorted(remaining_recover):
            entry = entries[key]
            down_s = max(0.0, (int(entry["cleared_at_us"]) - _since(key)) / 1_000_000)
            event = PagerEvent(
                event_id=f"item:{key}:{_since(key)}:rec",
                kind="recovery",
                scope="item",
                at_us=now_us,
                members=(_member(key),),
                signal=str(entry.get("signal", "")),
                down_s=down_s,
            )
            if _send(event):
                entries.pop(key, None)

    pending = sum(
        1
        for key, entry in entries.items()
        if not entry.get("seeded") and entry.get("paged_at_us") is None and key not in held_keys
    )
    write_json_atomic(
        state_path,
        {
            "items": entries,
            "delivery_error": delivery_error,
            "delivery_error_us": now_us if delivery_error else None,
        },
    )
    return PagerOutcome(
        sent=sent, failed=failed, pending=pending, held=len(held_keys), retired=retired
    )


# --- the cycle (obi dash run): collect → evaluate → persist ------------------------------------


@contextmanager
def cycle_lock(state_dir: Path) -> Iterator[bool]:
    """Non-blocking single-instance guard for the minute cron.

    Yields ``True`` with the lock held, or ``False`` when another cycle still holds it — a hung
    device read must never pile up one process per minute; the caller logs one line and exits 0
    (the skipped minute shows up as board age, honestly).
    """

    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / "run.lock"
    handle = lock_path.open("a")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _collect_fleet(
    config: Config, notes: list[str], *, now_us: int, probe: ProbeFn | None
) -> tuple[list[DeviceState] | None, set[str], bool]:
    """Run the 072 ICMP sweep in-process; return (states, UISP-flagged identities, from_cache).

    Reuses the exact ``obi icmp run`` path — same settings, same store, same debounce — so the
    072 watch pass and ``obi icmp status`` keep reading the store they always did, just fresher.
    The maintenance set is the controller's operator flag, carried by the same snapshot/cache
    the Wave roster rides.
    """

    from obi.core import icmp as icmp_mod
    from obi.core import icmpstore
    from obi.core.config import IcmpSettings
    from obi.core.inventory import load as load_inventory

    settings = config.icmp or IcmpSettings()
    siklu_hosts = list(config.siklu.hosts) if config.siklu is not None else []
    try:
        inventory = load_inventory(config.inventory_path)
    except OSError:
        inventory = None
    uisp_targets: list[icmp_mod.Target] = []
    maintenance: set[str] = set()
    ignore_names = list(config.uisp.ignore_names) if config.uisp is not None else []
    uisp_note: str | None = None
    if settings.include_uisp_ptp:
        uisp_targets, maintenance, uisp_note = icmp_mod.load_uisp_ptp_targets(
            settings, ignore_names=ignore_names
        )
        if uisp_note is not None:
            notes.append(uisp_note)
    else:
        # 135: the UISP maintenance flags are honored even when the radios are not probed.
        maintenance, _cached, uisp_note = icmp_mod.load_uisp_flagged(
            settings, ignore_names=ignore_names
        )
    from_cache = uisp_note is not None
    targets = icmp_mod.resolve_targets(
        inventory,
        siklu_hosts,
        ignore=settings.ignore_devices,
        include_siklu=settings.include_siklu,
        extra_hosts=settings.extra_hosts,
        uisp_targets=uisp_targets,
    )
    if not targets:
        notes.append("no ICMP targets — run: obi routeros discover")
        return None, maintenance, from_cache
    try:
        conn = icmpstore.connect(settings.db_path)
    except icmpstore.StoreError as exc:
        notes.append(f"icmp store unavailable: {exc}")
        return None, maintenance, from_cache
    try:
        icmp_mod.run_collect(settings, targets, conn=conn, now_us=now_us, probe=probe)
        return icmpstore.load_states(conn), maintenance, from_cache
    finally:
        conn.close()


def _collect_uplinks(
    config: Config, settings: DashSettings, notes: list[str]
) -> list[UplinkReading] | None:
    """Latest per-uplink rates from the 5-minute metrics store (read-only; total)."""

    from obi.core import metricstore
    from obi.core.config import default_metrics_db_path
    from obi.core.models import _utcnow

    if not settings.internet_links:
        return None
    db_path = (
        config.snmp.history.db_path
        if config.snmp is not None and config.snmp.history is not None
        else default_metrics_db_path()
    )
    if not db_path.exists():
        notes.append("no metrics store — check: obi metrics status")
        return None
    now_us = int(_utcnow().timestamp() * 1_000_000)
    readings: list[UplinkReading] = []
    try:
        conn = metricstore.connect_readonly(db_path)
    except metricstore.StoreError as exc:
        notes.append(f"metrics store unavailable: {exc}")
        return None
    try:
        for link in settings.internet_links:
            try:
                rate_in = metricstore.latest_rate(
                    conn, device=link.device, metric="ifHCInOctets", ifname=link.interface
                )
                rate_out = metricstore.latest_rate(
                    conn, device=link.device, metric="ifHCOutOctets", ifname=link.interface
                )
            except metricstore.StoreError as exc:
                readings.append(
                    UplinkReading(
                        link=link, in_bps=None, out_bps=None, sample_age_s=None, error=str(exc)
                    )
                )
                continue
            if rate_in is None or rate_out is None:
                readings.append(
                    UplinkReading(
                        link=link,
                        in_bps=None,
                        out_bps=None,
                        sample_age_s=None,
                        error="no rate sample",
                    )
                )
                continue
            newest_ts = max(rate_in[1], rate_out[1])
            readings.append(
                UplinkReading(
                    link=link,
                    in_bps=rate_in[0] * 8,  # bytes/s → bits/s
                    out_bps=rate_out[0] * 8,
                    sample_age_s=round(max(0.0, (now_us - newest_ts) / 1_000_000), 1),
                )
            )
    finally:
        conn.close()
    return readings


#: Netwatch tables are small (≤ tens of rows); the cap only bounds a pathological device.
_NETWATCH_MAX_RECORDS = 200


def _routeros_inventory_devices(config: Config) -> list[tuple[str, str]]:
    """(identity, address) for every identified, present RouterOS roster device."""

    from obi.core.inventory import load as load_inventory
    from obi.core.models import DiscoveryState

    try:
        inventory = load_inventory(config.inventory_path)
    except OSError:
        return []
    if inventory is None:
        return []
    return [
        (entry.identity or entry.address, entry.address)
        for entry in inventory.entries
        if entry.present_in_latest and entry.discovery_state is DiscoveryState.IDENTIFIED
    ]


def _collect_netwatch(
    config: Config, notes: list[str], *, driver: Any | None = None
) -> list[RouterNetwatch] | None:
    """One ``/tool/netwatch`` print per RouterOS device, fanned out (the 072-pool shape).

    Read-only through the driver's existing print rail (contracts/routeros-netwatch-read.md);
    a router whose read fails becomes a ``RouterNetwatch(error=…)`` — its pairs render stale,
    never red. Routers with no netwatch rows cost one cheap empty read.
    """

    from concurrent.futures import ThreadPoolExecutor
    from concurrent.futures import TimeoutError as FuturesTimeoutError

    from obi.core.audit import audit_scope, current_scope

    if config.routeros is None:
        notes.append("no [routeros] credentials — netwatch not polled")
        return None
    devices = _routeros_inventory_devices(config)
    if not devices:
        notes.append("no RouterOS inventory — netwatch not polled")
        return None
    if driver is None:
        from obi.core.drivers.mikrotik import MikrotikDriver

        driver = MikrotikDriver(config.routeros)

    # Contextvars are per-thread (audit.py's own rule): capture the cycle's ambient scope here
    # and re-open it inside each worker, so every netwatch read self-records like the pings do
    # (Constitution V — found missing on the first live cycle: 2 audited events, not 27).
    scope = current_scope()

    def _read(identity: str, address: str) -> RouterNetwatch:
        with audit_scope(*scope) if scope is not None else nullcontext():
            result = driver.read_path(
                address,
                ("tool", "netwatch"),
                timeout=config.timeout,
                max_records=_NETWATCH_MAX_RECORDS,
            )
        if result.error is not None:
            return RouterNetwatch(router=identity, error=result.error)
        return RouterNetwatch(
            router=identity, targets=[map_netwatch(record) for record in result.records]
        )

    out: list[RouterNetwatch] = []
    executor = ThreadPoolExecutor(max_workers=max(1, min(config.concurrency, len(devices))))
    try:
        futures = {
            executor.submit(_read, identity, address): identity for identity, address in devices
        }
        for future, identity in futures.items():
            try:
                out.append(future.result(timeout=config.timeout + 1.0))
            except FuturesTimeoutError:
                out.append(
                    RouterNetwatch(router=identity, error="read exceeded the per-device budget")
                )
    finally:
        # The 072-pool rule: never block on a hung future — a stuck device must not eat the
        # minute. Its thread dies with the process at cycle end.
        executor.shutdown(wait=False, cancel_futures=True)
    return out


def _collect_ping(
    config: Config, settings: DashSettings, notes: list[str], *, driver: Any | None = None
) -> list[RefProbe] | None:
    """The reference pings FROM the border router via the read-only 028 seam."""

    if config.routeros is None:
        notes.append("no [routeros] credentials — reference pings not probed")
        return None
    wanted = settings.ping_router.strip().lower()
    if not wanted:  # 137: no built-in reference router
        notes.append("no [dash] ping_router configured — reference pings not probed")
        return None
    address = None
    for identity, entry_address in _routeros_inventory_devices(config):
        if identity.lower() == wanted or entry_address.lower() == wanted:
            address = entry_address
            break
    if address is None:
        notes.append(f"ping router {settings.ping_router!r} not in inventory")
        return None
    if driver is None:
        from obi.core.drivers.mikrotik import MikrotikDriver

        driver = MikrotikDriver(config.routeros)

    live = config.routeros_live
    timeout = live.timeout_s if live.timeout_s is not None else config.timeout
    probes: list[RefProbe] = []
    for ref in settings.ping_refs:
        result = driver.ping(address, ref, count=live.ping_count, timeout=timeout)
        if result.error is not None:
            probes.append(
                RefProbe(
                    address=ref,
                    label=ref_label(ref),
                    rtt_avg_ms=None,
                    loss_pct=None,
                    error=result.error,
                )
            )
        else:
            # Full loss is DATA (the seam's totality contract), never an error.
            probes.append(
                RefProbe(
                    address=ref,
                    label=ref_label(ref),
                    rtt_avg_ms=result.rtt_avg_ms,
                    loss_pct=result.loss_pct,
                )
            )
    return probes


#: Where the throttled serial read is cached between dash cycles (feature 121).
MESH_STATUS_FILE = "mesh-status.json"


def _tracked_nodes(snapshot_nodes: Any, settings: Any, *, now_s: float) -> list[TrackedNode]:
    """Allowlisted nodes from one node-table read, ages relative to ``now_s``."""

    allowed = {entry.lower() for entry in settings.report_only}
    nodes: list[TrackedNode] = []
    seen: set[str] = set()
    for node in snapshot_nodes:
        if allowed and not (
            allowed & {node.node_id.lower(), node.short_name.lower(), node.long_name.lower()}
        ):
            continue
        seen.add(node.node_id.lower())
        nodes.append(
            TrackedNode(
                node_id=node.node_id,
                label=settings.display_names.get(node.node_id) or node.long_name or node.node_id,
                last_heard_age_s=(max(0.0, now_s - node.last_heard) if node.last_heard else None),
                snr_db=node.snr,
                hops_away=node.hops_away,
            )
        )
    # An allowlisted node the radio cannot see at all is the eviction case 117 exists for — it
    # must still appear on the tile, or the board would go green by forgetting the very node
    # the tile is there to watch.
    for entry in settings.report_only:
        if entry.startswith("!") and entry.lower() not in seen:
            nodes.append(
                TrackedNode(
                    node_id=entry,
                    label=settings.display_names.get(entry) or entry,
                    last_heard_age_s=None,
                )
            )
    return nodes


def _load_mesh_serial_cache(state_dir: Path | None) -> dict[str, Any] | None:
    return load_json(state_dir / MESH_STATUS_FILE) if state_dir is not None else None


def _store_mesh_serial_cache(
    state_dir: Path | None,
    *,
    now_s: float,
    reachable: bool,
    detail: str,
    nodes: list[TrackedNode],
) -> None:
    """Persist the serial read. Stores ABSOLUTE last-heard instants, not ages.

    Caching an age would freeze it: a cached "heard 2 minutes ago" would still read as 2
    minutes four cycles later, and the tile could never go yellow between reads.
    """

    if state_dir is None:
        return
    payload = {
        "read_at_s": now_s,
        "reachable": reachable,
        "detail": detail,
        "nodes": [
            {
                "node_id": node.node_id,
                "label": node.label,
                "last_heard_s": (
                    None if node.last_heard_age_s is None else now_s - node.last_heard_age_s
                ),
                "snr_db": node.snr_db,
                "hops_away": node.hops_away,
            }
            for node in nodes
        ],
    }
    with suppress(OSError):
        write_json_atomic(state_dir / MESH_STATUS_FILE, payload)


def _nodes_from_cache(raw_nodes: list[Any], *, now_s: float) -> list[TrackedNode]:
    """Rebuild tracked nodes from the cache, re-ageing against the current instant."""

    nodes: list[TrackedNode] = []
    for raw in raw_nodes:
        if not isinstance(raw, dict):
            continue
        last_heard_s = raw.get("last_heard_s")
        nodes.append(
            TrackedNode(
                node_id=str(raw.get("node_id", "")),
                label=str(raw.get("label", "")),
                last_heard_age_s=(
                    max(0.0, now_s - float(last_heard_s))
                    if isinstance(last_heard_s, (int, float))
                    else None
                ),
                snr_db=raw.get("snr_db"),
                hops_away=raw.get("hops_away"),
            )
        )
    return nodes


def collect_mesh(
    config: Config, notes: list[str], *, now_s: float, driver: Any | None = None
) -> MeshHealth | None:
    """Passively read the mesh radio and the 117 position store. NEVER transmits.

    The only driver calls are ``resolve_port`` and ``snapshot`` — the same pair
    :meth:`MeshLocator.record_pass` restricts itself to. ``request_position`` is not reachable
    from here, and a test drives this with a request path that raises to keep it that way
    (contract T1/T2): a status tile must never cost shared LoRa airtime, and the dash cycle
    runs every minute.

    ``driver`` is the injectable seam (the ``run_cycle``/``MeshLocator`` shape). It is not
    optional politeness: without it a test on the operator's own laptop opens the real radio,
    which is how this parameter came to exist.

    ``None`` ⇒ mesh is not configured on this machine ⇒ no tile at all.
    """

    if config.meshtastic is None:
        return None

    from obi.core import meshstore
    from obi.core.drivers.meshtastic import MeshFailure
    from obi.core.mesh import mesh_access

    settings = config.meshtastic
    dash_settings = config.dash
    state_dir = dash_settings.state_dir if dash_settings is not None else None

    # 123: the read goes to the mesh DAEMON, not the serial port, so it is cheap enough to
    # run every cycle — the old [dash.mesh] read_interval throttle (which existed to avoid
    # holding the exclusive port each minute) no longer gates anything. The cache file
    # remains as the degraded-mode source: when the daemon is down, the tile still shows
    # the last-known nodes with honestly-aged last-heard instants, under a red radio item
    # that names the DAEMON as the outage (FR-012 — "radio silent" ≠ "service down").
    driver = driver if driver is not None else mesh_access(settings)
    standby = False
    failure: MeshFailure | None = None
    snapshot_nodes: Any = ()
    port = driver.resolve_port()
    if isinstance(port, MeshFailure):
        failure = port
    else:
        result = driver.snapshot(port)
        if isinstance(result, MeshFailure):
            failure = result
        else:
            snapshot_nodes = result.nodes
    if failure is not None:
        cached = _load_mesh_serial_cache(state_dir)
        raw_nodes = cached.get("nodes") if isinstance(cached, dict) else None
        reachable, detail = False, failure.detail
        nodes = _nodes_from_cache(raw_nodes if isinstance(raw_nodes, list) else [], now_s=now_s)
    else:
        last_status = getattr(driver, "last_status", None)
        standby = isinstance(last_status, dict) and last_status.get("state") == "standby"
        if standby and isinstance(last_status, dict):
            detail = str(last_status.get("detail") or "radio on loan (standby)")
        else:
            detail = f"attached on {port}"
        reachable = True
        nodes = _tracked_nodes(snapshot_nodes, settings, now_s=now_s)
        _store_mesh_serial_cache(
            state_dir,
            now_s=now_s,
            reachable=reachable,
            detail=detail,
            nodes=nodes,
        )

    newest_age: float | None = None
    dated_by_observation = False
    try:
        conn = meshstore.connect_readonly(settings.positions_db_path)
        try:
            for entry in settings.report_only:
                fix = meshstore.last_known(conn, entry)
                if fix is None:
                    continue
                age = max(0.0, now_s - fix.effective_fix_s)
                if newest_age is None or age < newest_age:
                    newest_age = age
                    dated_by_observation = not fix.fix_time_s
        finally:
            with suppress(Exception):
                conn.close()
    except meshstore.StoreError as exc:
        # Best-effort, exactly as record-on-sight is: a store problem must not cost the tile.
        notes.append(f"mesh position history unavailable: {exc}")

    return MeshHealth(
        reachable=reachable,
        detail=detail,
        nodes=nodes,
        newest_fix_age_s=newest_age,
        newest_fix_dated_by_observation=dated_by_observation,
        standby=standby,
    )


def collect_lighting(config: Config, notes: list[str]) -> LightingHealth | None:
    """Read each allowlisted WLED controller's state. Read-only — never sets a light.

    ``None`` ⇒ no ``[wled]`` section ⇒ no tile.
    """

    if config.wled is None or not config.wled.devices:
        return None

    from obi.core.drivers.wled import WledDriver, WledState

    settings = config.wled
    if config.dash is not None and config.dash.lighting.timeout is not None:
        settings = replace(settings, timeout=config.dash.lighting.timeout)
    driver = WledDriver(settings)

    controllers: list[LightingController] = []
    for name, base_url in sorted(config.wled.devices.items()):
        try:
            result = driver.state(base_url)
        except Exception as exc:  # noqa: BLE001 - a tile must never fail the cycle
            notes.append(f"lighting read failed for {name}: {exc}")
            controllers.append(
                LightingController(name=name, reachable=False, detail=str(exc) or "read failed")
            )
            continue
        if not result.ok or not isinstance(result.value, WledState):
            controllers.append(
                LightingController(name=name, reachable=False, detail=result.error or "unreachable")
            )
            continue
        controllers.append(
            LightingController(
                name=name,
                reachable=True,
                on=result.value.on,
                brightness=result.value.brightness,
                raw=result.value.raw,
            )
        )
    return LightingHealth(controllers=controllers)


def reconcile_status_band(
    config: Config,
    board: Board,
    lighting: LightingHealth | None,
    *,
    engine: Any | None = None,
) -> None:
    """Drive the declared status bands toward the board's desired color (128, 2.3.0).

    Edge-triggered and drift-healing: for each device with a declared layout, compare THIS
    cycle's lighting read against :func:`desired_status` and write ONLY on mismatch — a
    steady state issues zero writes; an external stomp is healed within a cycle. No read
    this cycle (unreachable device, absent tile) or missing status segments ⇒ no write,
    never a guess. ``engine`` is the injectable lighting engine (tests). Every write and
    refusal is audited by the engine itself (action ``wled_status_set``, actor ``dash``).
    The CALLER contains this whole step — a lighting failure never costs the cycle.
    """

    settings = config.dash
    if settings is None or not settings.status_band.enabled:
        return
    if config.wled is None or not config.wled.segments:
        return  # unreachable behind the load-time cross-section rule; defensive
    desired = desired_status(board)
    if desired is None:
        return

    raw_by_name = {
        controller.name: controller.raw
        for controller in (lighting.controllers if lighting is not None else [])
    }
    log = logging.getLogger("obi")
    for name, layout in sorted(config.wled.segments.items()):
        raw = raw_by_name.get(name)
        if raw is None:
            continue  # not read this cycle (unreachable / no tile) — leave the bands alone
        seg_ids = {
            seg.get("id")
            for seg in raw.get("seg", [])
            if isinstance(seg, dict) and isinstance(seg.get("id"), int)
        }
        if not any(sid in seg_ids for sid in layout.status):
            log.warning(
                "status bands missing on %s (declared ids %s absent from device state) — "
                "not writing",
                name,
                list(layout.status),
            )
            continue
        from obi.core.lighting import status_band_matches

        if status_band_matches(raw, layout.status, desired):
            continue  # steady state — zero writes (SC-003)
        if engine is None:
            from obi.core.audit import AuditTrail
            from obi.core.lighting import Lighting

            wled_settings = config.wled
            if settings.status_band.timeout is not None:
                wled_settings = replace(wled_settings, timeout=settings.status_band.timeout)
            engine = Lighting(wled_settings, AuditTrail(config.audit_path))
        outcome = engine.set_status_band(
            name,
            look=desired,
            reason=f"dash reconcile: board asks {desired}",
            actor="dash",
        )
        log.info(
            "status band reconcile", extra={"device": name, "desired": desired, "outcome": outcome}
        )


def run_cycle(
    config: Config,
    *,
    now_us: int,
    probe: ProbeFn | None = None,
    driver: Any | None = None,
    pager_deliver: DeliverFn | None = None,
    lighting_engine: Any | None = None,
) -> Board:
    """One dashboard cycle: collect, evaluate, persist ``board.json``; return the board.

    Degrades honestly: every input that cannot be gathered becomes a ``None`` (its tile renders
    stale-with-hint) plus a note — a cycle ALWAYS writes a board (FR-016). ``probe`` is the
    injectable ICMP prober and ``driver`` the injectable RouterOS driver (tests); the real
    driver is built once and shared by the netwatch fan-out and the reference pings. The
    pipeline tile lands with US4 and renders the frame's placeholder until then.
    """

    settings = config.dash
    assert settings is not None, "run_cycle requires a [dash] section (caller guards)"

    if driver is None and config.routeros is not None:
        from obi.core.drivers.mikrotik import MikrotikDriver

        driver = MikrotikDriver(config.routeros)

    notes: list[str] = []
    fleet, uisp_flagged, uisp_cached = _collect_fleet(config, notes, now_us=now_us, probe=probe)
    # 135: ONE effective maintenance set for every path below (config ∪ UISP flags).
    maintenance = Maintenance.build(config.maintenance, uisp_flagged, uisp_cached=uisp_cached)
    netwatch = _collect_netwatch(config, notes, driver=driver)
    ping = _collect_ping(config, settings, notes, driver=driver)
    internet = _collect_uplinks(config, settings, notes)
    pipeline: list[PipelineFeed] | None
    try:
        pipeline = collect_pipeline_feeds(config, settings, now_us=now_us, maintenance=maintenance)
    except OSError as exc:
        notes.append(f"pipeline health unavailable: {exc}")
        pipeline = None
    # 121 display-only signals. Contained: a failure in either degrades its own tile (or
    # omits it) and lands a note — it never costs the cycle (contract B2).
    mesh: MeshHealth | None
    try:
        mesh = collect_mesh(config, notes, now_s=now_us / 1_000_000)
    except Exception as exc:  # noqa: BLE001
        notes.append(f"mesh status unavailable: {exc}")
        mesh = MeshHealth(reachable=False, detail=str(exc) or "read failed")
    lighting: LightingHealth | None
    try:
        lighting = collect_lighting(config, notes)
    except Exception as exc:  # noqa: BLE001
        notes.append(f"lighting status unavailable: {exc}")
        lighting = None
    carried = load_carried_state(settings.state_dir)

    inputs = DashInputs(
        fleet=fleet,
        fleet_maintenance=uisp_flagged,
        maintenance=maintenance,
        netwatch=netwatch,
        ping=ping,
        internet=internet,
        pipeline=pipeline,
        mesh=mesh,
        lighting=lighting,
        carried=carried,
        notes=notes,
    )
    result = evaluate(inputs, settings, now_us=now_us)
    result = _with_maintenance_summary(config, result, maintenance, carried)
    write_carried_state(settings.state_dir, result.state)
    write_board(settings.state_dir, result.board)

    # 128: the status-band reconcile — after the board is safely on disk, fully contained:
    # a lighting failure of any kind never affects the board, pager, or alerting (FR-008).
    try:
        reconcile_status_band(config, result.board, lighting, engine=lighting_engine)
    except Exception as exc:  # noqa: BLE001 - fail-quiet by contract (2.3.0)
        logging.getLogger("obi").warning("status band reconcile failed: %s", exc)

    # The pager pass (US3): opt-in, deterministic, after the board is safely on disk — a
    # delivery problem can never cost the board write. The CLI passes the Slack adapter.
    if settings.pager.enabled and pager_deliver is not None:
        sites = pager_sites(
            (identity for identity, _addr in _routeros_inventory_devices(config)),
            settings.pager.extra_sites,
        )
        outcome = run_pager(
            result.board, settings, now_us=now_us, deliver=pager_deliver, sites=sites
        )
        logging.getLogger("obi").info(
            "dash pager pass",
            extra={
                "sent": len(outcome.sent),
                "failed": outcome.failed,
                "pending": outcome.pending,
                "held": outcome.held,
                "retired": len(outcome.retired),
            },
        )
        for key in outcome.retired:
            record_event(
                "action",
                summary="dash pager: incident retired (entered maintenance)",
                target=key,
                payload={"key": key, "reason": "entered maintenance"},
            )
    return result.board


def _uisp_site_names(config: Config) -> list[str]:
    """UISP site names from the geometry export (``links.json``) — for typo detection only."""

    payload = load_json(config.web.export_path)
    if payload is None:
        return []
    return [
        str(site["name"])
        for site in payload.get("sites", [])
        if isinstance(site, dict) and site.get("name")
    ]


def _with_maintenance_summary(
    config: Config, result: EvalResult, maintenance: Maintenance, carried: dict[str, Any]
) -> EvalResult:
    """Attach the board's maintenance summary and audit set changes (135 FR-011, FR-023).

    The summary names everything in maintenance plus any config entry that matched nothing
    this cycle (a typo must be visible, never silently inert). The effective set's
    fingerprint rides the carried state; a change is recorded once.
    """

    board = result.board
    state = dict(result.state)
    previous = carried.get("maintenance")
    entries = maintenance.entries()
    fingerprint = maintenance.fingerprint()
    if not isinstance(previous, dict):
        previous = None
    if (previous is None and entries) or (
        previous is not None and previous.get("fingerprint") != fingerprint
    ):
        old = {str(e) for e in previous.get("entries", [])} if previous is not None else set()
        record_event(
            "action",
            summary="maintenance set changed",
            payload={
                "added": sorted(set(entries) - old),
                "removed": sorted(old - set(entries)),
                "reason": maintenance.settings.reason,
            },
        )
    state["maintenance"] = {"fingerprint": fingerprint, "entries": entries}

    if maintenance.is_empty:
        return replace(result, state=state)
    seen: list[str] = []
    count = 0
    for tile in board.tiles:
        for item in tile.items:
            seen.append(item.key)
            seen.extend(part for part in item.label.split("→") if part)
            if item.maintenance is not None:
                count += 1
    summary = maintenance.summary(
        items=count, unmatched=maintenance.unmatched(seen, _uisp_site_names(config))
    )
    return replace(result, board=replace(board, maintenance=summary), state=state)
