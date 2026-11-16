"""The dash-pager paging transport (feature 102) — the ONLY automated path to a page.

Wraps the pager's Slack ``DeliverFn`` (contracts/dash-paging.md): since 106 the seam carries
structured :class:`~obi.core.dash.PagerEvent` decisions, Slack renders them via
``format_slack_page`` and — as the PRIMARY channel — drives the retry loop. This wrapper
delivers to Slack first and returns that result UNCHANGED, so ``run_pager``'s state machine
is byte-identical with paging on or off. The page is additive and best-effort: one attempt
per NEW event to the statically configured person IDs, **deduped on the stable
``event_id``** exactly as the 106 pager-events contract requires of secondary channels — a
Slack outage re-emits the same decision every cycle, and the human must not be re-paged for
it. A page failure never alters the Slack outcome and is not retried once Slack has
delivered (the paging system offers no delivery confirmation); the first failure in a pass
reports ``paging failed: …`` over the Slack path itself. A hard sliding-hour cap, persisted
beside the pager state (restart-safe), suppresses (audited, never queued) while Slack
carries on uncapped. Everything here is deterministic — event text from ``run_pager``
(pure code), recipients from static config — no model output can reach a pager.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import TYPE_CHECKING

from obi.core.dash import DeliverFn, PagerEvent, format_slack_page, load_json, write_json_atomic
from obi.paging.client import PagingClient, record_page

if TYPE_CHECKING:
    from obi.core.config import PagingSettings

CAP_STATE_FILE = "paging-cap.json"
_HOUR_US = 3_600_000_000
#: Paged-event ids are remembered this long (their ids embed the incident's open
#: timestamp, so pruning can never collide a future incident with a forgotten one).
_DEDUP_TTL_US = 24 * _HOUR_US

#: ASCII folds for the Slack rendering's glyphs (pages are plain ASCII; charset support on
#: physical pagers is unknown, so everything non-ASCII is folded or dropped).
_FOLDS = {"🔴": "DOWN", "✅": "OK", "—": "-", "–": "-", "→": ":", "↔": "-", "…": "..."}


def page_text(event: PagerEvent, *, max_chars: int, now: datetime, tz: tzinfo = UTC) -> str:
    """Render one pager event as plain-ASCII page text, bounded by ``max_chars``.

    Derived from the canonical Slack rendering so both channels tell the same story:
    ``🔴 dash: X - d`` → ``obi DOWN X - d @HH:MM`` (the ``dash:`` prefix is dropped — "obi"
    already names the source, and pager screens are small). Truncation keeps the head — the
    identity lives at the front — dropping the time stamp first, then cutting the body with
    a ``...`` tail. No priority prefix (deliberate operator decision, clarification #3).
    """

    body = format_slack_page(event).strip()
    for glyph, fold in _FOLDS.items():
        body = body.replace(glyph, fold)
    body = body.replace("dash: ", "", 1)
    body = "obi " + " ".join(body.split())  # collapse doubled spaces left by folds
    body = body.encode("ascii", "ignore").decode()
    stamp = f" @{now.astimezone(tz).strftime('%H:%M')}"
    if len(body) + len(stamp) <= max_chars:
        return body + stamp
    if len(body) <= max_chars:
        return body
    return body[: max_chars - 3] + "..."


def _load_state(path: Path) -> tuple[list[int], dict[str, int]]:
    """(send timestamps µs, paged event_id → paged-at µs); corrupt/missing reads as empty."""

    raw = load_json(path)
    sent = raw.get("sent_us") if raw else None
    paged = raw.get("paged") if raw else None
    sent_us = (
        [int(v) for v in sent if isinstance(v, int | float)] if isinstance(sent, list) else []
    )
    paged_ids = (
        {str(k): int(v) for k, v in paged.items() if isinstance(v, int | float)}
        if isinstance(paged, dict)
        else {}
    )
    return sent_us, paged_ids


def wrap_deliver(
    slack_deliver: DeliverFn,
    settings: PagingSettings,
    *,
    state_dir: Path,
    notify: Callable[[str], None] | None = None,
    client: PagingClient | None = None,
    now_us: Callable[[], int] | None = None,
    now: Callable[[], datetime] | None = None,
    tz: tzinfo = UTC,
) -> DeliverFn:
    """Compose paging onto the pager's Slack transport. Never raises, never alters Slack.

    ``notify`` is the plain-text status channel for the once-per-pass ``paging failed:``
    notice (the CLI passes the same Slack status path the pager lines ride).
    """

    alerts = settings.alerts
    paging_client = client or PagingClient(settings)
    clock_us = now_us or (lambda: time.time_ns() // 1_000)
    clock = now or (lambda: datetime.now(UTC))
    cap_path = state_dir / CAP_STATE_FILE
    notified_failure = False  # one "paging failed" Slack line per pass

    def deliver(event: PagerEvent) -> tuple[bool, str | None]:
        ok, error = slack_deliver(event)
        # Containment backstop: a paging bug must never break the cycle or the Slack result.
        with contextlib.suppress(Exception):
            _page(event)
        return ok, error

    def _page(event: PagerEvent) -> None:
        nonlocal notified_failure
        tick = clock_us()
        sent_us, paged_ids = _load_state(cap_path)
        sent_us = [t for t in sent_us if t > tick - _HOUR_US]
        paged_ids = {k: v for k, v in paged_ids.items() if v > tick - _DEDUP_TTL_US}
        if event.event_id in paged_ids:
            # A re-emission (Slack retrying this same decision) — the human was already
            # paged for it; secondary channels dedup on the stable id (106 contract).
            write_json_atomic(cap_path, {"sent_us": sent_us, "paged": paged_ids})
            return
        body = page_text(event, max_chars=settings.message_max_chars, now=clock(), tz=tz)
        if len(sent_us) >= alerts.max_pages_per_hour:
            record_page(
                path="dash",
                people=alerts.people,
                groups=[],
                body=body,
                outcome="suppressed",
                error=f"hourly cap ({alerts.max_pages_per_hour}) reached",
            )
            write_json_atomic(cap_path, {"sent_us": sent_us, "paged": paged_ids})
            return
        result = paging_client.send_page(people=alerts.people, message=body)
        record_page(
            path="dash",
            people=alerts.people,
            groups=[],
            body=body,
            outcome="ok" if result.ok else "error",
            error=result.error,
        )
        if result.ok:
            sent_us.append(tick)
            paged_ids[event.event_id] = tick
        elif notify is not None and not notified_failure:
            notified_failure = True
            with contextlib.suppress(Exception):
                notify(f"paging failed: {result.error or 'unknown error'}")
        write_json_atomic(cap_path, {"sent_us": sent_us, "paged": paged_ids})

    return deliver
