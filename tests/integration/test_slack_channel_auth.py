"""T007 (US1): channel membership authorizes DMs — allowlist-first, fail-closed (feature 104)."""

from __future__ import annotations

import json
from pathlib import Path

from obi.core.inventory import InventoryEntry
from obi.core.models import DiscoveryState
from tests.fakes import (
    FakeMembership,
    chat_config,
    chat_runtime,
    dm_event,
    save_inventory,
    slack_handler,
    text_response,
    tool_response,
)

WAN = "C0FAKE66336"


def _runtime(tmp_path: Path, *, answers: int = 1):  # noqa: ANN202 — test helper
    config = chat_config(tmp_path)
    save_inventory(
        config.inventory_path, [InventoryEntry("172.25.110.1", "gw", DiscoveryState.IDENTIFIED)]
    )
    return chat_runtime(config, responses=[text_response("answer") for _ in range(answers)])


def _audit_records(tmp_path: Path) -> list[dict[str, object]]:
    path = tmp_path / "audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_channel_member_dm_is_answered_and_attributed(tmp_path: Path) -> None:
    config = chat_config(tmp_path)
    save_inventory(
        config.inventory_path, [InventoryEntry("172.25.110.1", "gw", DiscoveryState.IDENTIFIED)]
    )
    runtime = chat_runtime(
        config, responses=[tool_response("inventory_list", {}), text_response("answer")]
    )
    membership = FakeMembership(rosters={WAN: {"U_WAN1", "U_WAN2"}})
    handler, slack = slack_handler(
        runtime, allowlist=["U_ADMIN"], membership=membership, authorized_channels=[WAN]
    )

    handler.handle_event(dm_event("what's down?", user="U_WAN1", channel="D5", ts="3.1"))

    assert "answer" in "\n".join(slack.live_texts())
    assert len(runtime.client.calls) == 2
    # Attribution: the tool access is audited under the member's ID, exactly like an operator's.
    records = _audit_records(tmp_path)
    assert records and all(r.get("actor") == "U_WAN1" for r in records)


def test_non_member_dm_refused_denied_zero_access(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    membership = FakeMembership(rosters={WAN: {"U_WAN1"}})
    handler, slack = slack_handler(
        runtime, allowlist=["U_ADMIN"], membership=membership, authorized_channels=[WAN]
    )

    handler.handle_event(dm_event("what's down?", user="U_STRANGER", channel="D9", ts="4.1"))

    assert any("not authorized" in t.lower() for t in slack.texts())
    assert runtime.client.calls == []
    records = _audit_records(tmp_path)
    assert len(records) == 1
    assert records[0]["action"] == "slack:unauthorized"
    assert records[0]["outcome"] == "denied"
    assert records[0]["actor"] == "U_STRANGER"


def test_membership_error_fails_closed_with_honest_reply(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    membership = FakeMembership(fail=True)
    handler, slack = slack_handler(
        runtime, allowlist=["U_ADMIN"], membership=membership, authorized_channels=[WAN]
    )

    handler.handle_event(dm_event("what's down?", user="U_WAN1", channel="D5", ts="5.1"))

    # The honest degraded message — not the flat refusal — and zero model/tool access.
    assert any("verify" in t.lower() for t in slack.texts())
    assert not any("not authorized" in t.lower() for t in slack.texts())
    assert runtime.client.calls == []
    records = _audit_records(tmp_path)
    assert len(records) == 1
    assert records[0]["action"] == "slack:auth_error"
    assert records[0]["outcome"] == "error"
    assert records[0]["actor"] == "U_WAN1"


def test_allowlisted_dm_unaffected_by_membership_outage(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    membership = FakeMembership(fail=True)
    handler, slack = slack_handler(
        runtime, allowlist=["U_ADMIN"], membership=membership, authorized_channels=[WAN]
    )

    handler.handle_event(dm_event("what's down?", user="U_ADMIN", channel="D1", ts="6.1"))

    assert "answer" in "\n".join(slack.live_texts())
    # Allowlist is checked first: no roster call was even attempted for the operator.
    assert membership.calls == []


def test_no_channels_configured_keeps_pre104_behavior(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    handler, slack = slack_handler(runtime, allowlist=["U_ADMIN"])

    handler.handle_event(dm_event("what's down?", user="U_WAN1", channel="D5", ts="7.1"))

    assert any("not authorized" in t.lower() for t in slack.texts())
    assert runtime.client.calls == []


# --- T013 (US3): access follows the roster ------------------------------------------------------


def test_roster_flip_grants_then_revokes_without_restart(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, answers=2)
    membership = FakeMembership(rosters={WAN: set()})
    handler, slack = slack_handler(
        runtime, allowlist=["U_ADMIN"], membership=membership, authorized_channels=[WAN]
    )

    handler.handle_event(dm_event("hello?", user="U_NEW", channel="D5", ts="8.1"))
    assert any("not authorized" in t.lower() for t in slack.texts())

    membership.rosters[WAN].add("U_NEW")  # joins the channel (cache TTL is unit-tested)
    handler.handle_event(dm_event("hello again?", user="U_NEW", channel="D5", ts="9.1"))
    assert "answer" in "\n".join(slack.live_texts())

    membership.rosters[WAN].discard("U_NEW")  # leaves the channel
    handler.handle_event(dm_event("still there?", user="U_NEW", channel="D5", ts="10.1"))
    refusals = [t for t in slack.texts() if "not authorized" in t.lower()]
    assert len(refusals) == 2
    assert len(runtime.client.calls) == 1
