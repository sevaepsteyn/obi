"""Live-continuity parity (feature 137, US2, research D9).

Moving identity out of source must not change what the live host does once its config carries
the moved values. :func:`render_surfaces` renders every surface those values reach — prompts,
every LLM tool list with descriptions, the radio STT vocabulary, and the effective value of each
moved setting — as plain text. It is pure: no network, device or store I/O.

It deliberately works against BOTH the pre-137 code (builders without a ``config`` parameter,
event defaults resolved by ``config.weather or WeatherSettings()``) and the post-137 code, so one
snapshot recorded before any runtime change can be compared with one recorded after.

Snapshots hold REAL values (the operator's own config rendered): they are written owner-only
under ``$XDG_STATE_HOME/obi/identity/parity/`` and diffs are printed to the operator's terminal
only — never logged or audited.
"""

from __future__ import annotations

import difflib
import inspect
import json
import os
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from obi.core.config import Config

_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

#: The moved settings (contracts/config-keys.md), as dotted attribute paths on ``Config``.
#: ``None`` sections render as ``<unset>``; weather/traffic fall back to their dataclass defaults
#: when the section is absent ONLY if the code still has such defaults (pre-137 behaviour).
SETTING_PATHS: tuple[str, ...] = (
    "weather.latitude",
    "weather.longitude",
    "weather.nws_zone",
    "traffic.base_url",
    "paging.url",
    "dash.ping_router",
    "snmp.history.targets",
    "health_linux.username",
    "radio.ftdi_serial",
    "radio.pronunciations",
    "radio.speech_aliases",
    "web.default_ground_elevation",
    "web.brc",
    "paging.slack.recipients",
    "meshtastic.names",
    "meshtastic.aliases",
)


def _call(fn: Callable[..., Any], config: Config, *args: Any) -> Any:
    """Call a builder with ``config`` when its signature (post-137) accepts one."""

    params = inspect.signature(fn).parameters
    if "config" in params:
        return fn(*args, config=config)
    return fn(*args)


def _tools_text(tools: Iterable[Any]) -> str:
    rows = []
    for tool in tools:
        name = getattr(tool, "name", "?")
        description = getattr(tool, "description", "") or ""
        rows.append(f"## {name}\n{description.strip()}")
    return "\n\n".join(sorted(rows))


def _default_section(name: str) -> Any:
    """The pre-137 implicit default for an absent [weather]/[traffic] section, if constructible."""

    from obi.core import config as cfg

    cls = {"weather": "WeatherSettings", "traffic": "TrafficSettings"}.get(name)
    if cls is None or not hasattr(cfg, cls):
        return None
    try:
        return getattr(cfg, cls)()
    except TypeError:  # post-137: required fields, no implicit default
        return None


def _resolve(config: Config, dotted: str) -> Any:
    head, *rest = dotted.split(".")
    obj: Any = getattr(config, head, None)
    if obj is None:
        obj = _default_section(head)
    for part in rest:
        if obj is None:
            return None
        obj = getattr(obj, part, None)
    return obj


def _render_value(value: Any) -> str:
    if value is None:
        return "<unset>"
    if isinstance(value, dict):
        return json.dumps(dict(sorted(value.items())), sort_keys=True)
    if isinstance(value, list | tuple):
        return json.dumps(list(value))
    return repr(value)


def _guard(fn: Callable[[], str]) -> str:
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - a broken surface is itself a parity result
        return f"<render error: {type(exc).__name__}>"


def _chat_prompt(config: Config) -> str:
    from obi.core import prompt

    build = getattr(prompt, "build_system_prompt", None)
    if build is not None:
        return str(build(config))
    return str(prompt.system_prompt(None))  # pre-137: one static prompt


def _radio_prompt(config: Config) -> str:
    from obi.core import prompt

    radio = config.radio
    if radio is None:
        return "<radio not configured>"
    addendum: Callable[..., Any] = prompt.radio_system_addendum
    kwargs: dict[str, Any] = {
        "vocab_terms": radio.vocab_terms,
        "callsigns": [*radio.operator_handles, *radio.known_callers],
    }
    if "config" in inspect.signature(addendum).parameters:
        kwargs["config"] = config
    return str(addendum(**kwargs))


def _mcp_instructions(config: Config) -> str:
    from obi.mcp import server

    build = getattr(server, "build_instructions", None)
    if build is not None:
        return str(build(config))
    return str(getattr(server, "_INSTRUCTIONS", ""))


def _tools(kind: str, config: Config) -> str:
    from obi.core import toolset

    if kind == "chat":
        return _tools_text(_call(toolset.build_chat_tools, config))
    if kind == "watch":
        return _tools_text(_call(toolset.build_watch_tools, config))
    if kind in ("radio.safe", "radio.operator"):
        tier = kind.split(".")[1]
        return _tools_text(_call(toolset.build_radio_tools, config, tier))
    from obi.mcp import server

    return _tools_text(_call(server.served_tools, config))


def _radio_vocab(config: Config) -> str:
    if config.radio is None:
        return "<radio not configured>"
    from obi.radio.runner import build_vocab_prompt

    return str(build_vocab_prompt(config.radio))


def render_surfaces(config: Config) -> dict[str, str]:
    """Every identity-bearing surface as text (pure; see module docstring)."""

    from obi.core import prompt

    surfaces: dict[str, Callable[[], str]] = {
        "prompt.chat": lambda: _chat_prompt(config),
        "prompt.voice": lambda: str(prompt.VOICE_SYSTEM_ADDENDUM),
        "prompt.radio": lambda: _radio_prompt(config),
        "prompt.watch": lambda: str(prompt.watch_system_prompt()),
        "prompt.mcp": lambda: _mcp_instructions(config),
        "tools.chat": lambda: _tools("chat", config),
        "tools.mcp": lambda: _tools("mcp", config),
        "tools.watch": lambda: _tools("watch", config),
        "tools.radio.safe": lambda: _tools("radio.safe", config),
        "tools.radio.operator": lambda: _tools("radio.operator", config),
        "radio.vocab": lambda: _radio_vocab(config),
        "settings": lambda: "\n".join(
            f"{path} = {_render_value(_resolve(config, path))}" for path in SETTING_PATHS
        ),
    }
    return {name: _guard(fn) for name, fn in surfaces.items()}


# -- snapshots ------------------------------------------------------------------------------------


def parity_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(Path.home(), ".local", "state")
    return Path(base) / "obi" / "identity" / "parity"


def snapshot_path(label: str) -> Path:
    if not _LABEL.match(label):
        raise ValueError("a snapshot label is letters, digits, '.', '_' or '-' (max 64)")
    return parity_dir() / f"{label}.json"


def record(config: Config, label: str) -> Path:
    """Render and write the snapshot owner-only (dir 0700, file 0600)."""

    path = snapshot_path(label)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({"version": 1, "surfaces": render_surfaces(config)}, handle, indent=1)
    os.replace(tmp, path)
    return path


def load(label: str) -> dict[str, str]:
    data = json.loads(snapshot_path(label).read_text(encoding="utf-8"))
    return dict(data["surfaces"])


#: Surfaces whose TEXT 137 deliberately rewords (placeholders, neutral wording, config-rendered
#: names). A difference there is "intended" — still shown in full for the operator to review —
#: unless it changes BEHAVIOUR: for tool surfaces, the set of tool names offered is compared
#: strictly. Settings and the radio STT vocabulary are always strict.
WORDING_SURFACES: tuple[str, ...] = ("prompt.", "tools.")

#: Kept for callers/tests that pass explicit (prefix, regex) rules.
INTENDED_DIFFS: tuple[tuple[str, str], ...] = ()


def tool_names(text: str) -> set[str]:
    return {line[3:].strip() for line in text.splitlines() if line.startswith("## ")}


@dataclass
class SurfaceDiff:
    surface: str
    status: str  # "equal" | "intended" | "different" | "missing"
    diff: list[str] = field(default_factory=list)


def _changed_lines(diff: list[str]) -> list[str]:
    return [line[1:] for line in diff if line[:1] in "+-" and not line.startswith(("+++", "---"))]


def compare(
    a: dict[str, str],
    b: dict[str, str],
    *,
    intended: Iterable[tuple[str, str]] = INTENDED_DIFFS,
    wording: bool = True,
) -> list[SurfaceDiff]:
    rules = [(prefix, re.compile(rx)) for prefix, rx in intended]
    out: list[SurfaceDiff] = []
    for surface in sorted(set(a) | set(b)):
        if surface not in a or surface not in b:
            out.append(SurfaceDiff(surface, "missing"))
            continue
        if a[surface] == b[surface]:
            out.append(SurfaceDiff(surface, "equal"))
            continue
        diff = list(
            difflib.unified_diff(
                a[surface].splitlines(), b[surface].splitlines(), "before", "after", lineterm=""
            )
        )
        applicable = [rx for prefix, rx in rules if surface.startswith(prefix)]
        changed = [line for line in _changed_lines(diff) if line.strip()]
        intended_only = bool(applicable) and all(
            any(rx.search(line) for rx in applicable) for line in changed
        )
        if wording and surface.startswith(WORDING_SURFACES):
            same_tools = not surface.startswith("tools.") or tool_names(a[surface]) == tool_names(
                b[surface]
            )
            intended_only = intended_only or same_tools
        out.append(SurfaceDiff(surface, "intended" if intended_only else "different", diff))
    return out
