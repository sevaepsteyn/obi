"""A pre-rendered ``/api/router.json`` for the static demo (feature 136, research R3).

Live, the router window's L3 tab reads a RouterOS device. The demo has no device to read, so it
answers from data the sanitized dataset already holds:

* the device header comes from the ``status.json`` snapshot record;
* the interfaces and addresses are parsed from the device's sanitized config export
  (``/interface <type> add|set`` and ``/ip address add`` lines, RouterOS terse format).

The payload is the live handler's exact success shape (``router_detail_to_dict`` plus
``store``/``hint``), marked ``"snapshot": true`` so the UI can say so. A device with no config
export gets the live unknown-device shape carrying the demo message.
"""

from __future__ import annotations

import re
import shlex
from datetime import datetime
from typing import Any

from obi.core.demo.routes import UNAVAILABLE_MESSAGE
from obi.core.models import (
    RouterAddress,
    RouterDetail,
    RouterInterface,
    RouterReadOutcome,
    RouterSummary,
)
from obi.core.serialize import router_detail_to_dict

_FIND_DEFAULT = re.compile(r"\[\s*find\s+default-name=(\S+)\s*\]")


def _pairs(rest: str) -> dict[str, str]:
    rest = _FIND_DEFAULT.sub(lambda m: f"default-name={m.group(1)}", rest)
    out: dict[str, str] = {}
    try:
        tokens = shlex.split(rest, posix=True)
    except ValueError:
        tokens = rest.split()
    for token in tokens:
        key, sep, value = token.partition("=")
        if sep:
            out[key] = value
    return out


def _flag(value: str | None) -> bool | None:
    if value is None:
        return None
    return value.lower() in ("yes", "true")


def parse_export(text: str) -> tuple[list[RouterInterface], list[RouterAddress]]:
    """Interfaces + addresses from a terse ``/export`` (one command per line)."""

    interfaces: dict[str, RouterInterface] = {}
    addresses: list[RouterAddress] = []
    for line in text.splitlines():
        if line.startswith("/interface "):
            parts = line.split(maxsplit=3)
            if len(parts) < 3 or parts[2] not in ("add", "set"):
                continue  # `bridge port`, `list member`, `wireguard peers`… are not interfaces
            kv = _pairs(parts[3] if len(parts) > 3 else "")
            name = kv.get("name") or kv.get("default-name")
            if not name:
                continue
            mtu = kv.get("mtu")
            interfaces[name] = RouterInterface(
                name=name,
                type="ether" if parts[1] == "ethernet" else parts[1],
                running=None,
                disabled=_flag(kv.get("disabled")) or False,
                mac=kv.get("mac-address"),
                comment=kv.get("comment", ""),
                mtu=int(mtu) if mtu and mtu.isdigit() else None,
            )
        elif line.startswith("/ip address add "):
            kv = _pairs(line[len("/ip address add ") :])
            if "address" in kv and "interface" in kv:
                addresses.append(
                    RouterAddress(
                        address=kv["address"],
                        interface=kv["interface"],
                        network=kv.get("network"),
                        dynamic=False,
                        disabled=_flag(kv.get("disabled")) or False,
                        comment=kv.get("comment", ""),
                    )
                )
    return sorted(interfaces.values(), key=lambda i: i.name), addresses


def router_view(
    status_record: dict[str, Any] | None, export_text: str | None, *, read_at: datetime
) -> dict[str, Any]:
    """The ``/api/router.json`` answer for one device, from the dataset alone."""

    if status_record is None or export_text is None:
        detail = RouterDetail(
            outcome=RouterReadOutcome.UNKNOWN_DEVICE, read_at=read_at, detail=UNAVAILABLE_MESSAGE
        )
        payload = router_detail_to_dict(detail)
        payload.update({"store": "absent", "hint": UNAVAILABLE_MESSAGE, "snapshot": True})
        return payload
    interfaces, addresses = parse_export(export_text)
    summary = RouterSummary(
        identity=status_record.get("identity"),
        address=str(status_record.get("address", "")),
        model=status_record.get("model"),
        version=status_record.get("version"),
        board_name=status_record.get("board_name"),
        uptime_seconds=status_record.get("uptime_seconds"),
        cpu_load_percent=status_record.get("cpu_load_percent"),
        free_memory_bytes=status_record.get("free_memory_bytes"),
        total_memory_bytes=status_record.get("total_memory_bytes"),
    )
    detail = RouterDetail(
        outcome=RouterReadOutcome.OK,
        read_at=read_at,
        device=summary,
        detail="snapshot from the demo dataset — the live view reads the device",
        interfaces=interfaces,
        addresses=addresses,
    )
    payload = router_detail_to_dict(detail)
    payload.update({"store": "ok", "hint": None, "snapshot": True})
    return payload
