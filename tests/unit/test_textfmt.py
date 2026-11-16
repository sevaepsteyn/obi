"""Per-channel formatting converters (feature 082): markdown → Slack mrkdwn and → plain prose."""

from __future__ import annotations

import pytest

from obi.core.textfmt import to_plain_text, to_slack_mrkdwn


class TestToSlackMrkdwn:
    @pytest.mark.parametrize(
        ("md", "expected"),
        [
            ("**tamarack-gw** is up", "*tamarack-gw* is up"),
            ("__also bold__", "*also bold*"),
            ("***both***", "*_both_*"),
            ("~~gone~~", "~gone~"),
            ("# Heading", "*Heading*"),
            ("### Small heading ###", "*Small heading*"),
            ("- one\n- two", "• one\n• two"),
            ("* star bullet", "• star bullet"),
            ("[obi](https://example.com)", "<https://example.com|obi>"),
        ],
    )
    def test_each_construct(self, md: str, expected: str) -> None:
        assert to_slack_mrkdwn(md) == expected

    def test_bold_device_and_uptime_like_the_reported_bug(self) -> None:
        md = "**tamarack-gw** has been up for **7 weeks and 5 days**."
        assert to_slack_mrkdwn(md) == "*tamarack-gw* has been up for *7 weeks and 5 days*."
        assert "**" not in to_slack_mrkdwn(md)

    def test_inline_and_fenced_code_kept_and_not_reformatted(self) -> None:
        assert to_slack_mrkdwn("run `a **b** c`") == "run `a **b** c`"  # no bold inside code
        fenced = "```\n**not bold**\n```"
        assert to_slack_mrkdwn(fenced) == fenced

    def test_underscore_italic_and_single_star_are_left_alone(self) -> None:
        # `_x_` is valid italic in both dialects; single `*` is not rewritten (ambiguity, D3).
        assert to_slack_mrkdwn("_italic_ stays") == "_italic_ stays"
        assert to_slack_mrkdwn("core_gw_1 unchanged") == "core_gw_1 unchanged"

    def test_bold_wrapping_a_link(self) -> None:
        assert to_slack_mrkdwn("**[t](https://x.io)**") == "*<https://x.io|t>*"

    def test_no_op_on_plain_text(self) -> None:
        plain = "tamarack-gw is healthy — CPU at 1% and 3.7 GiB free.\nsecond line"
        assert to_slack_mrkdwn(plain) == plain  # byte-for-byte, incl. newline (INV-1)

    def test_idempotent_on_own_output(self) -> None:
        md = "# Report\n\n- **core-1** down\n- see [log](https://l.io) ~~old~~ `code`"
        once = to_slack_mrkdwn(md)
        assert to_slack_mrkdwn(once) == once  # INV-2

    def test_empty(self) -> None:
        assert to_slack_mrkdwn("") == ""


class TestToPlainText:
    @pytest.mark.parametrize(
        ("md", "expected"),
        [
            ("**bold**", "bold"),
            ("*italic*", "italic"),
            ("_italic_", "italic"),
            ("***both***", "both"),
            ("~~gone~~", "gone"),
            ("`code`", "code"),
            ("# Heading", "Heading"),
            ("## What happened", "What happened"),
            ("- one\n- two", "one\ntwo"),
            ("1. first\n2. second", "first\nsecond"),
            ("[obi docs](https://example.com)", "obi docs"),
            ("> quoted", "quoted"),
        ],
    )
    def test_each_construct(self, md: str, expected: str) -> None:
        assert to_plain_text(md) == expected

    def test_strips_all_markup_symbols_from_a_rich_answer(self) -> None:
        md = "**tamarack-gw** is up for *7 weeks*. See `uptime` and [more](https://x.io)."
        out = to_plain_text(md)
        assert out == "tamarack-gw is up for 7 weeks. See uptime and more."
        for symbol in ("**", "*", "`", "#", "](", "~~"):
            assert symbol not in out

    def test_keeps_underscores_inside_words(self) -> None:
        assert to_plain_text("device core_gw_1 rebooted") == "device core_gw_1 rebooted"

    def test_no_op_on_plain_text(self) -> None:
        assert to_plain_text("all links are up") == "all links are up"

    def test_idempotent(self) -> None:
        md = "## Status\n\n- **core-1** is *down*\n- `iface` ether1"
        once = to_plain_text(md)
        assert to_plain_text(once) == once

    def test_empty(self) -> None:
        assert to_plain_text("") == ""
