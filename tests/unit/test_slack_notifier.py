"""SlackNotifier adapter: DM formatting, threading, digest, and never-raise (feature 015)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from obi.core.watch import DeliveryResult, Evidence, WatchFinding
from obi.slack.notifier import SlackNotifier
from tests.fakes import FakeWebClient, watch_config

WIN_START = datetime(2026, 6, 25, 11, 0, 0, tzinfo=UTC)
WIN_END = datetime(2026, 6, 25, 12, 0, 0, tzinfo=UTC)


def _finding(  # type: ignore[no-untyped-def]
    device="core-1", severity="warning", summary="ether1 flapping", description="x"
):
    return WatchFinding(
        id=f"{device}-{severity}",
        created_at=WIN_END,
        device=device,
        window_start=WIN_START,
        window_end=WIN_END,
        severity=severity,
        confidence="high",
        summary=summary,
        description=description,
        evidence=[Evidence(kind="log", text="link ether1 down")],
        source="llm",
        suggested_remediation="check the cable",
    )


def _notifier(web):  # type: ignore[no-untyped-def]
    return SlackNotifier(web, "U_OP")


def test_single_finding_posts_summary_and_threaded_report() -> None:
    web = FakeWebClient()

    result = _notifier(web).deliver([_finding()])

    assert result == DeliveryResult(delivered=1, failed=0)
    assert web.opened == [["U_OP"]]  # a DM was opened for the recipient
    assert len(web.parents()) == 1  # one summary message
    assert "ether1 flapping" in web.parents()[0]["text"]
    assert web.replies()  # the full report is threaded under it
    assert all(post["channel"] == "D_OP" for post in web.posts)  # DM only, never a channel


def test_digest_for_multiple_is_a_single_parent() -> None:
    web = FakeWebClient()
    findings = [_finding("core-1", summary="a"), _finding("dist-2", "critical", "b")]

    result = _notifier(web).deliver(findings)

    assert result.delivered == 2
    assert len(web.parents()) == 1  # ONE digest message, never N separate DMs (FR-016)
    assert "2 findings" in web.parents()[0]["text"]
    assert len(web.replies()) >= 2  # one full report threaded per finding


def test_oversized_report_is_chunked_across_replies() -> None:
    web = FakeWebClient()

    result = _notifier(web).deliver([_finding(description="x" * 9000)])  # > Slack's limit

    assert result.delivered == 1
    assert len(web.replies()) >= 2  # split, content never dropped


def test_deliver_never_raises_when_opening_dm_fails() -> None:
    web = FakeWebClient(open_fail=True)

    result = _notifier(web).deliver([_finding()])

    assert result.delivered == 0 and result.failed == 1
    assert result.error  # a short reason is captured


def test_deliver_never_raises_when_parent_post_fails() -> None:
    web = FakeWebClient(post_fail=True)

    result = _notifier(web).deliver([_finding(), _finding("dist-2")])

    assert result.delivered == 0 and result.failed == 2


def test_failed_thread_reply_does_not_reduce_delivered() -> None:
    web = FakeWebClient(fail_replies=True)  # parent posts ok; threaded replies fail

    result = _notifier(web).deliver([_finding()])

    assert result == DeliveryResult(delivered=1, failed=0)  # operator already has the summary
    assert web.parents() and not web.replies()


def test_from_config_resolves_recipient_from_allowlist(tmp_path: Path) -> None:
    cfg = watch_config(tmp_path, slack=True)  # allowlist ["U_OP"], no slack_user override

    notifier = SlackNotifier.from_config(cfg)

    assert notifier._recipient == "U_OP"  # built from only the bot token + allowlist


def test_from_config_slack_user_overrides_allowlist(tmp_path: Path) -> None:
    cfg = watch_config(tmp_path, slack=True, slack_user="U_OTHER")

    notifier = SlackNotifier.from_config(cfg)

    assert notifier._recipient == "U_OTHER"


def test_summary_and_report_render_in_display_tz() -> None:
    # 069: with the operator's local zone, both the parent summary's Window and the threaded
    # report render local (UTC-7 in summer), never the stored UTC wall-clock.
    from zoneinfo import ZoneInfo

    web = FakeWebClient()
    notifier = SlackNotifier(web, "U_OP", ZoneInfo("America/Los_Angeles"))

    result = notifier.deliver([_finding()])  # window 11:00→12:00 UTC == 04:00→05:00 PDT

    assert result == DeliveryResult(delivered=1, failed=0)
    parent = web.parents()[0]["text"]
    assert "04:00:00-07:00 → 2026-06-25T05:00:00-07:00" in parent
    report = "".join(post["text"] for post in web.replies())
    assert "-07:00" in report and "12:00:00+00:00" not in report


def test_from_config_uses_log_tz_for_display() -> None:
    from zoneinfo import ZoneInfo

    cfg = watch_config(Path("/tmp/x"), slack=True, log_tz="America/Los_Angeles")

    notifier = SlackNotifier.from_config(cfg)

    assert notifier._display_tz == ZoneInfo("America/Los_Angeles")


def test_deliver_status_posts_one_plain_dm() -> None:
    # feature 062 heartbeat: one DM, no threaded report (not a finding).
    web = FakeWebClient()

    result = _notifier(web).deliver_status("obi watch healthy — heartbeat")

    assert result == DeliveryResult(delivered=1, failed=0)
    assert len(web.parents()) == 1 and not web.replies()


def test_deliver_status_label_names_the_sending_path() -> None:
    # 095 follow-up: pager sends must not audit as "watch heartbeat" — the label rides through.
    from obi.core.audit import audit_scope

    class _Recorder:
        def __init__(self) -> None:
            self.events: list[tuple[str, str, dict]] = []

        def record(self, kind, **kw) -> None:  # noqa: ANN001, ANN003 - the AuditRecorder seam
            self.events.append((kind, kw["summary"], kw["payload"]))

    recorder = _Recorder()
    with audit_scope(recorder, "cli:dash-run", "cli-dash-run-test"):  # type: ignore[arg-type]
        _notifier(FakeWebClient()).deliver_status("🔴 dash: gw — down 2m", label="dash page")
        _notifier(FakeWebClient()).deliver_status("obi watch healthy")  # default keeps 062's label
    summaries = [summary for _kind, summary, _payload in recorder.events]
    assert "slack dash page" in summaries
    assert "slack watch heartbeat" in summaries
    titles = [payload["title"] for _kind, _summary, payload in recorder.events]
    assert titles == ["dash page", "watch heartbeat"]


def test_deliver_status_never_raises_on_failure() -> None:
    web = FakeWebClient(post_fail=True)

    result = _notifier(web).deliver_status("heartbeat")

    assert result.delivered == 0 and result.failed == 1  # returned, not raised (FR-007)


def test_threaded_report_is_rendered_as_slack_mrkdwn() -> None:
    # feature 082: the threaded full report is converted from markdown to Slack mrkdwn, so the
    # operator sees rendered bold/headings/bullets, never literal ** or leading #.
    web = FakeWebClient()

    _notifier(web).deliver([_finding()])

    body = "".join(post["text"] for post in web.replies())
    assert "**" not in body  # no literal double-asterisk bold
    assert "*Device:*" in body  # markdown `**Device:**` -> single-* mrkdwn bold
    assert "• " in body  # `- ` bullets -> Slack bullets
    assert not any(line.startswith("# ") for line in body.splitlines())  # no ATX heading marks


def test_summary_parent_is_left_as_authored_mrkdwn() -> None:
    # The hand-composed summary/digest parents are already valid mrkdwn (single-*) and must not be
    # altered by the report conversion (FR-006/FR-007).
    web = FakeWebClient()

    _notifier(web).deliver([_finding()])

    parent = web.parents()[0]["text"]
    assert "*ether1 flapping*" in parent  # unchanged single-* bold
    assert "**" not in parent
