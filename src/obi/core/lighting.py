"""Interface-agnostic lighting engine (feature 103): obi's first device-write path.

Every surface (chat/Slack/voice tool handlers, the CLI) funnels through :class:`Lighting`, so
the guarantees live HERE, once: targets resolve only through the ``[wled]`` allowlist (a
non-allowlisted name is refused before any network contact); a realtime stream in control of
the device refuses the write honestly (during streams WLED ignores state writes frame-by-frame);
the state being overwritten is captured BEFORE every write — into the audit record always, and
into the one-deep snapshot store that ``revert`` restores; dry-run reports exactly what would
change and writes nothing. The gate ORDER is normative (contracts/tools.md): allowlist →
live-stream → snapshot → dry-run → apply.

The portal is shared, unauthenticated, last-writer-wins hardware: obi never writes unprompted
(no scheduler or watcher reaches this module), and revert exists precisely so any obi change —
including one that overwrote a camp mate's look — is one request away from undone.
"""

from __future__ import annotations

import contextlib
import difflib
import json
import os
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from obi.core.audit import AuditTrail
from obi.core.config import WledSettings, default_wled_snapshots_path
from obi.core.drivers.wled import (
    WledDriver,
    WledEffects,
    WledInfo,
    WledPalettes,
    WledResult,
    WledState,
)
from obi.core.models import AuditOutcome, AuditRecord, _utcnow


class LightingDriver(Protocol):
    """What the engine needs from a lighting adapter (satisfied by :class:`WledDriver`).

    Lives with the engine, not in ``drivers/base.py``: the base family is read-only network
    vendors by constitution; lighting is its own, explicitly write-capable adapter class
    (1.15.0, Principle IV note).
    """

    def info(self, base_url: str) -> WledResult: ...

    def state(self, base_url: str) -> WledResult: ...

    def effects(self, base_url: str) -> WledResult: ...

    def palettes(self, base_url: str) -> WledResult: ...

    def apply(self, base_url: str, partial_state: dict[str, Any]) -> WledResult: ...


_NO_DEVICE = "No lighting device is configured — add a [wled.devices] section to obi's config."

# Common color names → RGB. Deliberately small and static: hex covers everything else, and the
# model composing tool calls is told the accepted forms in the tool description.
_COLOR_NAMES: dict[str, tuple[int, int, int]] = {
    "black": (0, 0, 0),
    "white": (255, 255, 255),
    "warm white": (255, 170, 90),
    "red": (255, 0, 0),
    "green": (0, 200, 0),
    "blue": (0, 0, 255),
    "yellow": (255, 220, 0),
    "orange": (255, 140, 0),
    "amber": (255, 160, 0),
    "purple": (160, 32, 240),
    "violet": (160, 32, 240),
    "magenta": (255, 0, 255),
    "pink": (255, 105, 180),
    "cyan": (0, 255, 255),
    "teal": (0, 160, 160),
    "turquoise": (64, 224, 208),
    "gold": (255, 200, 0),
    "coral": (255, 127, 80),
    "salmon": (250, 128, 114),
    "indigo": (75, 0, 130),
    "navy": (0, 0, 128),
    "maroon": (128, 0, 0),
    "olive": (128, 128, 0),
    "gray": (128, 128, 128),
    "grey": (128, 128, 128),
    "silver": (192, 192, 192),
    "brown": (139, 69, 19),
}


def resolve_color(text: str) -> tuple[int, int, int] | None:
    """``#RRGGBB``/``RRGGBB`` hex or a known color name; ``None`` when unrecognized."""

    cleaned = text.strip().lower()
    if cleaned in _COLOR_NAMES:
        return _COLOR_NAMES[cleaned]
    hex_part = cleaned.removeprefix("#")
    if len(hex_part) == 6 and all(c in "0123456789abcdef" for c in hex_part):
        return (int(hex_part[0:2], 16), int(hex_part[2:4], 16), int(hex_part[4:6], 16))
    return None


def _resolve_catalog_name(name: str, catalog: list[str]) -> tuple[int | None, list[str]]:
    """Resolve a name against a device-reported catalog (103 research D4; shared by effects
    and palettes since 107 — index = device id in both).

    Returns ``(id, [])`` on a match — exact, then spaceless exact (spoken requests arrive
    with spaces the catalog may not have: "ice fire" IS "Icefire" — the radio-call-24
    phrasing), then unique substring — or ``(None, closest_names)`` on a miss/ambiguity:
    the caller answers "no such X, closest are …" and writes nothing.
    """

    needle = name.strip().lower()
    lowered = [entry.lower() for entry in catalog]
    if needle in lowered:
        return lowered.index(needle), []
    squashed = needle.replace(" ", "")
    squashed_catalog = [entry.replace(" ", "") for entry in lowered]
    if squashed and squashed in squashed_catalog:
        return squashed_catalog.index(squashed), []
    substr = [i for i, entry in enumerate(lowered) if needle in entry]
    if len(substr) == 1:
        return substr[0], []
    if len(substr) > 1:
        return None, [catalog[i] for i in substr[:5]]
    close = difflib.get_close_matches(needle, lowered, n=5, cutoff=0.6)
    return None, [catalog[lowered.index(c)] for c in close]


def resolve_effect(name: str, catalog: list[str]) -> tuple[int | None, list[str]]:
    """Resolve an effect name against the device's live effect catalog."""

    return _resolve_catalog_name(name, catalog)


def resolve_palette(name: str, catalog: list[str]) -> tuple[int | None, list[str]]:
    """Resolve a palette name against the device's live palette catalog (feature 107)."""

    return _resolve_catalog_name(name, catalog)


def _pct_to_scale(pct: int, scale: int = 255) -> int:
    """Caller-facing percent → device scale, clamped. WLED's sx/ix/c1/c2 run 0-255; the
    third custom slider (c3) runs 0-31 (research D2)."""

    return round(max(0, min(100, pct)) * scale / 100)


def _scale_to_pct(value: int, scale: int = 255) -> int:
    return round(max(0, min(scale, value)) * 100 / scale)


@dataclass(frozen=True)
class Snapshot:
    """The look captured immediately before an obi write — what ``revert`` restores."""

    device: str
    saved_at: str
    cause: str
    state: dict[str, Any]


def _view_segment(state: WledState, seg_id: int | None) -> WledState:
    """A view of ``state`` whose parsed color/effect/palette/speed/intensity conveniences
    come from the segment with ``seg_id`` instead of ``seg[0]`` (128).

    Under a declared layout ``seg[0]`` is a STATUS band, so an operator confirmation and the
    palette-visibility check must read the first MAIN segment — the pattern area the look
    actually landed on — or they describe the wrong strip (e.g. warning "solid won't show the
    palette" because the status band is Solid, while the animated main segments show it fine).
    Returns ``state`` unchanged when ``seg_id`` is ``None`` or absent from the device state.
    """

    if seg_id is None:
        return state
    seg = next(
        (s for s in state.raw.get("seg", []) if isinstance(s, dict) and s.get("id") == seg_id),
        None,
    )
    if seg is None:
        return state
    cols = seg.get("col")
    color: tuple[int, int, int] | None = None
    if isinstance(cols, list) and cols and isinstance(cols[0], list) and len(cols[0]) >= 3:
        color = (int(cols[0][0]), int(cols[0][1]), int(cols[0][2]))
    return replace(
        state,
        primary_color=color,
        effect_id=int(seg["fx"]) if isinstance(seg.get("fx"), int) else None,
        palette_id=int(seg["pal"]) if isinstance(seg.get("pal"), int) else None,
        speed=int(seg["sx"]) if isinstance(seg.get("sx"), int) else None,
        intensity=int(seg["ix"]) if isinstance(seg.get("ix"), int) else None,
    )


def describe_state(
    state: WledState,
    catalog: list[str] | None = None,
    palettes: list[str] | None = None,
) -> str:
    """One human line for a lighting state, used in confirmations and dry-run diffs."""

    if not state.on:
        return "off"
    parts = [f"on at {round(state.brightness / 255 * 100)}%"]
    if state.primary_color is not None:
        r, g, b = state.primary_color
        parts.append(f"color #{r:02X}{g:02X}{b:02X}")
    if state.effect_id is not None:
        if catalog and 0 <= state.effect_id < len(catalog):
            parts.append(f"effect {catalog[state.effect_id]!r}")
        elif state.effect_id != 0:
            parts.append(f"effect #{state.effect_id}")
    # 107: name the palette (0 = Default, elided) and, for animated effects, the settings.
    if state.palette_id is not None and state.palette_id != 0:
        if palettes and 0 <= state.palette_id < len(palettes):
            parts.append(f"palette {palettes[state.palette_id]!r}")
        else:
            parts.append(f"palette #{state.palette_id}")
    if state.effect_id not in (None, 0):
        if state.speed is not None:
            parts.append(f"speed {_scale_to_pct(state.speed)}%")
        if state.intensity is not None:
            parts.append(f"intensity {_scale_to_pct(state.intensity)}%")
    return ", ".join(parts)


class Lighting:
    """The engine. One instance per call site; construction does no I/O."""

    def __init__(
        self,
        settings: WledSettings,
        audit: AuditTrail,
        *,
        driver: LightingDriver | None = None,
        snapshots_path: Path | None = None,
    ) -> None:
        self._settings = settings
        self._audit = audit
        self._driver: LightingDriver = driver if driver is not None else WledDriver(settings)
        self._snapshots_path = (
            snapshots_path if snapshots_path is not None else default_wled_snapshots_path()
        )

    # -- device resolution (the allowlist gate) ------------------------------------------------

    def _resolve(self, device: str | None) -> tuple[str, str] | str:
        """Resolve a device name through the allowlist → ``(name, base_url)`` or an error string."""

        devices = self._settings.devices
        if device is None or not device.strip():
            if len(devices) == 1:
                name = next(iter(devices))
                return name, devices[name]
            return "Which lighting device? Configured: " + ", ".join(sorted(devices))
        name = device.strip()
        if name in devices:
            return name, devices[name]
        return (
            f"{name!r} is not an allowlisted lighting device — configured: "
            + ", ".join(sorted(devices))
            + ". obi only ever writes to [wled.devices] entries."
        )

    # -- snapshot store (feature FR-006; one-deep, atomic) -------------------------------------

    def _load_snapshots(self) -> dict[str, Any]:
        try:
            data = json.loads(self._snapshots_path.read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def held_snapshot(self, device: str) -> Snapshot | None:
        entry = self._load_snapshots().get(device)
        if not isinstance(entry, dict) or not isinstance(entry.get("state"), dict):
            return None
        return Snapshot(
            device=device,
            saved_at=str(entry.get("saved_at", "?")),
            cause=str(entry.get("cause", "?")),
            state=entry["state"],
        )

    def _persist_snapshot(self, snapshot: Snapshot) -> None:
        data = self._load_snapshots()
        data[snapshot.device] = {
            "saved_at": snapshot.saved_at,
            "cause": snapshot.cause,
            "state": snapshot.state,
        }
        self._snapshots_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=self._snapshots_path.parent, prefix=self._snapshots_path.name, suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(data, handle, indent=1)
            os.replace(tmp, self._snapshots_path)
        except OSError:
            # A failed persist must not block the write the operator asked for: the same
            # snapshot is already in the audit record. Best-effort cleanup, then continue.
            with contextlib.suppress(OSError):
                os.unlink(tmp)

    # -- audit ----------------------------------------------------------------------------------

    def _record(
        self,
        *,
        action: str,
        target: str,
        outcome: AuditOutcome,
        params: dict[str, object],
        detail: str,
        reason: str,
        actor: str | None,
    ) -> None:
        self._audit.record(
            AuditRecord(
                timestamp=_utcnow(),
                action=action,
                target=target,
                reason=reason or "lighting request",
                outcome=outcome,
                params=params,
                detail=detail,
                actor=actor,
            )
        )

    # -- reads ----------------------------------------------------------------------------------

    def status(self, device: str | None = None) -> str:
        """Read-only status line set for one device (the ``wled_status`` tool / CLI)."""

        resolved = self._resolve(device)
        if isinstance(resolved, str):
            return resolved
        name, base_url = resolved

        info_result = self._driver.info(base_url)
        if not info_result.ok or not isinstance(info_result.value, WledInfo):
            return f"{name}: unreachable — {info_result.error}"
        info = info_result.value

        lines = [f"{name} ({info.name}, WLED {info.version})"]
        state_result = self._driver.state(base_url)
        catalog: list[str] = []
        effects_result = self._driver.effects(base_url)
        if effects_result.ok and isinstance(effects_result.value, WledEffects):
            catalog = effects_result.value.names
        # 107: the palette catalog names the active palette; a failed read degrades to the
        # numeric id inside describe_state, never to a failed status.
        palette_catalog: list[str] = []
        palettes_result = self._driver.palettes(base_url)
        if palettes_result.ok and isinstance(palettes_result.value, WledPalettes):
            palette_catalog = palettes_result.value.names
        if state_result.ok and isinstance(state_result.value, WledState):
            # 128: under a declared layout seg[0] is a STATUS band — status must describe the
            # first MAIN segment (the pattern area) or it reports the band, not the look.
            layout = self._settings.segments.get(name)
            describe_id = layout.main[0] if layout is not None else None
            look = _view_segment(state_result.value, describe_id)
            lines.append(f"  look: {describe_state(look, catalog, palette_catalog or None)}")
        else:
            lines.append(f"  look: unavailable — {state_result.error}")
        if info.live:
            source = f" (source: {info.live_source})" if info.live_source else ""
            lines.append(f"  live stream IN CONTROL{source} — obi writes would not stick")
        details = []
        if info.led_count is not None:
            details.append(f"{info.led_count} LEDs")
        if info.power_estimate_mw is not None:
            details.append(f"~{info.power_estimate_mw / 1000:.1f} W")
        if info.wifi_rssi is not None:
            details.append(f"wifi {info.wifi_rssi} dBm")
        if info.uptime_s is not None:
            details.append(f"up {info.uptime_s // 3600}h{(info.uptime_s % 3600) // 60:02d}m")
        if details:
            lines.append("  " + ", ".join(details))
        held = self.held_snapshot(name)
        if held is not None:
            lines.append(
                f"  revert available: obi holds the look from before its last change "
                f"({held.saved_at})"
            )
        return "\n".join(lines)

    # -- writes (constitution 1.15.0: the decorative-lighting write class) -----------------------

    def set_look(
        self,
        device: str | None = None,
        *,
        color: str | None = None,
        color2: str | None = None,
        color3: str | None = None,
        effect: str | None = None,
        palette: str | None = None,
        brightness_pct: int | None = None,
        speed_pct: int | None = None,
        intensity_pct: int | None = None,
        custom1_pct: int | None = None,
        custom2_pct: int | None = None,
        custom3_pct: int | None = None,
        power: bool | None = None,
        dry_run: bool = False,
        reason: str = "",
        actor: str | None = None,
    ) -> str:
        """Apply an operator-requested look. Gate order is normative — see the module docstring."""

        requested: dict[str, object] = {
            key: value
            for key, value in (
                ("color", color),
                ("color2", color2),
                ("color3", color3),
                ("effect", effect),
                ("palette", palette),
                ("brightness_pct", brightness_pct),
                ("speed_pct", speed_pct),
                ("intensity_pct", intensity_pct),
                ("custom1_pct", custom1_pct),
                ("custom2_pct", custom2_pct),
                ("custom3_pct", custom3_pct),
                ("power", power),
            )
            if value is not None
        }
        if not requested:
            return (
                "Nothing to change — give a color, palette, effect, speed/intensity, "
                "brightness, or on/off."
            )

        resolved = self._resolve(device)
        if isinstance(resolved, str):
            if device:
                self._record(
                    action="wled_set",
                    target=str(device),
                    outcome=AuditOutcome.DENIED,
                    params={"requested": requested},
                    detail="not on the [wled.devices] allowlist",
                    reason=reason,
                    actor=actor,
                )
            return resolved
        name, base_url = resolved

        def refuse(detail: str, message: str) -> str:
            # 107 research D7: every resolution failure is audited — radio-call-24's failed
            # catalog read left a tool_call row but no action row, and the trail must tell
            # the whole story on its own.
            self._record(
                action="wled_set",
                target=name,
                outcome=AuditOutcome.ERROR,
                params={"requested": requested},
                detail=detail,
                reason=reason,
                actor=actor,
            )
            return message

        resolved_colors: list[tuple[int, int, int] | None] = []
        for slot_label, slot_text in (("color", color), ("color2", color2), ("color3", color3)):
            if slot_text is None:
                resolved_colors.append(None)
                continue
            slot_rgb = resolve_color(slot_text)
            if slot_rgb is None:
                return refuse(
                    f"unknown {slot_label} {slot_text!r}",
                    f"I don't know the color {slot_text!r} — use #RRGGBB hex or one of: "
                    + ", ".join(sorted(_COLOR_NAMES)),
                )
            resolved_colors.append(slot_rgb)
        rgb, rgb2, rgb3 = resolved_colors

        effect_id: int | None = None
        catalog: list[str] = []
        if effect is not None:
            effects_result = self._driver.effects(base_url)
            if not effects_result.ok or not isinstance(effects_result.value, WledEffects):
                return refuse(
                    f"could not read the effect catalog: {effects_result.error}",
                    f"{name}: could not read the effect catalog — {effects_result.error}",
                )
            catalog = effects_result.value.names
            effect_id, suggestions = resolve_effect(effect, catalog)
            if effect_id is None:
                hint = ("; closest matches: " + ", ".join(suggestions)) if suggestions else ""
                return refuse(
                    f"no pattern named {effect!r}",
                    f"{name} has no pattern named {effect!r}{hint}. Nothing changed.",
                )

        palette_id: int | None = None
        palette_catalog: list[str] = []
        if palette is not None:
            palettes_result = self._driver.palettes(base_url)
            if not palettes_result.ok or not isinstance(palettes_result.value, WledPalettes):
                return refuse(
                    f"could not read the palette catalog: {palettes_result.error}",
                    f"{name}: could not read the palette catalog — {palettes_result.error}",
                )
            palette_catalog = palettes_result.value.names
            palette_id, suggestions = resolve_palette(palette, palette_catalog)
            if palette_id is None:
                hint = ("; closest matches: " + ", ".join(suggestions)) if suggestions else ""
                return refuse(
                    f"no palette named {palette!r}",
                    f"{name} has no palette named {palette!r}{hint}. Nothing changed.",
                )

        info_result = self._driver.info(base_url)
        if not info_result.ok or not isinstance(info_result.value, WledInfo):
            self._record(
                action="wled_set",
                target=name,
                outcome=AuditOutcome.UNREACHABLE,
                params={"requested": requested},
                detail=str(info_result.error),
                reason=reason,
                actor=actor,
            )
            return f"{name}: unreachable — {info_result.error}. Nothing changed."
        if info_result.value.live:
            source = info_result.value.live_source or "realtime stream"
            self._record(
                action="wled_set",
                target=name,
                outcome=AuditOutcome.DENIED,
                params={"requested": requested},
                detail=f"live stream in control ({source})",
                reason=reason,
                actor=actor,
            )
            return (
                f"A live show ({source}) is driving {name} right now — a state write would "
                "not stick, so obi is not sending one. Try again when the stream stops."
            )

        state_result = self._driver.state(base_url)
        if not state_result.ok or not isinstance(state_result.value, WledState):
            self._record(
                action="wled_set",
                target=name,
                outcome=AuditOutcome.ERROR,
                params={"requested": requested},
                detail=f"could not capture pre-change state: {state_result.error}",
                reason=reason,
                actor=actor,
            )
            return (
                f"{name}: could not capture the current look ({state_result.error}) — "
                "not writing without a snapshot. Nothing changed."
            )
        before = state_result.value

        # 128: a declared segment-role layout bounds every operator look write to the MAIN
        # segments — the status bands belong to the dash reconciler, never to a look.
        layout = self._settings.segments.get(name)
        partial = _build_partial_state(
            before,
            rgb=rgb,
            rgb2=rgb2,
            rgb3=rgb3,
            effect_id=effect_id,
            palette_id=palette_id,
            brightness_pct=brightness_pct,
            speed=_pct_to_scale(speed_pct) if speed_pct is not None else None,
            intensity=_pct_to_scale(intensity_pct) if intensity_pct is not None else None,
            custom1=_pct_to_scale(custom1_pct) if custom1_pct is not None else None,
            custom2=_pct_to_scale(custom2_pct) if custom2_pct is not None else None,
            custom3=_pct_to_scale(custom3_pct, 31) if custom3_pct is not None else None,
            power=power,
            main_ids=layout.main if layout is not None else None,
        )
        # 128: describe the pattern area the look lands on, not seg[0] (a status band under a
        # layout) — the confirmation and the palette-visibility note must read the first MAIN
        # segment or they narrate the wrong strip.
        describe_id = layout.main[0] if layout is not None else None
        was = describe_state(
            _view_segment(before, describe_id), catalog or None, palette_catalog or None
        )

        if dry_run:
            self._record(
                action="wled_set",
                target=name,
                outcome=AuditOutcome.OK,
                params={"requested": requested, "would_send": partial, "dry_run": True},
                detail=f"dry-run; current look: {was}",
                reason=reason,
                actor=actor,
            )
            return (
                f"Dry-run for {name}: would send {json.dumps(partial, sort_keys=True)} "
                f"(currently {was}). Nothing changed."
            )

        snapshot = Snapshot(
            device=name,
            saved_at=_utcnow().isoformat(timespec="seconds"),
            cause=reason or "wled_set",
            state=before.raw,
        )
        self._persist_snapshot(snapshot)

        apply_result = self._driver.apply(base_url, partial)
        if not apply_result.ok or not isinstance(apply_result.value, WledState):
            self._record(
                action="wled_set",
                target=name,
                outcome=AuditOutcome.ERROR,
                params={"requested": requested, "snapshot": before.raw},
                detail=str(apply_result.error),
                reason=reason,
                actor=actor,
            )
            return f"{name}: the change failed — {apply_result.error}."
        now_state = _view_segment(apply_result.value, describe_id)
        now = describe_state(now_state, catalog or None, palette_catalog or None)
        self._record(
            action="wled_set",
            target=name,
            outcome=AuditOutcome.OK,
            params={"requested": requested, "sent": partial, "snapshot": before.raw},
            detail=f"was: {was}; now: {now}",
            reason=reason,
            actor=actor,
        )
        message = f"{name} is now {now} (was {was}). Say '{name} revert' to put it back."
        if palette is not None and now_state.effect_id == 0:
            # Legal but invisible (FR-007): Solid renders colors, not palettes — say so
            # instead of letting the caller think nothing happened.
            message += (
                " Note: the current pattern (solid color) won't show the palette — "
                "pick an animated pattern to see it."
            )
        return message

    def revert(
        self,
        device: str | None = None,
        *,
        dry_run: bool = False,
        reason: str = "",
        actor: str | None = None,
    ) -> str:
        """Restore the held pre-change snapshot verbatim; the revert re-snapshots what IT
        overwrites, so a revert is itself revertible (US2)."""

        resolved = self._resolve(device)
        if isinstance(resolved, str):
            return resolved
        name, base_url = resolved

        held = self.held_snapshot(name)
        if held is None:
            return (
                f"Nothing to revert on {name} — obi holds no pre-change look "
                "(it has not changed this device, or the snapshot was already restored)."
            )

        info_result = self._driver.info(base_url)
        if not info_result.ok or not isinstance(info_result.value, WledInfo):
            self._record(
                action="wled_revert",
                target=name,
                outcome=AuditOutcome.UNREACHABLE,
                params={"snapshot_saved_at": held.saved_at},
                detail=str(info_result.error),
                reason=reason,
                actor=actor,
            )
            return f"{name}: unreachable — {info_result.error}. Nothing changed."
        if info_result.value.live:
            source = info_result.value.live_source or "realtime stream"
            self._record(
                action="wled_revert",
                target=name,
                outcome=AuditOutcome.DENIED,
                params={"snapshot_saved_at": held.saved_at},
                detail=f"live stream in control ({source})",
                reason=reason,
                actor=actor,
            )
            return (
                f"A live show ({source}) is driving {name} right now — a state write would "
                "not stick, so obi is not sending one. Try again when the stream stops."
            )

        if dry_run:
            self._record(
                action="wled_revert",
                target=name,
                outcome=AuditOutcome.OK,
                params={"snapshot_saved_at": held.saved_at, "dry_run": True},
                detail="dry-run",
                reason=reason,
                actor=actor,
            )
            return (
                f"Dry-run for {name}: would restore the look captured {held.saved_at} "
                f"(cause: {held.cause}). Nothing changed."
            )

        state_result = self._driver.state(base_url)
        overwritten = (
            state_result.value.raw
            if state_result.ok and isinstance(state_result.value, WledState)
            else None
        )

        # 128: with a declared layout, restore only the MAIN segments' look (plus global
        # power/brightness) — the status bands keep whatever they show NOW, not whatever
        # the snapshot happened to record (FR-002). No layout ⇒ verbatim full-state POST,
        # byte-identical to pre-128 behavior.
        layout = self._settings.segments.get(name)
        to_apply = (
            _scoped_revert_partial(held.state, layout.main) if layout is not None else held.state
        )

        apply_result = self._driver.apply(base_url, to_apply)
        if not apply_result.ok or not isinstance(apply_result.value, WledState):
            self._record(
                action="wled_revert",
                target=name,
                outcome=AuditOutcome.ERROR,
                params={"snapshot_saved_at": held.saved_at},
                detail=str(apply_result.error),
                reason=reason,
                actor=actor,
            )
            return f"{name}: the revert failed — {apply_result.error}."

        if overwritten is not None:
            self._persist_snapshot(
                Snapshot(
                    device=name,
                    saved_at=_utcnow().isoformat(timespec="seconds"),
                    cause=reason or "wled_revert",
                    state=overwritten,
                )
            )
        self._record(
            action="wled_revert",
            target=name,
            outcome=AuditOutcome.OK,
            params={
                "snapshot_restored": held.state,
                "snapshot_saved_at": held.saved_at,
                "new_snapshot": overwritten,
            },
            detail=f"restored the look captured {held.saved_at}",
            reason=reason,
            actor=actor,
        )
        return (
            f"{name} restored to the look from before obi's last change "
            f"(captured {held.saved_at}). The overwritten look is held — revert again to undo."
        )

    # -- the status-band write path (feature 128, constitution 2.3.0) ---------------------------

    def set_status_band(
        self,
        device: str | None = None,
        *,
        look: str,
        reason: str = "",
        actor: str | None = None,
        dry_run: bool = False,
    ) -> str:
        """Set the declared STATUS segments to one of the closed vocabulary looks.

        The ONE code path of the 2.3.0 status-band clause: reachable only from the dash
        reconciler and the operator CLI — never from any toolset. Targets the layout's
        status ids exclusively, never reads or writes the operator revert snapshot, and is
        total (returns a message on every path). Gate order mirrors ``set_look``.
        """

        requested: dict[str, object] = {"look": look}

        resolved = self._resolve(device)
        if isinstance(resolved, str):
            if device:
                self._record(
                    action="wled_status_set",
                    target=str(device),
                    outcome=AuditOutcome.DENIED,
                    params={"requested": requested},
                    detail="not on the [wled.devices] allowlist",
                    reason=reason,
                    actor=actor,
                )
            return resolved
        name, base_url = resolved

        def refuse(detail: str, message: str, outcome: AuditOutcome = AuditOutcome.ERROR) -> str:
            self._record(
                action="wled_status_set",
                target=name,
                outcome=outcome,
                params={"requested": requested},
                detail=detail,
                reason=reason,
                actor=actor,
            )
            return message

        if look not in STATUS_LOOKS:
            return refuse(
                f"unknown status look {look!r}",
                f"Unknown status look {look!r} — the vocabulary is: "
                + ", ".join(STATUS_LOOKS)
                + ".",
            )

        layout = self._settings.segments.get(name)
        if layout is None:
            return refuse(
                "no [wled.segments] layout declared",
                f"{name} has no segment layout declared ([wled.segments.{name}]) — "
                "obi does not know which segments are the status bands. Nothing changed.",
            )

        seg_change = dict(STATUS_LOOKS[look])
        if look == "red":
            effects_result = self._driver.effects(base_url)
            if not effects_result.ok or not isinstance(effects_result.value, WledEffects):
                return refuse(
                    f"could not read the effect catalog: {effects_result.error}",
                    f"{name}: could not read the effect catalog — {effects_result.error}. "
                    "Nothing changed.",
                )
            blink_id, _ = resolve_effect(_RED_BLINK_EFFECT, effects_result.value.names)
            if blink_id is None:
                return refuse(
                    f"device has no {_RED_BLINK_EFFECT!r} effect",
                    f"{name} has no {_RED_BLINK_EFFECT!r} effect in its catalog. Nothing changed.",
                )
            seg_change["fx"] = blink_id

        info_result = self._driver.info(base_url)
        if not info_result.ok or not isinstance(info_result.value, WledInfo):
            self._record(
                action="wled_status_set",
                target=name,
                outcome=AuditOutcome.UNREACHABLE,
                params={"requested": requested},
                detail=str(info_result.error),
                reason=reason,
                actor=actor,
            )
            return f"{name}: unreachable — {info_result.error}. Nothing changed."
        if info_result.value.live:
            source = info_result.value.live_source or "realtime stream"
            self._record(
                action="wled_status_set",
                target=name,
                outcome=AuditOutcome.DENIED,
                params={"requested": requested},
                detail=f"live stream in control ({source})",
                reason=reason,
                actor=actor,
            )
            return (
                f"A live show ({source}) is driving {name} right now — a state write would "
                "not stick, so obi is not sending one. Try again when the stream stops."
            )

        partial: dict[str, Any] = {"seg": [{"id": sid, **seg_change} for sid in layout.status]}
        if look != "off":
            partial["on"] = True

        if dry_run:
            self._record(
                action="wled_status_set",
                target=name,
                outcome=AuditOutcome.OK,
                params={"requested": requested, "would_send": partial, "dry_run": True},
                detail="dry-run",
                reason=reason,
                actor=actor,
            )
            return (
                f"Dry-run for {name}: would send {json.dumps(partial, sort_keys=True)}. "
                "Nothing changed."
            )

        apply_result = self._driver.apply(base_url, partial)
        if not apply_result.ok or not isinstance(apply_result.value, WledState):
            self._record(
                action="wled_status_set",
                target=name,
                outcome=AuditOutcome.ERROR,
                params={"requested": requested, "sent": partial},
                detail=str(apply_result.error),
                reason=reason,
                actor=actor,
            )
            return f"{name}: the status write failed — {apply_result.error}."
        self._record(
            action="wled_status_set",
            target=name,
            outcome=AuditOutcome.OK,
            params={"requested": requested, "sent": partial},
            detail=f"status bands -> {look}",
            reason=reason,
            actor=actor,
        )
        return f"{name} status bands are now {look}."


#: The per-segment fields that make up a segment's LOOK — what a scoped revert restores.
_SEGMENT_LOOK_KEYS = ("col", "fx", "pal", "sx", "ix", "c1", "c2", "c3", "on", "bri")

#: 128: the status-band vocabulary — the WHOLE expressible surface of the status write path
#: (constitution 2.3.0). Colors are operator-tuned (2026-09-02, greened again same day): a
#: pleasant true green and warm amber for the calm states, pure red for alarm; the emerald
#: first tried read too blue on these strips. Red's effect is resolved by NAME ("Blink")
#: against the live catalog at write time — never a hardcoded id (103 research D4).
STATUS_LOOKS: dict[str, dict[str, Any]] = {
    "green": {"col": [[0, 210, 40]], "fx": 0, "pal": 0, "on": True},
    "yellow": {"col": [[255, 170, 0]], "fx": 0, "pal": 0, "on": True},
    # fx filled in from the catalog at write time; sx 205 ≈ 80% — a brisk, alarming blink.
    "red": {"col": [[255, 0, 0], [0, 0, 0]], "pal": 0, "sx": 205, "on": True},
    "off": {"on": False},
}
_RED_BLINK_EFFECT = "Blink"


def status_band_matches(raw: dict[str, Any], status_ids: tuple[int, ...], look: str) -> bool:
    """Whether a device state already shows ``look`` on every status segment — the pure
    drift judge the dash reconciler compares with before writing (edge-triggered, 128
    FR-006). Deliberately catalog-free: "red" matches red-and-animated (``fx != 0``), so
    judging needs no effect-catalog read on quiet cycles. Missing status segments never
    match — the reconciler must notice and decide, not assume."""

    if look not in STATUS_LOOKS:
        return False
    by_id = {
        seg.get("id"): seg
        for seg in raw.get("seg", [])
        if isinstance(seg, dict) and isinstance(seg.get("id"), int)
    }
    for sid in status_ids:
        seg = by_id.get(sid)
        if seg is None:
            return False
        seg_on = seg.get("on", True)
        if look == "off":
            if seg_on is not False:
                return False
            continue
        if seg_on is False:
            return False
        cols = seg.get("col")
        primary = cols[0] if isinstance(cols, list) and cols else None
        expected = STATUS_LOOKS[look]["col"][0]
        if not (isinstance(primary, list) and [int(c) for c in primary[:3]] == expected):
            return False
        fx = seg.get("fx", 0)
        if look == "red":
            if fx == 0:
                return False
        elif fx != 0:
            return False
    return True


def _scoped_revert_partial(held_state: dict[str, Any], main_ids: tuple[int, ...]) -> dict[str, Any]:
    """A partial that restores the held snapshot's MAIN-segment looks plus global power and
    brightness — and never names a status segment, so the bands' CURRENT state survives the
    revert by construction (128 FR-002)."""

    partial: dict[str, Any] = {}
    if isinstance(held_state.get("on"), bool):
        partial["on"] = held_state["on"]
    if isinstance(held_state.get("bri"), int):
        partial["bri"] = held_state["bri"]
    seg_partials: list[dict[str, Any]] = []
    for seg in held_state.get("seg", []):
        if not isinstance(seg, dict) or seg.get("id") not in main_ids:
            continue
        restored: dict[str, Any] = {"id": seg["id"]}
        for key in _SEGMENT_LOOK_KEYS:
            if key in seg:
                restored[key] = seg[key]
        seg_partials.append(restored)
    if seg_partials:
        partial["seg"] = seg_partials
    return partial


def _before_colors(before: WledState, main_ids: tuple[int, ...] | None = None) -> list[list[int]]:
    """The base segment's three color slots from the pre-change state, padded with black —
    the base a multi-slot color request composes over (107 research D4). With a declared
    layout (128), the base is the FIRST MAIN segment: segment 0 may be a status band whose
    colors are not part of the look being edited."""

    base_seg: dict[str, Any] | None = None
    segments = before.raw.get("seg")
    if isinstance(segments, list):
        for seg in segments:
            if not isinstance(seg, dict):
                continue
            if main_ids is not None and seg.get("id") not in main_ids:
                continue
            base_seg = seg
            break
    slots: list[list[int]] = []
    if base_seg is not None:
        cols = base_seg.get("col")
        if isinstance(cols, list):
            for entry in cols[:3]:
                if isinstance(entry, list) and len(entry) >= 3:
                    slots.append([int(entry[0]), int(entry[1]), int(entry[2])])
    while len(slots) < 3:
        slots.append([0, 0, 0])
    return slots


def _build_partial_state(
    before: WledState,
    *,
    rgb: tuple[int, int, int] | None,
    rgb2: tuple[int, int, int] | None = None,
    rgb3: tuple[int, int, int] | None = None,
    effect_id: int | None,
    palette_id: int | None = None,
    brightness_pct: int | None,
    speed: int | None = None,
    intensity: int | None = None,
    custom1: int | None = None,
    custom2: int | None = None,
    custom3: int | None = None,
    power: bool | None,
    main_ids: tuple[int, ...] | None = None,
) -> dict[str, Any]:
    """The minimal partial-state document WLED merges (103 research D2, 107 D4).

    Look changes target every segment of the current layout uniformly (the portal is run as
    one piece) — unless a segment-role layout is declared (128), in which case the uniform
    write is bounded to ``main_ids``: the status bands are the dash's, not the look's. A
    color-only request also selects effect 0 (Solid) so the color actually
    shows — a rule keyed to the PRIMARY slot only; background/tertiary colors never change
    the effect. A single-color request keeps 103's exact one-slot ``col`` shape; when a
    secondary/tertiary slot is requested, the full three-slot array is composed with
    before-state values in the unspecified slots (WLED merges ``col`` by index, so a sparse
    array cannot address slot 2 alone). Speed/intensity/custom values arrive already on the
    device scale. Any look request implies ``on`` unless the request itself is power-off.
    """

    partial: dict[str, Any] = {}
    if power is not None:
        partial["on"] = power
    if brightness_pct is not None:
        partial["bri"] = max(0, min(255, round(brightness_pct * 255 / 100)))
        partial.setdefault("on", True)

    seg_change: dict[str, Any] = {}
    if rgb2 is not None or rgb3 is not None:
        base = _before_colors(before, main_ids)
        requested_slots = (rgb, rgb2, rgb3)
        seg_change["col"] = [
            list(slot) if slot is not None else base[i] for i, slot in enumerate(requested_slots)
        ]
    elif rgb is not None:
        seg_change["col"] = [list(rgb)]
    if rgb is not None:
        seg_change["fx"] = effect_id if effect_id is not None else 0
    elif effect_id is not None:
        seg_change["fx"] = effect_id
    if palette_id is not None:
        seg_change["pal"] = palette_id
    if speed is not None:
        seg_change["sx"] = speed
    if intensity is not None:
        seg_change["ix"] = intensity
    if custom1 is not None:
        seg_change["c1"] = custom1
    if custom2 is not None:
        seg_change["c2"] = custom2
    if custom3 is not None:
        seg_change["c3"] = custom3
    if seg_change:
        partial.setdefault("on", True)
        segment_ids = [
            seg["id"]
            for seg in before.raw.get("seg", [])
            if isinstance(seg, dict)
            and isinstance(seg.get("id"), int)
            and (main_ids is None or seg["id"] in main_ids)
        ]
        partial["seg"] = [{"id": seg_id, **seg_change} for seg_id in segment_ids] or [seg_change]
    if partial.get("on") is False and "seg" not in partial and "bri" not in partial:
        pass  # a plain power-off stays exactly {"on": false}
    return partial
