"""The web tool bridge (feature 048): run an allowlisted obi tool for a LAN web user.

The web server's third extension point, alongside the static allowlist and the local-data ``/api``
routes (:mod:`obi.web.data_api`). Unlike those, this route runs a LIVE, read-only obi tool on
operator action — the first web path to obi's tools that is **not** mediated by the LLM (voice/chat
reach the same tools via the reasoning model). It reuses the exact chat/voice/watch machinery:
:func:`obi.chat.session.build_tool_context` + :func:`obi.core.toolset.build_tools` +
:func:`obi.core.toolset.build_dispatch`, so device resolution, the ``[routeros.live]`` caps, the
read-only guarantee, and the audit trail are all inherited unchanged — there is no second
ping/traceroute implementation. This is what makes the same tool reachable "by obi directly or by
the web user".

Layering (constitution III): this module parses the request and dispatches to the ONE core toolset;
all tool logic stays in the core. Read-only by construction: obi has no mutating tool, and
``WEB_TOOL_NAMES`` is a static second guard that never lowers a risk class at runtime. Every run is
recorded on the ambient audit scope with ``source="web"`` (feature 042). Heavy imports (the chat
stack pulls the vendor + LLM modules) are deferred so the static-only server stays light — the
``server.py`` discipline.

See ``specs/048-web-net-tools/contracts/web-tool-bridge.md``.
"""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING

from obi.web.data_api import _send_json

if TYPE_CHECKING:
    from http.server import BaseHTTPRequestHandler

    from obi.core.audit import AuditRecorder
    from obi.core.config import Config

# The static, in-code allowlist of tools a web user may invoke. v1 = the two RouterOS live
# diagnostics (read-only). This is the security boundary — a tool absent from it is refused before
# any dispatch, even though it exists for the LLM. Adding a future web tool = add its name here +
# ship its widget (contract §Invariants).
WEB_TOOL_NAMES: tuple[str, ...] = ("routeros_ping", "routeros_traceroute")

# A {tool, args} body is tiny; 4 KiB is ample and caps a malformed/abusive request cheaply.
_MAX_TOOL_BODY_BYTES = 4096


def validate_web_tools() -> None:
    """Assert the allowlist invariants (contract §Invariants 1–2); raises on violation.

    Used by the contract test — and cheap enough to call at wiring time — to guarantee every
    allowlisted name is a real, read-only tool (obi has none that mutate; this is a standing guard
    against ever adding a state-changing tool to the web surface).
    """

    from obi.core.toolset import build_tools

    by_name = {tool.name: tool for tool in build_tools()}
    for name in WEB_TOOL_NAMES:
        tool = by_name.get(name)
        if tool is None:
            raise ValueError(f"WEB_TOOL_NAMES has {name!r}, which is not a registered obi tool")
        if tool.mutating:
            raise ValueError(f"WEB_TOOL_NAMES has {name!r}, which is not read-only")


def handle_tool_request(
    handler: BaseHTTPRequestHandler,
    config: Config | None,
    recorder: AuditRecorder | None,
) -> None:
    """Serve one ``POST /api/tool``: run an allowlisted obi tool and return its rendered text.

    The caller (server.py ``do_POST``) has already routed the path; this owns everything after.
    A tool-level failure (unresolvable device, unreachable destination, 100% loss, missing arg) is
    the tool's own ``"error …"`` string returned with HTTP 200 — the surface never crashes on it
    (FR-010). Only a malformed request or an off-allowlist tool is a 4xx.
    """

    try:
        length = int(handler.headers.get("Content-Length") or 0)
    except ValueError:
        length = 0
    if length <= 0 or length > _MAX_TOOL_BODY_BYTES:
        _bad(handler, 400, "expected a JSON object body")
        return
    try:
        body = json.loads(handler.rfile.read(length))
    except (ValueError, UnicodeDecodeError):
        _bad(handler, 400, "expected a JSON object body")
        return
    if not isinstance(body, dict):
        _bad(handler, 400, "expected a JSON object body")
        return

    tool = body.get("tool")
    if not isinstance(tool, str) or not tool.strip():
        _bad(handler, 400, "missing 'tool'")
        return
    tool = tool.strip()
    args = body.get("args", {})
    if not isinstance(args, dict):
        _bad(handler, 400, "'args' must be an object")
        return
    if tool not in WEB_TOOL_NAMES:
        _bad(handler, 403, "tool not available on the web surface")
        return
    if config is None:
        # The server was started without a config (only tests do this): nothing to run against.
        _send_json(
            handler, 200,
            {"ok": True, "tool": tool, "text": "error: the web server has no config loaded."},
            head_only=False,
        )
        return

    # Deferred: the chat stack pulls the vendor + LLM modules; the static server stays light.
    from obi.chat.session import build_tool_context
    from obi.core.audit import audit_scope
    from obi.core.toolset import build_dispatch, build_tools

    # A fresh context + dispatch per request keeps this thread-safe under ThreadingHTTPServer (the
    # ToolContext carries mutable per-call fields); construction does no device/network I/O.
    ctx = build_tool_context(config)
    ctx.question = f"web tool: {tool}"
    dispatch = build_dispatch(ctx, build_tools())
    run = dispatch.get(tool)
    if run is None:  # allowlisted but not registered — a wiring bug (validate_web_tools guards it)
        _bad(handler, 403, "tool not available on the web surface")
        return

    session_id = f"web-{uuid.uuid4().hex[:12]}"
    with audit_scope(recorder, "web", session_id):
        text = run(args)  # dispatch never raises: a failure returns an "error …" string
    _send_json(handler, 200, {"ok": True, "tool": tool, "text": text}, head_only=False)


def _bad(handler: BaseHTTPRequestHandler, status: int, reason: str) -> None:
    _send_json(handler, status, {"ok": False, "error": reason}, head_only=False)
