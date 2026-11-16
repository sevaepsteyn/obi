"""The values feature 137 moved out of source into config (contracts/config-keys.md).

One :class:`MovedValue` per config key that replaced a built-in, deployment-specific default.
``was`` DESCRIBES the old default — it never states it (the old value was the real deployment).
``obi identity migrate-check`` walks this registry against a live config; a unit test keeps it
in lock-step with the contract table and with ``config.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class MovedValue:
    key: str  # dotted TOML path (``section.sub.key``)
    feature: str
    category: str  # identity category of the old default
    was: str  # a DESCRIPTION of the old built-in default — never the value
    unset_behaviour: str
    #: The section whose presence makes the key matter; ``None`` ⇒ always relevant.
    section: str | None
    #: True when a present section without this key is a load error (so "unset" is impossible
    #: for a present section; migrate-check reports the section itself instead).
    required: bool = False
    toml: str = ""  # snippet with a <value> placeholder
    #: The old built-in default applied even with NO section (weather/traffic/city grid ran on
    #: event defaults): absent then means behaviour CHANGES on migration, so it blocks.
    implicit: bool = False


MOVED: tuple[MovedValue, ...] = (
    MovedValue(
        "weather.latitude",
        "weather",
        "coord",
        "event-site coordinate",
        "weather off; weather tools hidden",
        "weather",
        True,
        "[weather]\nlatitude = <value>",
        implicit=True,
    ),
    MovedValue(
        "weather.longitude",
        "weather",
        "coord",
        "event-site coordinate",
        "weather off; weather tools hidden",
        "weather",
        True,
        "[weather]\nlongitude = <value>",
        implicit=True,
    ),
    MovedValue(
        "weather.nws_zone",
        "weather",
        "endpoint",
        "event-region NWS zone",
        "no NWS alerts fetched",
        "weather",
        False,
        '[weather]\nnws_zone = "<value>"',
        implicit=True,
    ),
    MovedValue(
        "weather.place_name",
        "weather",
        "place",
        "the event city's name in prompt text",
        'prompts say "the configured location"',
        "weather",
        False,
        '[weather]\nplace_name = "<value>"',
    ),
    MovedValue(
        "traffic.base_url",
        "gate traffic",
        "endpoint",
        "event dashboard URL",
        "traffic off; gate_traffic hidden",
        "traffic",
        True,
        '[traffic]\nbase_url = "<value>"',
        implicit=True,
    ),
    MovedValue(
        "paging.url",
        "paging",
        "endpoint",
        "event paging endpoint",
        "[paging] fails to load (required)",
        "paging",
        True,
        '[paging]\nurl = "<value>"',
        implicit=True,
    ),
    MovedValue(
        "playa.base_url",
        "camp/art locate fetch",
        "endpoint",
        "event public API URL",
        "`obi playa fetch` needs --base-url",
        None,
        False,
        '[playa]\nbase_url = "<value>"',
        implicit=True,
    ),
    MovedValue(
        "brc.origin_lat",
        "city grid + addresses",
        "coord",
        "city-plan origin constant",
        "city grid off; addresses fall back to coordinates; playa_locate hidden",
        None,
        False,
        "[brc]\norigin_lat = <value>",
        implicit=True,
    ),
    MovedValue(
        "brc.origin_lon",
        "city grid + addresses",
        "coord",
        "city-plan origin constant",
        "city grid off; addresses fall back to coordinates; playa_locate hidden",
        None,
        False,
        "[brc]\norigin_lon = <value>",
        implicit=True,
    ),
    MovedValue(
        "brc.year",
        "city grid + addresses",
        "endpoint",
        "city-plan edition constant",
        "city grid off",
        None,
        False,
        "[brc]\nyear = <value>",
        implicit=True,
    ),
    MovedValue(
        "brc.source_url",
        "city grid fetch",
        "endpoint",
        "GIS repository URL constant",
        "the local cache only (no fetch)",
        "brc",
        False,
        '[brc]\nsource_url = "<value>"',
        implicit=True,
    ),
    MovedValue(
        "brc.az12_deg",
        "city addresses",
        "coord",
        "fallback 12:00 azimuth constant",
        "45.0",
        "brc",
        False,
        "[brc]\naz12_deg = <value>",
    ),
    MovedValue(
        "web.default_ground_elevation",
        "web export",
        "coord",
        "event-site elevation",
        "0.0 m",
        None,
        False,
        "[web]\ndefault_ground_elevation = <value>",
        implicit=True,
    ),
    MovedValue(
        "web.ospf_sources",
        "OSPF map",
        "device",
        "two real router names",
        "roster order",
        None,
        False,
        '[web]\nospf_sources = ["<value>"]',
        implicit=True,
    ),
    MovedValue(
        "dash.ping_router",
        "dash PING tile",
        "device",
        "a real device",
        'PING tile "not configured"',
        "dash",
        False,
        '[dash]\nping_router = "<value>"',
        implicit=True,
    ),
    MovedValue(
        "snmp.history.targets",
        "metrics history",
        "device",
        "the real name-suffix convention",
        "every identified device",
        "snmp.history",
        False,
        '[snmp.history]\ntargets = "<value>"',
        implicit=True,
    ),
    MovedValue(
        "health.linux.username",
        "Linux health probe",
        "person",
        "the event team's SSH user",
        "[health.linux] fails to load (required with hosts)",
        "health.linux",
        True,
        '[health.linux]\nusername = "<value>"',
        implicit=True,
    ),
    MovedValue(
        "radio.ftdi_serial",
        "radio",
        "serial",
        "the real PTT cable serial",
        "[radio] needs ftdi_serial or serial_port",
        "radio",
        False,
        '[radio]\nftdi_serial = "<value>"',
        implicit=True,
    ),
    MovedValue(
        "radio.pronunciations",
        "radio",
        "place",
        "a site-name pronunciation pair",
        "no pronunciation guide",
        "radio",
        False,
        '[radio.pronunciations]\n<TERM> = "<value>"',
        implicit=True,
    ),
    MovedValue(
        "radio.speech_aliases",
        "radio",
        "place",
        "a site-name mis-hearing alias",
        "no inbound alias",
        "radio",
        False,
        '[radio.speech_aliases]\n<heard> = "<value>"',
        implicit=True,
    ),
    MovedValue(
        "radio.channel_name",
        "radio persona",
        "endpoint",
        "the event's radio channel",
        '"the operations radio channel"',
        "radio",
        False,
        '[radio]\nchannel_name = "<value>"',
        implicit=True,
    ),
    MovedValue(
        "site.timezone",
        "local times",
        "coord",
        "the event's timezone",
        "UTC",
        None,
        False,
        '[site]\ntimezone = "<value>"',
        implicit=True,
    ),
    MovedValue(
        "victoria.display_name",
        "external metrics tools",
        "person",
        "the external team's name in tool text",
        '"the external metrics store"',
        "victoria",
        False,
        '[victoria]\ndisplay_name = "<value>"',
        implicit=True,
    ),
    MovedValue(
        "victoria.rf_matcher",
        "backbone_rf_health",
        "device",
        "the RF label matcher",
        "the role=ptp-rf matcher",
        "victoria",
        False,
        "[victoria]\nrf_matcher = '<value>'",
        implicit=True,
    ),
    MovedValue(
        "wled.descriptions.*",
        "lighting text",
        "place",
        "the lighting device's tower description in prompt/tool text",
        "device names only",
        "wled",
        False,
        '[wled.descriptions]\n<name> = "<value>"',
    ),
)


#: Sections whose feature ran on built-in defaults even when the section was absent.
_IMPLICIT_SECTIONS = frozenset({"weather", "traffic", "brc", "playa", "site", "web"})


def _lookup(raw: Any, dotted: str) -> Any:
    """``dotted`` in the raw TOML; ``*`` matches any table key (first hit wins)."""

    parts = dotted.split(".")
    nodes = [raw]
    for part in parts:
        nxt: list[Any] = []
        for node in nodes:
            if not isinstance(node, dict):
                continue
            if part == "*":
                nxt.extend(node.values())
            elif part in node:
                nxt.append(node[part])
        nodes = nxt
    return next((n for n in nodes if n not in (None, "", [], {})), None)


def _exists(raw: Any, dotted: str) -> bool:
    node = raw
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


@dataclass(frozen=True)
class MigrationRow:
    value: MovedValue
    state: str  # "SET" | "UNSET" | "SECTION ABSENT"

    @property
    def blocking(self) -> bool:
        """The old built-in default would have applied here and nothing replaces it now, so
        behaviour changes silently on migration. Purely new, optional keys never block."""

        if self.state == "SET" or not self.value.implicit:
            return False
        top = self.value.key.split(".")[0]
        return self.state == "UNSET" or top in _IMPLICIT_SECTIONS


def migration_rows(raw: dict[str, Any]) -> list[MigrationRow]:
    """Each moved value's state in the RAW config TOML — never its value.

    Reads the file's own keys (not the parsed Config), so a key the operator set explicitly
    is told apart from a neutral default the parser filled in."""

    rows = []
    for moved in MOVED:
        section = moved.section or moved.key.rsplit(".", 1)[0].split(".*")[0]
        present = _exists(raw, section)
        if _lookup(raw, moved.key) is not None:
            state = "SET"
        elif present:
            state = "UNSET"
        else:
            state = "SECTION ABSENT"
        rows.append(MigrationRow(moved, state))
    return rows
