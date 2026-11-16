"""The static-demo route catalogue and data-file key scheme (feature 136, contracts/bundle.md).

Every route the live web server dispatches is classified here, with how the static bundle answers
it:

* **rendered**: pre-rendered at export time by the live handler itself, one file per key;
* **filtered**: answered in the browser from a shipped slice;
* **snapshot**: a pre-rendered stand-in for a live device read;
* **unavailable**: the live error shape plus the demo message;
* **empty**: the live empty-store response.

A data file lives at ``data/api/<route>/<key>.json``. The key is ``index`` for an empty canonical
query, otherwise ``sha1(canonical)[:16]``. The canonical query keeps only the route's KEY params,
sorted and percent-encoded exactly as ``demo/api.js`` encodes them (the parity test pins both).
"""

from __future__ import annotations

import hashlib
import io
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import quote

RENDERED = "rendered"
FILTERED = "filtered"
SNAPSHOT = "snapshot"
UNAVAILABLE = "unavailable"
EMPTY = "empty"
DEMO = "demo"

UNAVAILABLE_MESSAGE = "not available in the demo — this reads the device live"


@dataclass(frozen=True)
class RouteSpec:
    path: str
    strategy: str
    key_params: tuple[str, ...] = ()


#: Every path the live server dispatches (``web/server.py`` ``_respond``). A route added there
#: must be classified here: the catalogue test fails otherwise.
ROUTES: dict[str, RouteSpec] = {
    "dash": RouteSpec("/api/dash.json", RENDERED),
    "inventory": RouteSpec("/api/inventory.json", RENDERED),
    "health": RouteSpec("/api/health.json", RENDERED, ("device",)),
    "metrics": RouteSpec("/api/metrics.json", RENDERED, ("target", "metric", "window")),
    "configs": RouteSpec(
        "/api/configs.json", RENDERED, ("device", "view", "at", "from", "to", "limit")
    ),
    "logs": RouteSpec("/api/logs.json", FILTERED),
    "audit": RouteSpec("/api/audit.json", FILTERED),
    "router": RouteSpec("/api/router.json", SNAPSHOT, ("device",)),
    "phy": RouteSpec("/api/phy.json", UNAVAILABLE),
    "tool": RouteSpec("/api/tool", UNAVAILABLE),
    "radio-clip": RouteSpec("/api/radio-clip.wav", UNAVAILABLE),
    "watch": RouteSpec("/api/watch.json", EMPTY),
    "radio": RouteSpec("/api/radio.json", EMPTY),
    "voice": RouteSpec("/voice.json", DEMO),
    "turn": RouteSpec("/api/turn", DEMO),
    "healthz": RouteSpec("/healthz", EMPTY),
}

#: The metric windows the MET picker offers (``widgets/met.js``): each is pre-rendered.
METRIC_WINDOWS: tuple[str, ...] = ("1h", "6h", "24h", "7d")


def _encode(value: str) -> str:
    # Percent-encode everything but unreserved characters — the same set demo/api.js produces
    # from encodeURIComponent once it additionally escapes !'()*.
    return quote(value, safe="-_.~")


def canonical_query(route: str, params: Mapping[str, str | None]) -> str:
    """The route's key params present in ``params``, sorted, as ``k=v&k=v`` (encoded)."""

    spec = ROUTES[route]
    pairs = sorted((k, str(v)) for k, v in params.items() if k in spec.key_params and v)
    return "&".join(f"{_encode(k)}={_encode(v)}" for k, v in pairs)


def route_key(route: str, params: Mapping[str, str | None]) -> str:
    canonical = canonical_query(route, params)
    if not canonical:
        return "index"
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:16]


def data_path(route: str, params: Mapping[str, str | None]) -> str:
    """The bundle-relative file for a rendered/snapshot response."""

    return f"data/api/{route}/{route_key(route, params)}.json"


# --- calling a live handler in-process ----------------------------------------------------------


class _Capture:
    """Stands in for ``BaseHTTPRequestHandler``: the handlers only use these four members."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.wfile = io.BytesIO()
        self.status: int | None = None
        self.headers: dict[str, str] = {}

    def send_response(self, code: int) -> None:
        self.status = code

    def send_header(self, key: str, value: str) -> None:
        self.headers[key] = value

    def end_headers(self) -> None:
        return None


def capture(
    handler_fn: Callable[..., None], path: str, *args: object, **kwargs: object
) -> tuple[int, Any]:
    """Run one live ``data_api`` handler for ``path``; return ``(status, parsed JSON)``."""

    fake = _Capture(path)
    handler_fn(cast(Any, fake), *args, **kwargs)
    body = fake.wfile.getvalue()
    return fake.status or 500, json.loads(body) if body else None
