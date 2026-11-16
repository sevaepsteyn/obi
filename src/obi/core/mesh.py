"""The mesh-locate engine (feature 111): resolve → freshness → limiter → translate → render.

One class, :class:`MeshLocator`, owns every decision between "where is Tumble?" and the
answer string — the toolset handler and the ``obi mesh`` CLI are thin callers, so every
numeric and wording decision is core-tested once (Principle III, the ``Lighting`` shape).

The policy (125, operator directive 2026-09-02): resolve the name against the daemon's
node table; the radio's own node answers from its own info; everything else answers
PASSIVELY from the recorded data with honest age — a lookup NEVER transmits or waits on
the air. Freshness is bought on a schedule by the mesh daemon's automatic pass
(``meshdaemon._auto_refresh``: one rate-limited request per tracked node per
``auto_request_s``, same durable ``ratelimit.json`` caps, audited as ``meshd:auto``).
The :class:`_RateLimiter` lives here but is consumed by the daemon — the one budget
every transmission spends from.

Since feature 123 the engine does not touch the serial port: its default access is the
mesh DAEMON's client (:func:`mesh_access`), and only the daemon process
(:mod:`obi.core.meshdaemon`) opens the radio — contract O1 in ``specs/123-mesh-daemon``.

Feature 126 slims every position-bearing answer to one sentence (no heard/battery
detail, no age words while current) and appends ", near <art name>" when the position
sits within ``art_near_ft`` of a piece in the operator-downloaded art dataset
(:mod:`obi.core.artmarks`).
"""

from __future__ import annotations

import contextlib
import difflib
import json
import logging
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from obi.core import artmarks, meshstore
from obi.core.audit import AuditTrail
from obi.core.brcaddr import (
    BrcAddress,
    ModelLoader,
    no_model,
    to_address,
)
from obi.core.config import (
    MeshtasticSettings,
)
from obi.core.drivers.meshtastic import (
    MeshAccess,
    MeshFailure,
    MeshNode,
    MeshOutcome,
    MeshPosition,
)

#: The tool's whole answer must fit every surface, radio included (contract: ≤600 chars,
#: line 1 a complete spoken-ready sentence).
MAX_ANSWER_CHARS = 600

_SUGGESTION_CAP = 3
_CANDIDATE_CAP = 6

#: SNR at/above which a direct-heard node with no position is called "likely nearby".
_STRONG_SNR_DB = 5.0

#: Fuzzy-resolution constants — DELIBERATELY the radio callsign matcher's
#: (radio/trigger.py: _MATCH_THRESHOLD/_AMBIGUITY_MARGIN): one mishearing policy
#: everywhere a transcribed name meets a roster.
_FUZZY_THRESHOLD = 0.75
_FUZZY_MARGIN = 0.05


def _squeeze(text: str) -> str:
    """Lowercase alphanumerics only (the trigger.py squeeze): "Art Car" ≡ "artcar"."""

    return re.sub(r"[^a-z0-9]", "", text.lower())


logger = logging.getLogger("obi")

_RATELIMIT_FILE = "ratelimit.json"


class _RateLimiter:
    """The durable transmission caps (FR-005): per-node spacing + a rolling-hour global cap.

    State is re-read on every check and rewritten atomically on every send, so concurrent
    obi processes (chat daemon, CLI, MCP) see each other's transmissions. An unreadable
    state file is treated as empty — the caps then rebuild from the next send.
    """

    def __init__(
        self, settings: MeshtasticSettings, state_dir: Path, now: Callable[[], float]
    ) -> None:
        self._settings = settings
        self._path = state_dir / _RATELIMIT_FILE
        self._now = now

    def _load(self) -> tuple[dict[str, float], list[float]]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}, []
        per_node_raw = raw.get("per_node") if isinstance(raw, dict) else None
        window_raw = raw.get("hour_window") if isinstance(raw, dict) else None
        per_node: dict[str, float] = {}
        if isinstance(per_node_raw, dict):
            for node_id, stamp in per_node_raw.items():
                if isinstance(stamp, (int, float)):
                    per_node[str(node_id)] = float(stamp)
        window = [
            float(stamp)
            for stamp in (window_raw if isinstance(window_raw, list) else [])
            if isinstance(stamp, (int, float))
        ]
        return per_node, window

    def denial(self, node_id: str) -> str | None:
        """Why a request to ``node_id`` may NOT transmit right now; ``None`` = allowed."""

        now = self._now()
        per_node, window = self._load()
        last = per_node.get(node_id)
        if last is not None and now - last < self._settings.node_rate_limit_s:
            return f"this node was asked {max(0, int(now - last))}s ago"
        recent = [stamp for stamp in window if now - stamp < 3600.0]
        if len(recent) >= self._settings.hourly_tx_cap:
            return f"the hourly mesh-transmission cap ({self._settings.hourly_tx_cap}) is reached"
        return None

    def record_send(self, node_id: str) -> None:
        now = self._now()
        per_node, window = self._load()
        per_node[node_id] = now
        window = [stamp for stamp in window if now - stamp < 3600.0]
        window.append(now)
        payload = json.dumps({"per_node": per_node, "hour_window": window})
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            scratch = self._path.with_suffix(".json.tmp")
            scratch.write_text(payload, encoding="utf-8")
            os.replace(scratch, self._path)
        except OSError:
            # A limiter that cannot persist must not take the answer down with it; the
            # in-flight request was already allowed and the next check re-reads disk.
            pass


def mesh_access(settings: MeshtasticSettings) -> MeshAccess:
    """The default way ANY consumer reaches the radio (123): the daemon's RPC client.

    The one place that decision lives — :class:`MeshLocator` and the dash collector both
    default through here, so every surface switched to the daemon together and no code
    path outside the daemon opens the serial port (contract O1). Tests keep injecting
    drivers/fakes explicitly.
    """

    from obi.core.meshclient import MeshDaemonClient

    return MeshDaemonClient(settings)


def record_fixes(
    settings: MeshtasticSettings,
    nodes: tuple[MeshNode, ...],
    *,
    model_loader: ModelLoader,
    now: Callable[[], float],
) -> tuple[int, int, str | None]:
    """Record allowlisted nodes' fixes: (new rows, allowlisted fixes seen, store error).

    Factored out of the locator (123) so the daemon's live recorder, its backstop sweep,
    and the engine's record-on-sight all share one allowlist/dedupe/address path.
    Best-effort; never raises.
    """

    allowed = {_squeeze(entry) for entry in settings.report_only}
    if not allowed:
        return 0, 0, None
    sightings = [
        (node, node.position)
        for node in nodes
        if node.position is not None
        and allowed & {_squeeze(node.node_id), _squeeze(node.short_name), _squeeze(node.long_name)}
    ]
    if not sightings:
        return 0, 0, None
    recorded, error = _record_position_rows(settings, sightings, model_loader=model_loader, now=now)
    return recorded, len(sightings), error


def _record_position_rows(
    settings: MeshtasticSettings,
    sightings: list[tuple[MeshNode, MeshPosition]],
    *,
    model_loader: ModelLoader,
    now: Callable[[], float],
) -> tuple[int, str | None]:
    """Write fixes for already-vetted sightings; a StoreError is logged, never raised."""

    allowed = {_squeeze(entry) for entry in settings.report_only}
    if not allowed:
        return 0, None  # nothing recordable — don't even create the store file
    model = model_loader()
    observed_at = int(now())
    recorded = 0
    try:
        conn = meshstore.connect(settings.positions_db_path)
    except meshstore.StoreError as exc:
        logger.warning("mesh record: position store unavailable: %s", exc)
        return 0, str(exc)
    try:
        for node, position in sightings:
            if not (
                allowed
                & {
                    _squeeze(node.node_id),
                    _squeeze(node.short_name),
                    _squeeze(node.long_name),
                }
            ):
                continue
            address = to_address(position.latitude, position.longitude, model)
            fix = meshstore.RecordedFix(
                node_id=node.node_id,
                latitude=position.latitude,
                longitude=position.longitude,
                altitude_m=position.altitude_m,
                fix_time_s=position.fix_time or 0,
                observed_at_s=observed_at,
                hops_away=node.hops_away,
                snr_db=node.snr,
                address=address.text,
            )
            if meshstore.record_fix(conn, fix):
                recorded += 1
    except meshstore.StoreError as exc:
        logger.warning("mesh record: write failed: %s", exc)
        return recorded, str(exc)
    finally:
        with contextlib.suppress(Exception):
            conn.close()
    return recorded, None


@dataclass(frozen=True)
class RecordOutcome:
    """One `obi mesh record` sweep, summarized (feature 117).

    ``skipped`` carries the quiet-skip reason (radio absent/busy — nothing was read);
    ``error`` means the STORE failed (the sweep saw fixes it could not keep) — the one
    outcome the cron log must make loud."""

    recorded: int
    tracked: int
    skipped: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class Resolved:
    node: MeshNode


@dataclass(frozen=True)
class Ambiguous:
    candidates: tuple[str, ...]


@dataclass(frozen=True)
class Unknown:
    suggestions: tuple[str, ...]


def _display(node: MeshNode, display_names: dict[str, str] | None = None) -> str:
    """The name obi says. An operator ``[meshtastic.names]`` entry wins outright — the
    field lesson (2026-08-29): mesh names are device labels ("Tumble 9831 (TMBL)"), and
    on the air the answer should just say "Tumble"."""

    if display_names:
        named = display_names.get(node.node_id)
        if named:
            return named
    long_name, short = node.long_name.strip(), node.short_name.strip()
    if long_name and short and long_name.lower() != short.lower():
        return f"{long_name} ({short})"
    return long_name or short or node.node_id


def _resolve(
    needle: str,
    nodes: tuple[MeshNode, ...],
    display_names: dict[str, str] | None = None,
) -> Resolved | Ambiguous | Unknown:
    """Exact (id/short/long) → unique substring → suggestions; never a silent guess."""

    lowered = needle.strip().lower()
    exact = [
        node
        for node in nodes
        if lowered in (node.node_id.lower(), node.short_name.lower(), node.long_name.lower())
    ]
    if len(exact) == 1:
        return Resolved(exact[0])
    if len(exact) > 1:
        return Ambiguous(tuple(_display(node, display_names) for node in exact[:_CANDIDATE_CAP]))
    hits = [
        node
        for node in nodes
        if lowered in node.node_id.lower()
        or lowered in node.short_name.lower()
        or lowered in node.long_name.lower()
    ]
    if len(hits) == 1:
        return Resolved(hits[0])
    if hits:
        return Ambiguous(tuple(_display(node, display_names) for node in hits[:_CANDIDATE_CAP]))
    names = {node.short_name: node for node in nodes if node.short_name}
    names.update({node.long_name: node for node in nodes if node.long_name})
    by_lower = {name.lower(): name for name in names}
    close = difflib.get_close_matches(lowered, list(by_lower), n=_SUGGESTION_CAP, cutoff=0.5)
    suggestions: list[str] = []
    for lower_name in close:
        display = _display(names[by_lower[lower_name]], display_names)
        if display not in suggestions:
            suggestions.append(display)
    return Unknown(tuple(suggestions))


def _age_words(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 90:
        return "under a minute"
    if seconds < 5400:
        minutes = int(seconds // 60)
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    if seconds < 172_800:
        hours = int(seconds // 3600)
        return f"{hours} hour{'s' if hours != 1 else ''}"
    days = int(seconds // 86_400)
    return f"{days} day{'s' if days != 1 else ''}"


def _ago(seconds: float) -> str:
    return "just now" if seconds < 90 else f"{_age_words(seconds)} ago"


def config_node_id(settings: MeshtasticSettings, node_ref: str) -> str | None:
    """Resolve a caller's name to an ALLOWLISTED node id from config alone (feature 117).

    The radio's node table is gone exactly when this matters (eviction, radio
    unreachable), so resolution runs over the operator's own naming: aliases, the
    ``report_only`` entries themselves, and display names. Only allowlisted ids
    resolve — a non-allowlisted name never reaches the history store (FR-008).
    """

    squeezed = _squeeze(node_ref)
    if not squeezed:
        return None
    candidate = node_ref
    for alias, target in settings.aliases.items():
        if squeezed == _squeeze(alias):
            candidate = target
            break
    allowed = {_squeeze(entry): entry for entry in settings.report_only}
    candidate_squeezed = _squeeze(candidate)
    entry = allowed.get(candidate_squeezed)
    if entry is not None and entry.startswith("!"):
        return entry
    for node_id, name in settings.display_names.items():
        if candidate_squeezed == _squeeze(name) and _squeeze(node_id) in allowed:
            return node_id
    return None


class MeshLocator:
    """Total mesh-locate engine: every answer is a bounded string, never an exception."""

    def __init__(
        self,
        settings: MeshtasticSettings,
        audit: AuditTrail,
        *,
        driver: MeshAccess | None = None,
        model_loader: ModelLoader | None = None,
        state_dir: Path | None = None,
        now: Callable[[], float] | None = None,
    ) -> None:
        self._settings = settings
        self._audit = audit
        # 123: the default is the daemon client — the engine itself never opens the port.
        self._driver = driver or mesh_access(settings)
        self._model_loader = model_loader or no_model
        self._now = now or time.time
        # 125: the engine no longer holds the transmission limiter — lookups are passive
        # and the daemon's automatic pass owns the budget. ``state_dir`` is accepted for
        # caller compatibility; nothing here uses it anymore.
        del state_dir

    # --- the one public entry point ------------------------------------------------------

    def locate(self, node_ref: str, *, reason: str, actor: str | None) -> str:
        port = self._driver.resolve_port()
        if isinstance(port, MeshFailure):
            return self._failure_answer(node_ref, port)
        snapshot = self._driver.snapshot(port)
        if isinstance(snapshot, MeshFailure):
            return self._failure_answer(node_ref, snapshot)

        # 117 record-on-sight: every table read this engine performs harvests the
        # allowlisted nodes' fixes into the history store (best-effort, never transmits,
        # never sacrifices the answer to a disk problem).
        self._record_snapshot(snapshot.nodes)

        # The report_only privacy gate (operator decision 2026-08-29): when set, the
        # RESOLUTION UNIVERSE shrinks to the listed nodes before any matching — other
        # stations' positions and even their names never appear in any answer (radio is
        # any-caller; strangers' trackers are not obi's to report).
        nodes = snapshot.nodes
        if self._settings.report_only:
            allowed = {_squeeze(entry) for entry in self._settings.report_only}
            nodes = tuple(
                node
                for node in snapshot.nodes
                if allowed
                & {_squeeze(node.node_id), _squeeze(node.short_name), _squeeze(node.long_name)}
            )

        # Operator aliases win before any mesh-DB matching ("art car" → the node id): an
        # alias is a deliberate naming decision, so it beats fuzzy resolution outright.
        # Squeezed comparison (the callsign matcher's) so spacing/case never matter.
        needle, alias_note = node_ref, ""
        squeezed_ref = _squeeze(node_ref)
        for alias, target in self._settings.aliases.items():
            if squeezed_ref and squeezed_ref == _squeeze(alias):
                needle, alias_note = target, f" (alias '{node_ref}')"
                break

        resolution = _resolve(needle, nodes, self._settings.display_names)
        if isinstance(resolution, Unknown):
            # The callsign-match stage (field 2026-08-29, "tumbler" for tumble): a fuzzy
            # near-miss above the caller threshold resolves instead of refusing —
            # unless two DIFFERENT nodes tie, which refuses (a wrong node is worse
            # than none). Runs only after exact/substring found nothing.
            resolution = self._fuzzy_resolve(node_ref, nodes, resolution)
        if isinstance(resolution, Ambiguous):
            return (
                f"Several mesh nodes match '{needle}'{alias_note}: "
                + ", ".join(resolution.candidates)
                + " — which one?"
            )
        if isinstance(resolution, Unknown):
            if self._settings.report_only:
                # 117: the motivating incident — an allowlisted node EVICTED from the
                # radio's table is unresolvable against it, but the recorded history
                # still knows where it last was. Config-only resolution, store lookup.
                config_id = self._config_node_id(needle)
                last = self._last_known_fix(config_id) if config_id else None
                if config_id is not None and last is not None:
                    display = self._settings.display_names.get(config_id) or config_id
                    return (
                        f"{display} was last known at {self._last_known_text(last)} — "
                        "it isn't in the radio's node table right now."
                    )[:MAX_ANSWER_CHARS]
                allowed_names = ", ".join(
                    sorted({_display(node, self._settings.display_names) for node in nodes})
                ) or ", ".join(self._settings.report_only)
                return (
                    f"No mesh node matches '{needle}'{alias_note} — mesh location is "
                    f"enabled only for {allowed_names} right now."
                )
            if resolution.suggestions:
                return (
                    f"No mesh node matches '{needle}'{alias_note} — closest are "
                    + ", ".join(resolution.suggestions)
                    + "."
                )
            return (
                f"No mesh node matches '{needle}'{alias_note} among the "
                f"{len(snapshot.nodes)} nodes the radio can hear."
            )

        node = resolution.node
        position, refresh_note = self._best_position(node)
        return self._render(node, position, refresh_note)

    def _fuzzy_resolve(
        self,
        node_ref: str,
        nodes: tuple[MeshNode, ...],
        fallback: Unknown,
    ) -> Resolved | Ambiguous | Unknown:
        """The radio callsign-match shape over node names AND alias keys.

        Same constants as ``radio/trigger.py``: squeeze, accept at ≥ 0.75, refuse a
        near-tie (< 0.05 margin) between two DIFFERENT nodes. Below threshold, the
        suggestions ``fallback`` stands.
        """

        squeezed = _squeeze(node_ref)
        if len(squeezed) < 2:
            return fallback
        by_id = {node.node_id: node for node in nodes}
        candidates: list[tuple[str, MeshNode]] = [
            (name, node) for node in nodes for name in (node.short_name, node.long_name) if name
        ]
        for alias, target in self._settings.aliases.items():
            resolved = _resolve(target, nodes, self._settings.display_names)
            if isinstance(resolved, Resolved):
                candidates.append((alias, resolved.node))
        if not candidates:
            return fallback
        scored = sorted(
            (
                (difflib.SequenceMatcher(None, squeezed, _squeeze(name)).ratio(), name, node)
                for name, node in candidates
            ),
            key=lambda item: item[0],
            reverse=True,
        )
        best_score, _best_name, best_node = scored[0]
        if best_score < _FUZZY_THRESHOLD:
            return fallback
        rivals = [
            node
            for score, _name, node in scored[1:]
            if node.node_id != best_node.node_id and best_score - score < _FUZZY_MARGIN
        ]
        if rivals:
            displays: list[str] = []
            for node in (best_node, *rivals):
                display = _display(by_id[node.node_id], self._settings.display_names)
                if display not in displays:
                    displays.append(display)
            return Ambiguous(tuple(displays[:_CANDIDATE_CAP]))
        return Resolved(best_node)

    # --- freshness + the ONE transmitting path -------------------------------------------

    def _best_position(self, node: MeshNode) -> tuple[MeshPosition | None, str | None]:
        """(best position, staleness note). 125: PURELY PASSIVE — a lookup never
        transmits or waits on the air. Freshness is the daemon's job now
        (:meth:`obi.core.meshdaemon.MeshDaemon._auto_refresh` buys it on the operator's
        schedule); the engine just reports what is recorded, with honest age."""

        stored = node.position
        stored_age = self._position_age(stored)
        fresh = (
            stored is not None
            and stored_age is not None
            and (stored_age <= self._settings.fresh_within_s)
        )
        if node.is_self or fresh:
            return stored, None
        return stored, "no fresher position has been heard"

    def _position_age(self, position: MeshPosition | None) -> float | None:
        if position is None or position.fix_time is None:
            return None
        return max(0.0, self._now() - position.fix_time)

    # --- record on sight + the sweep (feature 117) ---------------------------------------

    def record_pass(self) -> RecordOutcome:
        """One passive sweep: read the node table once, record allowlisted fixes.

        The sweep's only driver calls are ``resolve_port`` and ``snapshot`` — it is
        structurally unable to transmit (FR-002; a test pins it with a request path
        that raises). Radio absent/busy is a QUIET skip: the next cron tick resumes.
        """

        port = self._driver.resolve_port()
        if isinstance(port, MeshFailure):
            logger.info("mesh record: skipped — %s", self._failure_reason(port))
            return RecordOutcome(0, 0, skipped=self._failure_reason(port))
        snapshot = self._driver.snapshot(port)
        if isinstance(snapshot, MeshFailure):
            logger.info("mesh record: skipped — %s", self._failure_reason(snapshot))
            return RecordOutcome(0, 0, skipped=self._failure_reason(snapshot))
        recorded, tracked, error = self._record_snapshot(snapshot.nodes)
        logger.info(
            "mesh record: %d new fix(es), %d allowlisted node(s) with a fix in the table",
            recorded,
            tracked,
        )
        return RecordOutcome(recorded, tracked, error=error)

    def _record_snapshot(self, nodes: tuple[MeshNode, ...]) -> tuple[int, int, str | None]:
        """(new rows, allowlisted fixes seen, store error). Best-effort; never raises."""

        return record_fixes(self._settings, nodes, model_loader=self._model_loader, now=self._now)

    def _record_positions(
        self, sightings: list[tuple[MeshNode, MeshPosition]]
    ) -> tuple[int, str | None]:
        """Write fixes for already-vetted sightings; a StoreError is logged, never raised."""

        return _record_position_rows(
            self._settings, sightings, model_loader=self._model_loader, now=self._now
        )

    # --- the last-known fallback (feature 117) -------------------------------------------

    def _config_node_id(self, node_ref: str) -> str | None:
        return config_node_id(self._settings, node_ref)

    def _is_allowlisted(self, node_id: str) -> bool:
        allowed = {_squeeze(entry) for entry in self._settings.report_only}
        return _squeeze(node_id) in allowed

    def _last_known_fix(self, node_id: str) -> meshstore.RecordedFix | None:
        """The store's freshest fix for ``node_id``; any store trouble reads as no history."""

        try:
            conn = meshstore.connect_readonly(self._settings.positions_db_path)
        except meshstore.StoreError:
            return None
        try:
            return meshstore.last_known(conn, node_id)
        except meshstore.StoreError:
            return None
        finally:
            with contextlib.suppress(Exception):
                conn.close()

    def _last_known_text(self, fix: meshstore.RecordedFix) -> str:
        age = max(0.0, self._now() - fix.effective_fix_s)
        return f"{fix.address}, {_ago(age)}"

    def _failure_answer(self, node_ref: str, failure: MeshFailure) -> str:
        """Radio unreachable/busy: last-known when history exists, today's prose otherwise."""

        config_id = self._config_node_id(node_ref)
        last = self._last_known_fix(config_id) if config_id else None
        if config_id is None or last is None:
            return self._failure_prose(failure)
        display = self._settings.display_names.get(config_id) or config_id
        return (
            f"{display} was last known at {self._last_known_text(last)} — "
            f"{self._failure_reason(failure)}."
        )[:MAX_ANSWER_CHARS]

    # --- rendering -----------------------------------------------------------------------

    def _failure_reason(self, failure: MeshFailure) -> str:
        if failure.outcome is MeshOutcome.NO_RADIO:
            return f"the mesh radio isn't reachable ({failure.detail})"
        if failure.outcome is MeshOutcome.PORT_BUSY:
            return f"the mesh radio is busy ({failure.detail})"
        if failure.outcome is MeshOutcome.DAEMON_DOWN:
            return f"the mesh daemon isn't running ({failure.detail})"
        return f"mesh lookup failed ({failure.detail})"

    def _failure_prose(self, failure: MeshFailure) -> str:
        reason = self._failure_reason(failure)
        return reason[0].upper() + reason[1:] + "."

    def _render(
        self, node: MeshNode, position: MeshPosition | None, refresh_note: str | None
    ) -> str:
        display = _display(node, self._settings.display_names)
        address: BrcAddress | None = None
        if position is not None:
            address = to_address(position.latitude, position.longitude, self._model_loader())

        if address is not None and position is not None:
            # 126 (operator directive 2026-09-02): a position-bearing answer is ONE
            # sentence — the position, plainly, with a nearby art piece as the landmark
            # people can actually see. No last-heard/battery bookkeeping, and no age
            # words while the position is current; stale positions keep honest age.
            place = address.text
            art = artmarks.nearest_within(
                position.latitude,
                position.longitude,
                artmarks.load_art(self._settings.art_path),
                self._settings.art_near_ft,
            )
            if art is not None:
                place = f"{place}, near {art.name}"
            if refresh_note is None:
                return f"{display} is at {place}."[:MAX_ANSWER_CHARS]
            age = self._position_age(position)
            as_of = _ago(age) if age is not None else "an unknown time ago"
            return (f"{display} was at {place} as of {as_of} — {refresh_note}.")[:MAX_ANSWER_CHARS]

        if refresh_note is not None:
            last = (
                self._last_known_fix(node.node_id) if self._is_allowlisted(node.node_id) else None
            )
            if last is not None:
                # 117: the node is live on the mesh but positionless — the history line
                # rides the same head sentence so the radio's one breath carries it.
                head = (
                    f"{display} has no live position — {refresh_note}. "
                    f"Last known: {self._last_known_text(last)}."
                )
            else:
                head = f"{display} has no known position — {refresh_note}."
        else:
            head = f"{display} has no known position."

        lines = [head]
        heard = self._heard_line(node, likely_nearby=position is None)
        if heard is not None:
            lines.append(heard)
        if node.battery_level is not None:
            if node.battery_level > 100:
                lines.append("- On external power.")
            else:
                lines.append(f"- Battery {node.battery_level}%.")

        answer = "\n".join(lines)
        while len(answer) > MAX_ANSWER_CHARS and len(lines) > 1:
            lines.pop()
            answer = "\n".join(lines)
        return answer[:MAX_ANSWER_CHARS]

    def _heard_line(self, node: MeshNode, *, likely_nearby: bool) -> str | None:
        # 126: reached only for POSITIONLESS answers now — heard/signal detail is all
        # such an answer has to offer (and carries the "likely nearby" hint).
        if node.last_heard is None:
            return None
        elapsed = max(0.0, self._now() - node.last_heard)
        parts = [f"- Last heard {_ago(elapsed)}"]
        if node.hops_away is not None:
            if node.hops_away == 0:
                parts.append("direct")
            else:
                parts.append(f"via {node.hops_away} hop{'s' if node.hops_away != 1 else ''}")
        strong = node.snr is not None and node.snr >= _STRONG_SNR_DB
        if node.snr is not None:
            quality = "strong" if strong else ("ok" if node.snr >= -5.0 else "weak")
            parts.append(f"{quality} signal (SNR {node.snr:g})")
        line = ", ".join(parts)
        if likely_nearby and node.hops_away == 0 and strong:
            line += " — likely nearby"
        return line + "."
