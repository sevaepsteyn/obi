"""T013 (US1): an allowlisted DM → grounded answer in-thread, with a working indicator + audit."""

from __future__ import annotations

from pathlib import Path

from obi.core.inventory import InventoryEntry
from obi.core.models import DiscoveryState
from tests.fakes import (
    chat_config,
    chat_runtime,
    dm_event,
    save_inventory,
    slack_handler,
    text_response,
    tool_response,
)


def test_dm_question_grounded_and_threaded(tmp_path: Path) -> None:
    config = chat_config(tmp_path)
    save_inventory(
        config.inventory_path,
        [
            InventoryEntry("172.25.110.1", "gw", DiscoveryState.IDENTIFIED),
            InventoryEntry("172.25.110.2", "ap", DiscoveryState.UNREACHABLE),
        ],
    )
    runtime = chat_runtime(
        config,
        responses=[
            tool_response("inventory_list", {"discovery_state": "unreachable"}),
            text_response("One device is unreachable: ap."),
        ],
    )
    handler, slack = slack_handler(runtime, allowlist=["U_OP"])

    handler.handle_event(dm_event("what's down?", user="U_OP", channel="D1", ts="1.1"))

    texts = slack.texts()
    # A transient "thinking" placeholder is posted before the answer (FR-012)…
    work_idx = next(i for i, t in enumerate(texts) if "thinking" in t.lower())
    ans_idx = next(i for i, t in enumerate(texts) if "ap" in t)
    assert work_idx < ans_idx
    # …and is deleted once the real reply lands, so it never lingers next to the answer.
    assert slack.posts[work_idx]["ts"] in slack.deletes
    assert not any("thinking" in t.lower() for t in slack.live_texts())
    # The answer is posted in-thread to the message root, in the same DM channel.
    answer = next(p for p in slack.posts if "ap" in p["text"])
    assert answer["channel"] == "D1"
    assert answer["thread_ts"] == "1.1"
    # The tool ran and was audited, attributed to the operator's Slack user ID.
    lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    assert any('"inventory_list"' in line and '"actor":"U_OP"' in line for line in lines)


def test_answer_markdown_is_rendered_as_slack_mrkdwn(tmp_path: Path) -> None:
    # feature 082: the model's standard markdown is converted to Slack mrkdwn before posting, so the
    # operator sees *bold*, never literal **bold**. Service placeholders are posted unconverted.
    config = chat_config(tmp_path)
    save_inventory(
        config.inventory_path,
        [InventoryEntry("172.25.110.1", "tamarack-gw", DiscoveryState.IDENTIFIED)],
    )
    runtime = chat_runtime(
        config,
        responses=[text_response("**tamarack-gw** has been up for **7 weeks and 5 days**.")],
    )
    handler, slack = slack_handler(runtime, allowlist=["U_OP"])

    handler.handle_event(dm_event("status?", user="U_OP", channel="D1", ts="1.1"))

    answer = next(p for p in slack.posts if "tamarack-gw" in p["text"])
    assert "*tamarack-gw*" in answer["text"] and "*7 weeks and 5 days*" in answer["text"]
    assert "**" not in answer["text"]  # no literal double-asterisks reach the operator
    # the transient "thinking" placeholder is a service message, posted verbatim (not converted).
    assert any(t == "🧠 thinking…" for t in slack.texts())
