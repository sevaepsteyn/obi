"""Unit: the Slack-DM member paging tool (feature 130, constitution 3.0.0).

Covers the deterministic guards around the verbatim relay: strict roster resolution, the
operator recipient allowlist ([paging.slack] recipients — the WAN team / on-call shape),
attribution + total-length bound, the per-sender sliding-hour cap, and honest transport
outcomes. All offline: an injected opener plays the uAPI, tmp_path holds roster and cap
state (XDG-free by construction — every path flows from ``roster_path``).
"""

from __future__ import annotations

import json
import ssl
import urllib.request
from pathlib import Path
from typing import Any, cast

from obi.core.config import PagingSettings, PagingSlackSettings, Secret
from obi.core.toolset import MEDIUM_RISK, ToolContext
from obi.paging.chat_tool import CAP_FILE_NAME, build_page_send_tool
from obi.paging.client import PagingClient
from obi.paging.roster import RosterEntry, save_roster

_HOUR_US = 3_600 * 1_000_000

_ROSTER = [
    RosterEntry(type="person", id="430", name="Mira", fullname="Mira E"),
    RosterEntry(type="person", id="101", name="Alex One", fullname="Alex Won"),
    RosterEntry(type="person", id="102", name="Alexa Two", fullname="Alexa Toews"),
    RosterEntry(type="person", id="103", name="Kai", fullname="Kai Kim"),
    RosterEntry(type="group", id="7", name="WAN On Call"),
    RosterEntry(type="group", id="8", name="Towers On Call"),
    RosterEntry(type="group", id="9", name="All ESD"),
]

#: The operator-shaped allowlist: named people + the two on-call rotations. Kai and the
#: "All ESD" group are deliberately outside it.
_RECIPIENTS = ["mira", "alex one", "wan on call", "towers on call"]

_CTX = cast(ToolContext, None)  # the handler ignores ctx (audited via record_page instead)


class _Opener:
    """Plays the uAPI: records every request payload, returns a scripted body."""

    def __init__(self, body: str = '{"status": "queued"}', status: int = 200) -> None:
        self.body = body
        self.status = status
        self.payloads: list[dict[str, Any]] = []

    def __call__(
        self, req: urllib.request.Request, ctx: ssl.SSLContext | None, timeout: float
    ) -> tuple[int, str]:
        assert req.data is not None
        self.payloads.append(json.loads(req.data.decode()))
        return self.status, self.body


class _Clock:
    def __init__(self, start_us: int = 1_000_000_000_000) -> None:
        self.us = start_us

    def __call__(self) -> int:
        return self.us


def _settings(
    tmp_path: Path, *, recipients: list[str] | None = None, max_per_hour: int = 4
) -> PagingSettings:
    roster_path = tmp_path / "roster.csv"
    save_roster(roster_path, _ROSTER)
    return PagingSettings(
        username="mira",
        password=Secret("pw"),
        url="https://pager.example.net/api.php",
        roster_path=roster_path,
        slack=PagingSlackSettings(
            channels=["C_WAN"],
            recipients=list(_RECIPIENTS if recipients is None else recipients),
            max_pages_per_hour=max_per_hour,
        ),
    )


def _tool(
    settings: PagingSettings,
    *,
    opener: _Opener | None = None,
    clock: _Clock | None = None,
    sender_id: str = "U_MEMBER",
    sender_display: str = "Toph",
) -> tuple[Any, _Opener, _Clock]:
    opener = opener or _Opener()
    clock = clock or _Clock()
    client = PagingClient(settings, opener=opener, sleep=lambda _s: None)
    tool = build_page_send_tool(
        settings,
        sender_id=sender_id,
        sender_display=sender_display,
        client=client,
        now_us=clock,
    )
    return tool, opener, clock


def _cap_state(settings: PagingSettings) -> dict[str, list[int]]:
    path = settings.roster_path.parent / CAP_FILE_NAME
    return json.loads(path.read_text()) if path.exists() else {}


# --- the relay (US1) --------------------------------------------------------------------------


def test_success_sends_one_attributed_page(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    tool, opener, _ = _tool(settings)

    result = tool.handler(_CTX, {"to": "mira", "message": "generator down at NOC"})

    assert result == "page sent to Mira (430): from Toph via obi: generator down at NOC"
    assert len(opener.payloads) == 1
    sent = opener.payloads[0]["sendpage"]
    assert sent["recipients"] == {"people": ["430"]}
    assert sent["message"] == "from Toph via obi: generator down at NOC"
    assert _cap_state(settings) == {"U_MEMBER": [1_000_000_000_000]}


def test_tool_shape_is_the_contract(tmp_path: Path) -> None:
    tool, _, _ = _tool(_settings(tmp_path))
    assert tool.name == "page_send"
    assert tool.mutating is True
    assert tool.risk_class == MEDIUM_RISK
    assert set(tool.input_schema["required"]) == {"to", "message"}


def test_allowlisted_group_is_pageable(tmp_path: Path) -> None:
    # The on-call rotations are groups; being allowlisted is exactly what makes them legal.
    tool, opener, _ = _tool(_settings(tmp_path))

    result = tool.handler(_CTX, {"to": "towers on call", "message": "ice on the guy wires"})

    assert result.startswith("page sent to Towers On Call (8):")
    assert opener.payloads[0]["sendpage"]["recipients"] == {"groups": ["8"]}


def test_transport_failure_is_honest_and_uncounted(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    tool, opener, _ = _tool(settings, opener=_Opener(body='{"error": "unknown recipient"}'))

    result = tool.handler(_CTX, {"to": "mira", "message": "hi"})

    assert result == "error page failed: unknown recipient"
    assert len(opener.payloads) == 1
    assert _cap_state(settings) == {}  # a failed page never consumes the budget


# --- the recipient allowlist (operator decision 2026-09-03) -----------------------------------


def test_person_outside_the_allowlist_refuses_everything(tmp_path: Path) -> None:
    # All-or-nothing: one blocked recipient refuses the whole request, nothing transmits.
    tool, opener, _ = _tool(_settings(tmp_path))

    result = tool.handler(_CTX, {"to": "mira, kai", "message": "hi"})

    assert result.startswith("error: not on the pageable list: Kai")
    assert opener.payloads == []


def test_group_outside_the_allowlist_refuses(tmp_path: Path) -> None:
    tool, opener, _ = _tool(_settings(tmp_path))

    result = tool.handler(_CTX, {"to": "all esd", "message": "hi"})

    assert result.startswith("error: not on the pageable list: All ESD")
    assert opener.payloads == []


def test_allowlist_typo_narrows_never_widens(tmp_path: Path) -> None:
    # A token that resolves to nothing grants nothing; the rest of the allowlist stands.
    tool, opener, _ = _tool(_settings(tmp_path, recipients=["mira", "no such name"]))

    assert tool.handler(_CTX, {"to": "mira", "message": "hi"}).startswith("page sent")
    assert tool.handler(_CTX, {"to": "alex one", "message": "hi"}).startswith(
        "error: not on the pageable list"
    )
    assert len(opener.payloads) == 1


# --- strict roster protection (US3) -----------------------------------------------------------


def test_ambiguous_name_refuses_with_candidates(tmp_path: Path) -> None:
    tool, opener, _ = _tool(_settings(tmp_path))

    result = tool.handler(_CTX, {"to": "alex", "message": "hi"})

    assert result.startswith("error: ")
    assert "ambiguous" in result
    assert "Alex One (101)" in result
    assert "Alexa Two (102)" in result
    assert opener.payloads == []


def test_unknown_name_refuses(tmp_path: Path) -> None:
    tool, opener, _ = _tool(_settings(tmp_path))
    result = tool.handler(_CTX, {"to": "bob", "message": "hi"})
    assert result.startswith("error: no recipient matches 'bob'")
    assert opener.payloads == []


def test_missing_roster_refuses_with_operator_guidance(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.roster_path.unlink()
    tool, opener, _ = _tool(settings)

    result = tool.handler(_CTX, {"to": "mira", "message": "hi"})

    assert "no paging roster on this machine" in result
    assert "obi page refresh" in result
    assert opener.payloads == []


def test_empty_message_and_empty_recipients_refuse(tmp_path: Path) -> None:
    tool, opener, _ = _tool(_settings(tmp_path))
    assert tool.handler(_CTX, {"to": "mira", "message": "  "}) == "error: a page needs message text"
    assert (
        tool.handler(_CTX, {"to": " ", "message": "hi"})
        == "error: a page needs at least one recipient"
    )
    assert opener.payloads == []


def test_over_length_names_the_post_prefix_overage(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    tool, opener, _ = _tool(settings)
    # "from Toph via obi: " is 19 chars; fill to exactly one char over the 200 limit.
    message = "x" * (settings.message_max_chars - 19 + 1)

    result = tool.handler(_CTX, {"to": "mira", "message": message})

    assert result.startswith("error: message too long — 1 chars over the 200-char page limit")
    assert opener.payloads == []


# --- the per-sender sliding-hour cap ----------------------------------------------------------


def test_cap_suppresses_then_slides(tmp_path: Path) -> None:
    settings = _settings(tmp_path, max_per_hour=2)
    clock = _Clock()
    tool, opener, _ = _tool(settings, clock=clock)

    assert tool.handler(_CTX, {"to": "mira", "message": "one"}).startswith("page sent")
    clock.us += 60 * 1_000_000
    assert tool.handler(_CTX, {"to": "mira", "message": "two"}).startswith("page sent")
    clock.us += 60 * 1_000_000
    result = tool.handler(_CTX, {"to": "mira", "message": "three"})
    assert result == (
        "error: page cap reached (2/hour per sender) — try later or contact the operator"
    )
    assert len(opener.payloads) == 2  # the suppressed request never reached the transport

    clock.us += _HOUR_US  # the window slides past the first two sends
    assert tool.handler(_CTX, {"to": "mira", "message": "four"}).startswith("page sent")
    assert len(opener.payloads) == 3


def test_cap_is_per_sender(tmp_path: Path) -> None:
    settings = _settings(tmp_path, max_per_hour=1)
    clock = _Clock()
    opener = _Opener()
    tool_a, _, _ = _tool(settings, opener=opener, clock=clock, sender_id="U_A")
    tool_b, _, _ = _tool(settings, opener=opener, clock=clock, sender_id="U_B")

    assert tool_a.handler(_CTX, {"to": "mira", "message": "hi"}).startswith("page sent")
    assert tool_a.handler(_CTX, {"to": "mira", "message": "hi"}).startswith(
        "error: page cap reached"
    )
    # A's exhausted budget never starves B — the state file is keyed by sender.
    assert tool_b.handler(_CTX, {"to": "mira", "message": "hi"}).startswith("page sent")
    assert set(_cap_state(settings)) == {"U_A", "U_B"}
