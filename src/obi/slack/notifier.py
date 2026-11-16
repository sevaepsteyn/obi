"""Outbound watch-alert delivery to Slack (feature 015).

A thin transport adapter over the cloud Slack Web API: given the watcher's qualifying findings, post
a direct message to the operator — one summary (+ the full report threaded) for a single finding, or
one digest for several (each report threaded). Outbound only: it uses ``chat.postMessage`` with the
**bot token** and needs no Socket Mode connection, no app token, and no running listener (contrast
:mod:`obi.slack.listener`, the inbound half). ``slack_sdk`` is imported lazily in
:meth:`SlackNotifier.from_config`, so importing this module never requires it.

Implements the core :class:`obi.core.watch.Notifier` seam and never raises — every failure is
returned in a :class:`~obi.core.watch.DeliveryResult` so a Slack problem can never break the
read-only watch cycle (FR-007). **Alerting is DM-only and never posts to a channel** (FR-003):
:meth:`~SlackNotifier.deliver` and :meth:`~SlackNotifier.deliver_status` can only reach the
configured operator. The single exception is :meth:`~SlackNotifier.deliver_status_to`, added by
feature 092 for `obi slack say --to`, where a human is typing the destination — no automated
path reaches it.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, tzinfo
from typing import Any

from obi.core.audit import record_event
from obi.core.config import Config
from obi.core.textfmt import to_slack_mrkdwn
from obi.core.watch import SEVERITIES, DeliveryResult, WatchFinding, render_markdown
from obi.slack.handler import _split_message

logger = logging.getLogger(__name__)


def _record_delivery(
    finding: WatchFinding, *, outcome: str, error: str | None, display_tz: tzinfo = UTC
) -> None:
    """Audit one finding's Slack delivery (feature 042) — rides the ambient watch scope.

    The audited body is rendered in the same ``display_tz`` (069) as the DM, so the audit trail
    matches exactly what the operator received.
    """

    record_event(
        "report",
        summary=f"slack report: {finding.summary}",
        payload={
            "channel": "slack",
            "report_id": finding.id,
            "severity": finding.severity,
            "title": finding.summary,
            "body": render_markdown(finding, display_tz=display_tz),
        },
        target=finding.device or None,
        outcome=outcome,
        error=error,
    )

def _record_status(
    text: str, *, outcome: str, error: str | None, label: str = "watch heartbeat"
) -> None:
    """Audit one plain status DM — the label names WHICH status path sent it.

    062's watcher heartbeat is the default; the 095 dash pager and 092's `say` pass their own
    labels so `obi audit` reads honestly (a page is not a heartbeat)."""

    record_event(
        "report",
        summary=f"slack {label}",
        payload={"channel": "slack", "title": label, "body": text},
        target=None,
        outcome=outcome,
        error=error,
    )


_SEVERITY_EMOJI = {
    "info": ":large_blue_circle:",
    "warning": ":warning:",
    "critical": ":rotating_light:",
}


def _emoji(severity: str) -> str:
    return _SEVERITY_EMOJI.get(severity, ":warning:")


def _rank(severity: str) -> int:
    try:
        return SEVERITIES.index(severity)
    except ValueError:
        return 0


def _summary(finding: WatchFinding, display_tz: tzinfo = UTC) -> str:
    """The scannable single-finding summary (FR-006); the full report goes in the thread.

    Window timestamps are shown in ``display_tz`` (069) — the operator's local zone.
    """

    addr = f" ({finding.device_address})" if finding.device_address else ""
    window = f"{finding.window_start.astimezone(display_tz).isoformat()} → " \
        f"{finding.window_end.astimezone(display_tz).isoformat()}"
    return "\n".join(
        [
            f"{_emoji(finding.severity)} *{finding.summary}*",
            f"*Device:* {finding.device}{addr}",
            f"*Window:* {window}",
            f"*Likely cause:* {finding.likely_cause or '—'}",
            f"*Manual fix:* {finding.suggested_remediation or '—'} _(obi made no change)_",
        ]
    )


def _digest(findings: list[WatchFinding]) -> str:
    """Combined header for 2+ findings (FR-016): one line each, worst severity first."""

    ordered = sorted(findings, key=lambda f: _rank(f.severity), reverse=True)
    lines = [f"{_emoji(ordered[0].severity)} *obi watch — {len(findings)} findings this cycle*"]
    lines += [f"{_emoji(f.severity)} {f.device} — {f.summary}" for f in ordered]
    return "\n".join(lines)


def _reason(exc: Exception) -> str:
    """A short, secret-free failure reason — Slack's error code when available, else the type."""

    response = getattr(exc, "response", None)
    if response is not None:
        try:
            err = response.get("error")  # slack_sdk SlackApiError carries response["error"]
        except AttributeError:
            err = None
        if err:
            return str(err)
    return type(exc).__name__


def _is_channel(target: str) -> bool:
    """Channel targets are ``#name`` or a raw C…/G… id; anything else is treated as a user."""

    return target.startswith("#") or bool(re.fullmatch(r"[CG][A-Z0-9]+", target))


class SlackNotifier:
    """Delivers watch findings to the operator as a Slack DM (the core ``Notifier`` seam)."""

    def __init__(self, web: Any, recipient: str, display_tz: tzinfo = UTC) -> None:
        self._web = web
        self._recipient = recipient
        self._display_tz = display_tz  # 069: the operator's log_tz for on-screen timestamps

    @classmethod
    def from_config(cls, config: Config, *, recipient: str | None = None) -> SlackNotifier:
        """Build from config: a bot-token ``WebClient`` + the resolved operator recipient.

        Uses ONLY the bot token (FR-002); the app token is never read. Recipient defaults to the
        first ``[slack]`` allowlist entry unless ``[watch] slack_user`` overrides it (FR-012);
        an explicit ``recipient`` (feature 088: ``[radio] slack_user``) overrides both.
        """

        if config.slack is None:  # pragma: no cover — CLI guards on slack_notify + load validation
            raise ValueError("SlackNotifier requires a [slack] config section")
        recipient = recipient or config.watch.slack_user or config.slack.allowlist[0]
        display_tz = config.logs.tzinfo() if config.logs is not None else UTC
        from slack_sdk.web import WebClient

        web = WebClient(token=config.slack.bot_token.reveal())
        return cls(web, recipient, display_tz)

    def deliver(self, findings: list[WatchFinding]) -> DeliveryResult:
        """Post the operator a DM (single or digest) + each finding's full report threaded.

        Never raises (FR-007): a failure to open the DM or post the parent fails the whole batch; a
        failed *threaded reply* (after the parent) is logged but does not reduce the delivered count
        — the operator already has the summary/digest and can pull the report from the CLI.
        """

        count = len(findings)
        if count == 0:  # the core never calls with an empty list; defensive
            return DeliveryResult(delivered=0, failed=0)

        tz = self._display_tz
        try:
            channel = self._open_dm()
            parent = _summary(findings[0], tz) if count == 1 else _digest(findings)
            thread_ts = self._post(channel, parent)
        except Exception as exc:
            # feature 042: a failed batch is still an auditable delivery attempt.
            for finding in findings:
                _record_delivery(finding, outcome="error", error=_reason(exc), display_tz=tz)
            return DeliveryResult(delivered=0, failed=count, error=_reason(exc))

        for finding in findings:
            # Render the report's markdown as Slack mrkdwn (feature 082) so the threaded body shows
            # bold/headings/bullets rendered, not literal ** / # . The .md file and audit body
            # (via render_markdown directly) stay canonical markdown.
            body = to_slack_mrkdwn(render_markdown(finding, display_tz=tz))
            for chunk in _split_message(body):
                try:
                    self._post(channel, chunk, thread_ts=thread_ts)
                except Exception:
                    logger.warning(
                        "slack threaded report post failed", extra={"finding": finding.id}
                    )
            _record_delivery(finding, outcome="ok", error=None, display_tz=tz)
        return DeliveryResult(delivered=count, failed=0)

    def deliver_status(self, text: str, *, label: str = "watch heartbeat") -> DeliveryResult:
        """Post the operator one plain status DM (the 062 heartbeat; 095 pages ride it too).

        Not a finding: no threaded report, no severity floor, no dedup. ``label`` names the
        sending path in the audit trail. Never raises (FR-007) — a failure comes back in the
        :class:`DeliveryResult` so it can never break the calling cycle."""

        try:
            channel = self._open_dm()
            self._post(channel, text)
        except Exception as exc:
            _record_status(text, outcome="error", error=_reason(exc), label=label)
            return DeliveryResult(delivered=0, failed=1, error=_reason(exc))
        _record_status(text, outcome="ok", error=None, label=label)
        return DeliveryResult(delivered=1, failed=0)

    def deliver_status_to(self, target: str, text: str) -> DeliveryResult:
        """Post one plain status message to an explicit target — a user DM or a channel.

        The operator's manual test path (feature 092, ``obi slack say --to``). A channel is
        posted to directly: ``conversations_open`` takes user ids, so routing a channel
        through the DM path would fail with a confusing ``channel_not_found``.

        Never raises, like its siblings — a failure comes back in the result.
        """

        try:
            channel = target if _is_channel(target) else self._open_dm_with(target)
            self._post(channel, text)
        except Exception as exc:
            _record_status(text, outcome="error", error=_reason(exc))
            return DeliveryResult(delivered=0, failed=1, error=_reason(exc))
        _record_status(text, outcome="ok", error=None)
        return DeliveryResult(delivered=1, failed=0)

    def _open_dm_with(self, user: str) -> str:
        response = self._web.conversations_open(users=[user])
        channel: str = response["channel"]["id"]
        return channel

    def _open_dm(self) -> str:
        response = self._web.conversations_open(users=[self._recipient])
        channel: str = response["channel"]["id"]
        return channel

    def _post(self, channel: str, text: str, thread_ts: str | None = None) -> str:
        response = self._web.chat_postMessage(channel=channel, text=text, thread_ts=thread_ts)
        ts: str = response["ts"]
        return ts
