"""The unified LAN HTTPS server (feature 022 as the AR server; unified by 033).

One origin serves everything browser-facing: the static viewer assets (the landing page
that selects the 2D or 3D version, both viewer modules, the vendored three.js), the current
geometry export, and — when voice is mounted — the 024 voice turn protocol. It is stdlib-only,
read-only, and allowlisted: it never contacts the controller or any device (refreshing data
is ``obi web export``), and any path outside the allowlist is a 404.

HTTPS is not optional: WebXR exists only in secure contexts (a plain ``http://<lan-ip>``
origin has no ``navigator.xr`` at all — 022 research §7), and browsers grant microphone
capture only to secure origins (024). A self-signed certificate is generated **once** into
obi's web state dir by shelling out to the ``openssl`` CLI (macOS/Linux ship it; no new
Python dependency); the operator accepts the browser warning once per device.

The voice turn transport (``POST /api/turn``) lives here too (absorbed from the retired
standalone talk server, 024→033): multipart validation and the NDJSON event stream, in
front of the transport-agnostic engine in :mod:`obi.voice.web.turns`.
"""

from __future__ import annotations

import contextlib
import email
import email.policy
import json
import logging
import socket
import ssl
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # the voice engine pulls the chat stack — keep the static server light
    from obi.core.audit import AuditRecorder
    from obi.core.config import (
        AuditSettings,
        Config,
        DashSettings,
        LogsSettings,
        RadioSettings,
        SnmpHistorySettings,
    )
    from obi.voice.web.turns import VoiceWebApp

logger = logging.getLogger("obi")

# 90 s of 16 kHz PCM16 ≈ 2.9 MB; 4 MB leaves headroom for the multipart envelope (024 FR-015).
MAX_BODY_BYTES = 4 * 1024 * 1024

# Typed questions (033): same order of magnitude as a spoken one — a cap, not a limit anyone
# hits typing by hand.
MAX_TEXT_CHARS = 2000

# 056 R6: the optional right-click context part. The client already truncates its chip to this
# bound (what you see is what is sent); the server enforces it so a hand-rolled request can't
# smuggle a larger block past the operator's view. Far inside MAX_BODY_BYTES.
MAX_CONTEXT_CHARS = 4000

STATIC_DIR = Path(__file__).parent / "static"

# The COMPLETE set of paths the server will answer (FR-012): URL path → (file, content-type).
# Everything else — traversal attempts included — is a 404; there is no directory listing.
_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    # 037: the workspace theme is the one served stylesheet (tokens + shell + widget styles).
    ".css": "text/css; charset=utf-8",
    # 055: the terrain hillshade underlay raster, served beside terrain.json.
    ".png": "image/png",
}


def _allowlist(
    static_dir: Path, export_path: Path, snapshots_dir: Path | None = None
) -> dict[str, Path]:
    routes = {
        "/": static_dir / "index.html",
        "/index.html": static_dir / "index.html",
        "/viewer.js": static_dir / "viewer.js",
        # 080: the pure stick-nav math viewer.js imports. New entries here are startup-
        # fixed — shipping one means a server restart, not just a reload.
        "/nav.js": static_dir / "nav.js",
        # 033: the 2D version's module — the landing picks which viewer a browser loads.
        "/viewer2d.js": static_dir / "viewer2d.js",
        "/vendor/three.module.min.js": static_dir / "vendor" / "three.module.min.js",
        "/links.json": export_path,
        # 031: the static terrain artifact, written by the export beside links.json;
        # fetched once per viewer session (never polled). Missing file ⇒ 404 ⇒ 029 view.
        "/terrain.json": export_path.with_name("terrain.json"),
        # 055: the terrain hillshade underlay raster (only the .png is served; the sidecar
        # terrain-shade.meta.json is regeneration-policy input and stays unserved). Missing
        # file ⇒ 404 ⇒ the widget offers no terrain toggle.
        "/terrain-shade.png": export_path.with_name("terrain-shade.png"),
        # 098: the BRC city-grid artifact (written by the export beside links.json; fetched
        # once per viewer session, terrain.json-style) + the pure geometry module viewer.js
        # imports. Missing artifact ⇒ 404 ⇒ no city layer, no panel row. Startup-fixed like
        # every entry here — shipping these meant ONE server restart at deploy.
        "/brc.json": export_path.with_name("brc.json"),
        "/brc.js": static_dir / "brc.js",
        # 032: the viewer's voice module (capture/turn/playback). Static like the rest;
        # whether voice is actually ON is /voice.json, not this file's presence.
        "/voice.js": static_dir / "voice.js",
        # 037: the terminal workspace — shell, window manager, widget catalog, theme, and
        # one module per widget (specs/037-web-workspace/contracts/workspace-urls.md).
        "/theme.css": static_dir / "theme.css",
        "/shell.js": static_dir / "shell.js",
        "/wm.js": static_dir / "wm.js",
        "/registry.js": static_dir / "registry.js",
        "/widgets/map.js": static_dir / "widgets" / "map.js",
        "/widgets/talk.js": static_dir / "widgets" / "talk.js",
        "/widgets/xr.js": static_dir / "widgets" / "xr.js",
        # 038: the widget kit + the first data widget (its data route is /api/logs.json,
        # dispatched explicitly — see data_api.py).
        "/kit.js": static_dir / "kit.js",
        "/widgets/logs.js": static_dir / "widgets" / "logs.js",
        # 040: the shared board driver + one module per board widget.
        "/boards.js": static_dir / "boards.js",
        "/widgets/uisp.js": static_dir / "widgets" / "uisp.js",
        # 047: the RouterOS fleet board (renamed from stat) + the per-router live-detail widget.
        "/widgets/ros.js": static_dir / "widgets" / "ros.js",
        "/widgets/router.js": static_dir / "widgets" / "router.js",
        # 075: the router window's PHY tab (lazy-imported by router.js) + the curated faceplate
        # layout data module it imports (the bands.js static-vocabulary precedent). Its data route
        # is /api/phy.json (dispatched above).
        "/widgets/phy.js": static_dir / "widgets" / "phy.js",
        "/faceplates.js": static_dir / "faceplates.js",
        "/widgets/sik.js": static_dir / "widgets" / "sik.js",
        "/widgets/unifi.js": static_dir / "widgets" / "unifi.js",
        "/widgets/nbx.js": static_dir / "widgets" / "nbx.js",
        "/widgets/watch.js": static_dir / "widgets" / "watch.js",
        # 041: the chart primitive + the two bespoke-UI widgets.
        "/chart.js": static_dir / "chart.js",
        "/widgets/met.js": static_dir / "widgets" / "met.js",
        "/widgets/cfg.js": static_dir / "widgets" / "cfg.js",
        "/widgets/hlt.js": static_dir / "widgets" / "hlt.js",
        # 096: the router-window HEALTH tab pane (lazily imported by router.js).
        "/widgets/rhealth.js": static_dir / "widgets" / "rhealth.js",
        # 051: the per-device config-history window (opened from a CFG row or `/conf <device>`).
        "/widgets/conf.js": static_dir / "widgets" / "conf.js",
        # 042: the audit-trail inspection board (its data route is /api/audit.json).
        "/widgets/audit.js": static_dir / "widgets" / "audit.js",
        # 088: the walkie-talkie channel transcript board (its data route is /api/radio.json).
        "/widgets/radio.js": static_dir / "widgets" / "radio.js",
        # 043: the unified backhaul board (merges the uisp-links + siklu snapshots client-side).
        "/widgets/links.js": static_dir / "widgets" / "links.js",
        # 044: the shared band vocabulary both viewer surfaces import (toggles, grouping).
        "/bands.js": static_dir / "bands.js",
        # 048: the two net-tool widgets + the shared form helper they import — each POSTs
        # /api/tool (the web tool bridge) to run obi's routeros_ping/routeros_traceroute tools.
        "/nettool.js": static_dir / "nettool.js",
        "/widgets/ping.js": static_dir / "widgets" / "ping.js",
        "/widgets/trace.js": static_dir / "widgets" / "trace.js",
        # 052: the OSPF topology map + its per-router drill-down window (both poll
        # /snapshots/ospf.json; no live device route).
        "/widgets/ospf.js": static_dir / "widgets" / "ospf.js",
        "/widgets/ospf_router.js": static_dir / "widgets" / "ospf_router.js",
        # 058: the per-link detail window (opened from a MAP link tap or a LINKS row click;
        # reads links.json + terrain.json, filtered to one link — no live device route).
        "/widgets/link.js": static_dir / "widgets" / "link.js",
        # 076: the ephemeral snapshot window — mounts an inert DOM clone of a popped-out panel body
        # (no fetch, no store). The ONE new served file this feature adds (⇒ one restart at deploy).
        "/widgets/snap.js": static_dir / "widgets" / "snap.js",
        # 095: the status dashboard board (its data route is /api/dash.json — the persisted
        # board.json, never a device read). The ONE new served file this feature adds.
        "/widgets/dash.js": static_dir / "widgets" / "dash.js",
    }
    if snapshots_dir is not None:
        # 040 camp 2: cron-written per-tool snapshots (obi web snapshot). The name set is
        # parity-tested against core.snapshot.SNAPSHOT_TOOL_NAMES — hardcoded here so the
        # static server never imports the snapshot (engine) module.
        for name in ("status", "uisp", "uisp-links", "siklu", "unifi", "netbox-drift", "ospf"):
            routes[f"/snapshots/{name}.json"] = snapshots_dir / f"{name}.json"
    return routes


def ensure_certificate(state_dir: Path, *, openssl: str = "openssl") -> tuple[Path, Path]:
    """Generate (once) and return the self-signed cert + key under ``state_dir``.

    Reused on every later serve; regeneration = delete the two files. Raises
    :class:`RuntimeError` with a clear message if the ``openssl`` CLI is unavailable/fails.
    """

    cert = state_dir / "ar-server.crt"
    key = state_dir / "ar-server.key"
    if cert.exists() and key.exists():
        return cert, key
    state_dir.mkdir(parents=True, exist_ok=True)
    command = [
        openssl,
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-days",
        "3650",
        "-subj",
        "/CN=obi-ar",
        "-keyout",
        str(key),
        "-out",
        str(cert),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, timeout=60)
    except FileNotFoundError as exc:
        raise RuntimeError(
            "the 'openssl' CLI is required to generate the web server's self-signed "
            "certificate (WebXR needs HTTPS) but was not found on PATH"
        ) from exc
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"openssl certificate generation failed: {exc}") from exc
    with contextlib.suppress(OSError):
        key.chmod(0o600)
    return cert, key


# --- the voice turn transport (024, absorbed from the standalone talk server by 033) --------


def _send_json(
    handler: BaseHTTPRequestHandler, status: int, payload: dict[str, object]
) -> None:
    body = json.dumps(payload).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _multipart_parts(handler: BaseHTTPRequestHandler, body: bytes) -> dict[str, bytes] | None:
    """The named multipart/form-data parts, or ``None`` when the body isn't multipart."""

    content_type = handler.headers.get("Content-Type", "")
    if not content_type.startswith("multipart/form-data"):
        return None
    prefix = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode()
    message = email.message_from_bytes(prefix + body, policy=email.policy.HTTP)
    if not message.is_multipart():
        return None
    parts: dict[str, bytes] = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        payload = part.get_payload(decode=True)
        if isinstance(name, str) and isinstance(payload, bytes):
            parts[name] = payload
    return parts


def handle_turn_request(handler: BaseHTTPRequestHandler, app: VoiceWebApp) -> None:
    """Serve one ``POST /api/turn`` on ``handler`` (024 contracts/voice-turn-api.md + 033).

    Validates the multipart body — a spoken turn carries an ``audio`` WAV part, a typed turn
    (033) a ``text`` part; exactly one of the two — then streams the app's NDJSON events.
    The caller has already routed the path; this owns everything after that.
    """

    # Deferred: the engine modules pull numpy/chat; a static-only server never loads them.
    from obi.voice.web.turns import valid_conversation_id
    from obi.voice.web.wav import WavError, WavTooLongError, decode

    try:
        length = int(handler.headers.get("Content-Length") or 0)
    except ValueError:
        length = 0
    if length <= 0:
        _send_json(handler, 400, {"error": "a multipart/form-data body is required"})
        return
    if length > MAX_BODY_BYTES:
        _send_json(
            handler,
            413,
            {"error": f"body too large (>{MAX_BODY_BYTES} bytes) — record a shorter question"},
        )
        return
    parts = _multipart_parts(handler, handler.rfile.read(length))
    if parts is None:
        _send_json(handler, 400, {"error": "the body must be multipart/form-data"})
        return
    conversation_raw = parts.get("conversation")
    audio = parts.get("audio")
    text_raw = parts.get("text")
    if conversation_raw is None or (audio is None) == (text_raw is None):
        _send_json(
            handler,
            400,
            {"error": "'conversation' plus exactly one of 'audio' or 'text' is required"},
        )
        return
    conversation_id = conversation_raw.decode("utf-8", "replace").strip()
    if not valid_conversation_id(conversation_id):
        _send_json(
            handler,
            400,
            {"error": "conversation id must be 8-64 chars of [A-Za-z0-9-] (UUID recommended)"},
        )
        return

    # 056: the optional context part rides EITHER input mode (a seeded chat can be asked by
    # mic or by typing). Stripped-empty ⇒ absent; oversize ⇒ 413 so the operator removes the
    # attachment rather than have it silently truncated.
    context: str | None = None
    context_raw = parts.get("context")
    if context_raw is not None:
        stripped = context_raw.decode("utf-8", "replace").strip()
        if stripped:
            if len(stripped) > MAX_CONTEXT_CHARS:
                _send_json(
                    handler,
                    413,
                    {
                        "error": (
                            f"context too large (>{MAX_CONTEXT_CHARS} chars) — "
                            "remove the attachment and ask directly"
                        )
                    },
                )
                return
            context = stripped

    text: str | None = None
    capture = None
    if text_raw is not None:
        text = text_raw.decode("utf-8", "replace").strip()
        if not text:
            _send_json(handler, 400, {"error": "the 'text' part must not be empty"})
            return
        if len(text) > MAX_TEXT_CHARS:
            _send_json(
                handler,
                413,
                {"error": f"question too long (>{MAX_TEXT_CHARS} chars) — ask a shorter one"},
            )
            return
    else:
        assert audio is not None  # the exactly-one gate above guarantees
        try:
            capture = decode(audio, max_seconds=app.settings.web_max_seconds)
        except WavTooLongError as exc:
            _send_json(handler, 413, {"error": str(exc)})
            return
        except WavError as exc:
            _send_json(handler, 400, {"error": str(exc)})
            return
        del audio

    handler.send_response(200)
    handler.send_header("Content-Type", "application/x-ndjson")
    handler.send_header("Cache-Control", "no-cache")
    handler.end_headers()

    def emit(event: dict[str, object]) -> None:
        handler.wfile.write(json.dumps(event).encode() + b"\n")
        handler.wfile.flush()

    try:
        if text is not None:
            app.turn_text(conversation_id, text, emit, context=context)
        else:
            assert capture is not None  # decoded above on the audio branch
            app.turn(conversation_id, capture, emit, context=context)
    except OSError:
        # The browser walked out of Wi-Fi range mid-stream: nothing to answer to.
        logger.info("voice turn client disconnected mid-turn")


def lan_addresses() -> list[str]:
    """Best-effort list of this host's LAN IPv4 addresses (for the printed URLs).

    The primary-route address (UDP connect trick — no packet is sent) first, then any other
    addresses the hostname resolves to; loopback excluded, order-preserving dedup.
    """

    addresses: list[str] = []
    with contextlib.suppress(OSError):
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("192.0.2.1", 80))  # TEST-NET-1: never actually contacted (UDP)
            addresses.append(str(probe.getsockname()[0]))
        finally:
            probe.close()
    with contextlib.suppress(OSError):
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addresses.append(str(info[4][0]))
    seen: set[str] = set()
    result = []
    for address in addresses:
        if address.startswith("127.") or address in seen:
            continue
        seen.add(address)
        result.append(address)
    return result


class _WebRequestHandler(BaseHTTPRequestHandler):
    """Static-allowlist handler, plus the voice routes (032, on by default since 048) and the
    ``POST /api/tool`` web tool bridge (048).

    ``timeout`` (socketserver's per-connection socket timeout) bounds a stalled client: with
    the deferred TLS handshake (084, see ``build_server``) the handshake runs on this worker
    thread's first read, so a client that connects and goes silent costs one thread for at
    most ``timeout`` seconds — never the listener (playa WiFi clients stall mid-handshake
    routinely; one froze the whole NOC UI on 2026-08-19).

    ``voice_app`` is ``None`` on a voice-less server (``[web] voice = false`` or a voice build
    failure): ``/voice.json`` then truthfully reports ``enabled: false`` and ``POST /api/turn`` is
    a 404. ``POST /api/tool`` is always mounted — it runs allowlisted read-only obi tools
    (ping/traceroute); tool-level gating handles an absent ``[routeros]``.
    """

    # 084: per-socket-operation timeout (socketserver applies it to the connection). Bounds a
    # stalled client — including the deferred TLS handshake, which runs on this thread's first
    # read — WITHOUT capping handler duration (a 90 s voice turn blocks on the LLM, not the
    # socket, so it is unaffected).
    timeout = 30

    routes: dict[str, Path] = {}
    voice_app: VoiceWebApp | None = None
    voice_max_seconds: int = 90
    # 038/040: the local-data API's wiring — None keeps each /api route answering its
    # absent-store shape (a state, not an error); the routes never touch the network.
    logs_settings: LogsSettings | None = None
    watch_dir: Path | None = None
    inventory_path: Path | None = None
    metrics_settings: SnmpHistorySettings | None = None
    configs_app_config: Config | None = None
    audit_settings: AuditSettings | None = None
    # 088: the radio transcript feed — a read-only local-store route (camp 1); None ⇒ absent.
    radio_settings: RadioSettings | None = None
    # 095: the status dashboard — serves the cycle's persisted board.json (camp 1, a local FILE
    # read, deliberately never a device read or an evaluation); None ⇒ absent.
    dash_settings: DashSettings | None = None
    # 048: the web tool bridge. `configs_app_config` doubles as the tool bridge's Config (it is the
    # whole app config); `tool_recorder` is the process-singleton audit recorder (lazy WAL conn,
    # thread-shared) so every web tool run is captured with source="web". None ⇒ audit disabled.
    tool_recorder: AuditRecorder | None = None

    # BaseHTTPRequestHandler writes access lines to stderr; route them to the obi logger.
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib signature
        logger.debug("web-server: " + format, *args)

    def _resolve(self) -> tuple[Path, str] | None:
        path = self.path.split("?", 1)[0]
        target = self.routes.get(path)
        if target is None or not target.is_file():
            return None
        content_type = _CONTENT_TYPES.get(target.suffix, "application/octet-stream")
        return target, content_type

    def _respond(self, *, head_only: bool) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            # 081: the liveness probe for `obi web status`. Answered before any store/device/auth
            # path — cheap and always true while the serve loop is alive; no config needed.
            self._healthz(head_only=head_only)
            return
        if path == "/voice.json":
            self._voice_json(head_only=head_only)
            return
        if path == "/api/logs.json":
            # 038/040: explicit dispatch (the /voice.json pattern) — every other /api/* stays 404.
            from obi.web.data_api import handle_logs_request

            handle_logs_request(self, self.logs_settings, head_only=head_only)
            return
        if path == "/api/watch.json":
            from obi.web.data_api import handle_watch_request

            handle_watch_request(self, self.watch_dir, self.logs_settings, head_only=head_only)
            return
        if path == "/api/inventory.json":
            from obi.web.data_api import handle_inventory_request

            handle_inventory_request(self, self.inventory_path, head_only=head_only)
            return
        if path == "/api/metrics.json":
            from obi.web.data_api import handle_metrics_request

            handle_metrics_request(self, self.metrics_settings, head_only=head_only)
            return
        if path == "/api/configs.json":
            from obi.web.data_api import handle_configs_request

            handle_configs_request(self, self.configs_app_config, head_only=head_only)
            return
        if path == "/api/router.json":
            # 047: the ONE live device-read route — read-only RouterOS drill-down (const. 1.8.0).
            from obi.web.data_api import handle_router_request

            handle_router_request(self, self.configs_app_config, head_only=head_only)
            return
        if path == "/api/phy.json":
            # 075: the SECOND live device-read route — read-only PHY console (const. 1.11.0).
            from obi.web.data_api import handle_phy_request

            handle_phy_request(
                self, self.configs_app_config, self.metrics_settings, head_only=head_only
            )
            return
        if path == "/api/health.json":
            # 096: fleet hardware health — local metrics store only, never a device.
            from obi.web.data_api import handle_health_request

            handle_health_request(self, self.configs_app_config, head_only=head_only)
            return
        if path == "/api/audit.json":
            from obi.web.data_api import handle_audit_request

            handle_audit_request(self, self.audit_settings, head_only=head_only)
            return
        if path == "/api/radio.json":
            # 088: the radio transcript feed (local store, read-only — the logs-api idiom).
            from obi.web.data_api import handle_radio_request

            handle_radio_request(self, self.radio_settings, head_only=head_only)
            return
        if path == "/api/radio-clip.wav":
            # 088 follow-on (constitution 1.13.0): replay a retained channel clip on the LAN.
            from obi.web.data_api import handle_radio_clip_request

            handle_radio_clip_request(self, self.radio_settings, head_only=head_only)
            return
        if path == "/api/dash.json":
            # 095: the status board — the persisted board.json (local file, read-only).
            from obi.web.data_api import handle_dash_request

            handle_dash_request(self, self.dash_settings, head_only=head_only)
            return
        if path == "/api/tool":
            # 048: the web tool bridge is POST-only (a GET/HEAD is a client bug, not a 404).
            self.send_error(405, "POST only")
            return
        resolved = self._resolve()
        if resolved is None:
            self.send_error(404, "not served")
            return
        target, content_type = resolved
        try:
            body = target.read_bytes()
        except OSError:
            self.send_error(404, "not served")
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _healthz(self, *, head_only: bool) -> None:
        """The liveness endpoint (081): unconditional ``200 ok`` — no auth, no store access."""

        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _voice_json(self, *, head_only: bool) -> None:
        """The voice capability probe (032, contracts/ar-voice-api.md): always answerable."""

        body = json.dumps(
            {"enabled": self.voice_app is not None, "max_seconds": self.voice_max_seconds}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        self._respond(head_only=False)

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib naming
        self._respond(head_only=True)

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        path = self.path.split("?", 1)[0]
        if path == "/api/turn" and self.voice_app is not None:
            handle_turn_request(self, self.voice_app)
            return
        if path == "/api/tool":
            # 048: run an allowlisted, read-only obi tool for the LAN web user (ping/traceroute).
            # Always mounted — tool-level gating handles an absent [routeros]. Deferred import so a
            # static-only server never pulls the chat stack until the first tool run.
            from obi.web.tool_api import handle_tool_request

            handle_tool_request(self, self.configs_app_config, self.tool_recorder)
            return
        if path in self.routes or path in (
            "/healthz", "/voice.json", "/api/logs.json", "/api/watch.json", "/api/inventory.json",
            "/api/metrics.json", "/api/configs.json", "/api/audit.json", "/api/router.json",
            "/api/phy.json", "/api/radio.json", "/api/radio-clip.wav", "/api/dash.json",
            "/api/health.json",
        ):
            self.send_error(405, "GET/HEAD only")
            return
        self.send_error(404, "not served")


def create_server(
    *,
    bind: str,
    port: int,
    export_path: Path,
    state_dir: Path,
    static_dir: Path = STATIC_DIR,
    voice_app: VoiceWebApp | None = None,
    voice_max_seconds: int = 90,
    logs_settings: LogsSettings | None = None,
    watch_dir: Path | None = None,
    inventory_path: Path | None = None,
    metrics_settings: SnmpHistorySettings | None = None,
    configs_app_config: Config | None = None,
    audit_settings: AuditSettings | None = None,
    radio_settings: RadioSettings | None = None,
    dash_settings: DashSettings | None = None,
) -> ThreadingHTTPServer:
    """Build the TLS-wrapped, allowlisted server (not yet serving — caller runs it).

    Separated from :func:`serve_forever` so tests can run it in-process on an ephemeral port.
    ``voice_app`` (032) mounts the 024 voice turn engine on this origin; ``None`` (default)
    keeps the server exactly as it was — static files only. ``logs_settings`` (038) wires
    the read-only local-data route ``/api/logs.json`` to the 013 store.
    """

    cert, key = ensure_certificate(state_dir)
    # 048: one process-singleton recorder for the web tool bridge (source="web"); None when
    # [audit] is disabled or no config was passed. Deferred import keeps the static server light.
    tool_recorder = None
    if configs_app_config is not None:
        from obi.core.audit import recorder_from_config

        tool_recorder = recorder_from_config(configs_app_config)
    handler = type(
        "_BoundWebRequestHandler",
        (_WebRequestHandler,),
        {
            "routes": _allowlist(static_dir, export_path, state_dir / "snapshots"),
            "voice_app": voice_app,
            "voice_max_seconds": voice_max_seconds,
            "logs_settings": logs_settings,
            "watch_dir": watch_dir,
            "inventory_path": inventory_path,
            "metrics_settings": metrics_settings,
            "configs_app_config": configs_app_config,
            "audit_settings": audit_settings,
            "radio_settings": radio_settings,
            "dash_settings": dash_settings,
            "tool_recorder": tool_recorder,
        },
    )
    server = _QuietTLSServer((bind, port), handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=cert, keyfile=key)
    # 084: DEFER the TLS handshake to each worker thread. Wrapping the listening socket with
    # handshake-on-connect (the default) runs the handshake inside accept() on the single
    # listener thread — one client that TCP-connects and stalls mid-handshake freezes every
    # other request (observed live 2026-08-19: server alive, idle, all routes timing out).
    # With do_handshake_on_connect=False the accepted SSLSocket handshakes lazily on the
    # worker's first read, bounded by the handler's per-connection ``timeout``.
    server.socket = context.wrap_socket(
        server.socket, server_side=True, do_handshake_on_connect=False
    )
    return server


class _QuietTLSServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that logs stalled/garbage connections in one line (084).

    With the deferred TLS handshake, a scanner poking the port or a client dying mid-handshake
    raises in the WORKER thread (SSLError / timeout / reset) — routine on an event network, so
    they get one stderr line instead of socketserver's default full traceback. Real handler
    errors keep the default behavior."""

    def handle_error(
        self, request: socket.socket | tuple[bytes, socket.socket], client_address: object
    ) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, (ssl.SSLError, TimeoutError, ConnectionError)):
            print(f"web: dropped {client_address}: {exc!r}", file=sys.stderr)
            return
        super().handle_error(request, client_address)


def urls_for(server: ThreadingHTTPServer, bind: str) -> list[str]:
    """Every candidate ``https://`` URL to open from the headset (wrong-VLAN diagnosis)."""

    port = server.server_address[1]
    hosts = lan_addresses() if bind in ("", "0.0.0.0") else [bind]
    return [f"https://{host}:{port}/" for host in hosts] or [f"https://{bind}:{port}/"]


# --- feature 136: the LOCAL preview of a static demo bundle -----------------------------------


def create_demo_server(
    bundle: Path, *, base: str = "/obi/", port: int = 8765
) -> ThreadingHTTPServer:
    """Serve a built demo bundle statically under ``base`` on 127.0.0.1 — nothing else.

    Separate from the live allowlist server on purpose: it reads no store, calls no handler and
    has no API routes. It serves exactly the files a static host (GitHub Pages, the VM) would, so
    the preview proves the published site, including the sub-path.
    """

    import posixpath
    from functools import partial
    from http.server import SimpleHTTPRequestHandler
    from urllib.parse import unquote

    prefix = "/" + base.strip("/") + "/" if base.strip("/") else "/"
    root = bundle.resolve()

    class _DemoHandler(SimpleHTTPRequestHandler):
        def translate_path(self, path: str) -> str:
            raw = unquote(path.split("?", 1)[0].split("#", 1)[0])
            clean = posixpath.normpath(raw) + ("/" if raw.endswith("/") else "")
            if not clean.startswith(prefix):
                return str(root / ".outside-the-base")  # never exists → 404
            return super().translate_path("/" + clean[len(prefix) :])

        def do_GET(self) -> None:
            if self.path.split("?", 1)[0] == prefix.rstrip("/") and prefix != "/":
                self.send_response(301)
                self.send_header("Location", prefix)
                self.end_headers()
                return
            super().do_GET()

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return None

    handler = partial(_DemoHandler, directory=str(root))
    return ThreadingHTTPServer(("127.0.0.1", port), handler)
