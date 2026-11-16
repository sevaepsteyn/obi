"""The demo profile: the ``[demo]`` section of ``config.toml`` (feature 134, data-model.md).

Parsed straight from the raw TOML table so the demo stays out of the main ``Config`` dataclass
(its only consumer is ``obi demo``). Validation is fail-closed: anything that could put private
material inside a dataset, or publish under a mistaken window, is a :class:`ProfileError`.
The destination anchor and rotation are PRIVATE — never written to the manifest.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from obi.core.demo import ProfileError

DEFAULT_WINDOW_HOURS = 48
MAX_SPAN = timedelta(days=14)
# USGS 3DEP CONUS bounds (the terrain tiles, EPQS and GNIS sources all need US coverage).
_CONUS = (24.0, 50.0, -125.0, -66.0)


def _state_root() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(Path.home(), ".local", "state")
    return Path(base) / "obi"


def _cache_root() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(Path.home(), ".cache")
    return Path(base) / "obi"


@dataclass(frozen=True)
class DemoProfile:
    output_dir: Path
    mapping_path: Path
    denylist_path: Path
    terrain_cache_dir: Path
    state_dir: Path
    since: datetime | None = None
    until: datetime | None = None
    window_hours: int = DEFAULT_WINDOW_HOURS
    metrics_decimate: int = 6
    audit_command_sample: int = 10
    max_dataset_mb: int = 200
    destination_lat: float = 37.16
    destination_lon: float = -118.27
    rotation_deg: float = 0.0
    epoch_start: date = date(2025, 6, 2)
    gnis_state: str = "CA"
    timezone: str = "America/Los_Angeles"
    region_label: str = "Owens Valley, CA"
    strict_event_terms: bool = False
    audit_payload_max_bytes: int = 4096
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def _parse_dt(value: Any, label: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError as exc:
            raise ProfileError(f"[demo] {label} is not an ISO datetime: {value!r}") from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def load_profile(
    raw: dict[str, Any],
    *,
    config_path: Path,
    overrides: dict[str, Any] | None = None,
    require_denylist: bool = True,
) -> DemoProfile:
    """Build and validate the profile from the raw ``[demo]`` table plus CLI ``overrides``."""

    table: dict[str, Any] = dict(raw.get("demo", {}) or {})
    table.update({k: v for k, v in (overrides or {}).items() if v is not None})
    state_dir = _state_root()
    config_dir = config_path.expanduser().resolve().parent

    def path_of(key: str, default: Path) -> Path:
        value = table.get(key)
        return Path(str(value)).expanduser() if value is not None else default

    output_dir = path_of("output_dir", Path.cwd() / "demo-dataset")
    mapping_path = path_of("mapping_path", state_dir / "demo" / "mapping.json")
    denylist_path = path_of("denylist_path", config_dir / "demo-denylist.txt")
    terrain_cache_dir = path_of("terrain_cache_dir", _cache_root() / "demo-terrain")

    for name, root in (("state", state_dir), ("config", config_dir)):
        if _inside(output_dir, root):
            raise ProfileError(f"[demo] output_dir must not be inside obi's {name} directory")
    for name, private in (("mapping_path", mapping_path), ("denylist_path", denylist_path)):
        if _inside(private, output_dir):
            raise ProfileError(f"[demo] {name} must not be inside output_dir")
    if _inside(terrain_cache_dir, state_dir):
        raise ProfileError("[demo] terrain_cache_dir must not be inside the live state directory")

    if require_denylist:
        if not denylist_path.exists():
            raise ProfileError(
                f"private denylist not found: {denylist_path} (create it, chmod 600)"
            )
        if stat.S_IMODE(denylist_path.stat().st_mode) & 0o077:
            raise ProfileError(f"private denylist {denylist_path} must be owner-only (chmod 600)")

    since = _parse_dt(table["since"], "since") if "since" in table else None
    until = _parse_dt(table["until"], "until") if "until" in table else None
    if (since is None) != (until is None):
        raise ProfileError("[demo] since and until must be given together")
    if since is not None and until is not None:
        if until <= since:
            raise ProfileError("[demo] until must be after since")
        if until - since > MAX_SPAN:
            raise ProfileError("[demo] the window may span at most 14 days")

    epoch_raw = table.get("epoch_start", "2025-06-02")
    try:
        epoch_start = epoch_raw if isinstance(epoch_raw, date) else date.fromisoformat(epoch_raw)
    except ValueError as exc:
        raise ProfileError(f"[demo] epoch_start is not a date: {epoch_raw!r}") from exc
    if epoch_start.weekday() != 0:
        raise ProfileError("[demo] epoch_start must be a Monday")

    timezone = str(table.get("timezone", "America/Los_Angeles"))
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ProfileError(f"[demo] unknown timezone {timezone!r}") from exc

    lat = float(table.get("destination_lat", 37.16))
    lon = float(table.get("destination_lon", -118.27))
    if not (_CONUS[0] <= lat <= _CONUS[1] and _CONUS[2] <= lon <= _CONUS[3]):
        raise ProfileError("[demo] destination must lie inside US 3DEP (CONUS) coverage")
    rotation = float(table.get("rotation_deg", 0.0))
    if not 0.0 <= rotation < 360.0:
        raise ProfileError("[demo] rotation_deg must be in [0, 360)")

    decimate = int(table.get("metrics_decimate", 6))
    command_sample = int(table.get("audit_command_sample", 10))
    budget = int(table.get("max_dataset_mb", 200))
    if decimate < 1 or command_sample < 1 or budget <= 0:
        raise ProfileError(
            "[demo] metrics_decimate and audit_command_sample must be ≥ 1, max_dataset_mb > 0"
        )

    return DemoProfile(
        output_dir=output_dir,
        mapping_path=mapping_path,
        denylist_path=denylist_path,
        terrain_cache_dir=terrain_cache_dir,
        state_dir=state_dir,
        since=since,
        until=until,
        window_hours=int(table.get("window_hours", DEFAULT_WINDOW_HOURS)),
        metrics_decimate=decimate,
        audit_command_sample=command_sample,
        max_dataset_mb=budget,
        destination_lat=lat,
        destination_lon=lon,
        rotation_deg=rotation,
        epoch_start=epoch_start,
        gnis_state=str(table.get("gnis_state", "CA")).upper(),
        timezone=timezone,
        region_label=str(table.get("region_label", "Owens Valley, CA")),
        strict_event_terms=bool(table.get("strict_event_terms", False)),
        audit_payload_max_bytes=int(table.get("audit_payload_max_bytes", 4096)),
    )
