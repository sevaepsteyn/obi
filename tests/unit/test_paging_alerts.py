"""Unit (feature 102): the dash-pager paging transport — rendering, dedup, cap, composition.

Post-106 the pager seam carries structured PagerEvents; the paging wrapper is a SECONDARY
channel under contracts/pager-events.md: Slack drives retries, paging dedups on the stable
event_id so a Slack outage's re-emissions never re-page a human.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from obi.core.audit import audit_scope
from obi.core.config import PagingAlertSettings, PagingSettings, Secret
from obi.core.dash import PagerEvent, PagerMember
from obi.paging.alerts import CAP_STATE_FILE, page_text, wrap_deliver
from obi.paging.client import PageResult

NOW = datetime(2026, 8, 26, 18, 45, tzinfo=UTC)
NOW_US = int(NOW.timestamp() * 1_000_000)


def _member(label: str = "host: gw", detail: str = "no reply 3m") -> PagerMember:
    return PagerMember(signal="fleet", key=f"fleet|{label}", label=label, detail=detail)


def _red(event_id: str = "item:fleet|gw:100", label: str = "gw", detail: str = "no reply 3m"):
    return PagerEvent(
        event_id=event_id,
        kind="red",
        scope="item",
        at_us=NOW_US,
        members=(PagerMember(signal="fleet", key=f"fleet|{label}", label=label, detail=detail),),
        signal="fleet",
    )


def _recovery(event_id: str = "item:fleet|gw:100:rec", label: str = "gw") -> PagerEvent:
    return PagerEvent(
        event_id=event_id,
        kind="recovery",
        scope="item",
        at_us=NOW_US,
        members=(PagerMember(signal="fleet", key=f"fleet|{label}", label=label, detail=""),),
        signal="fleet",
        down_s=240.0,
    )


def _incident() -> PagerEvent:
    return PagerEvent(
        event_id="link:magnolia~noc~60:100",
        kind="red",
        scope="incident",
        at_us=NOW_US,
        members=(
            PagerMember(
                signal="netwatch", key="netwatch|a", label="magnolia-gw: noc-60", detail="loss"
            ),
        ),
        sites=("magnolia", "noc"),
        band="60",
    )


def _settings(**alert_overrides: object) -> PagingSettings:
    alerts = {"enabled": True, "people": ["101"], "max_pages_per_hour": 6}
    alerts.update(alert_overrides)
    return PagingSettings(
        username="mira",
        password=Secret("pw"),
        alerts=PagingAlertSettings(**alerts),  # type: ignore[arg-type]
    )


class FakeClient:
    """Stands in for PagingClient.send_page; scripts ok/fail per call."""

    def __init__(self, *results: PageResult) -> None:
        self.results = list(results)
        self.calls: list[tuple[list[str], str]] = []

    def send_page(self, *, people, groups=(), message):  # noqa: ANN001, ANN003, ANN202
        self.calls.append((list(people), message))
        return self.results.pop(0) if self.results else PageResult(ok=True, status=200)


class Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    def record(self, kind, **kw) -> None:  # noqa: ANN001, ANN003 - the AuditRecorder seam
        self.events.append((kind, kw["summary"], kw["payload"]))


class SlackLog:
    """The primary channel: records delivered events + plain-text notices."""

    def __init__(self, ok: bool = True, error: str | None = None) -> None:
        self.events: list[PagerEvent] = []
        self.notices: list[str] = []
        self.reply = (ok, error)

    def __call__(self, event: PagerEvent) -> tuple[bool, str | None]:
        self.events.append(event)
        return self.reply

    def notify(self, text: str) -> None:
        self.notices.append(text)


def _wrap(slack: SlackLog, client: FakeClient, state_dir: Path, settings=None):  # noqa: ANN001, ANN202
    return wrap_deliver(
        slack,
        settings or _settings(),
        state_dir=state_dir,
        notify=slack.notify,
        client=client,  # type: ignore[arg-type]
        now_us=lambda: NOW_US,
        now=lambda: NOW,
    )


# --- rendering ---------------------------------------------------------------------------------


def test_page_text_renders_red_recovery_and_incident_plain_ascii() -> None:
    red = _red(label="noc-ptp-manzanita-60", detail="no reply 3m")
    down = page_text(red, max_chars=200, now=NOW)
    assert down == "obi DOWN host: noc-ptp-manzanita-60 - no reply 3m @18:45"
    ok = page_text(_recovery(label="gw"), max_chars=200, now=NOW)
    assert ok == "obi OK host: gw recovered (down 4m) @18:45"
    incident = page_text(_incident(), max_chars=200, now=NOW)
    assert (
        incident == "obi DOWN link magnolia-noc (60): netwatch: magnolia-gw: noc-60 - loss @18:45"
    )
    assert down.isascii() and ok.isascii() and incident.isascii()


def test_page_text_stamp_uses_display_tz() -> None:
    pacific = timezone(timedelta(hours=-7))
    assert page_text(_red(), max_chars=200, now=NOW, tz=pacific).endswith("@11:45")


def test_page_text_truncation_drops_stamp_then_cuts_head_preserving() -> None:
    # "obi DOWN host: " prefix is 15 chars; a 35-char label ⇒ 50-char body: stamp dropped.
    body_only = page_text(_red(label="x" * 35, detail=""), max_chars=50, now=NOW)
    assert body_only == "obi DOWN host: " + "x" * 35 and len(body_only) == 50
    cut = page_text(_red(label="gw-with-a-name", detail="d" * 300), max_chars=60, now=NOW)
    assert len(cut) == 60
    assert cut.startswith("obi DOWN host: gw-with-a-name") and cut.endswith("...")


# --- composition -------------------------------------------------------------------------------


def test_slack_result_returned_unchanged_and_page_sent(tmp_path: Path) -> None:
    slack = SlackLog()
    client = FakeClient()
    deliver = _wrap(slack, client, tmp_path)
    event = _red()
    assert deliver(event) == (True, None)
    assert slack.events == [event]
    assert client.calls == [(["101"], "obi DOWN host: gw - no reply 3m @18:45")]


def test_slack_retries_of_the_same_event_never_re_page(tmp_path: Path) -> None:
    # The 106 contract's core demand on secondary channels: dedup on the stable event_id.
    slack = SlackLog(ok=False, error="slack down")
    client = FakeClient()
    deliver = _wrap(slack, client, tmp_path)
    event = _red()
    assert deliver(event) == (False, "slack down")  # cycle 1: page goes out
    assert deliver(event) == (False, "slack down")  # cycles 2-3: Slack retry re-emissions
    assert deliver(event) == (False, "slack down")
    assert len(client.calls) == 1  # ONE page for one incident, despite three emissions
    assert len(slack.events) == 3  # Slack kept retrying (its own loop, untouched)


def test_dedup_state_survives_restart(tmp_path: Path) -> None:
    event = _red()
    deliver = _wrap(SlackLog(), FakeClient(), tmp_path)
    deliver(event)
    client2 = FakeClient()
    deliver2 = _wrap(SlackLog(), client2, tmp_path)  # a fresh dash cycle process
    deliver2(event)
    assert client2.calls == []  # remembered across processes via paging-cap.json


def test_recovery_is_a_distinct_event_and_pages_once(tmp_path: Path) -> None:
    client = FakeClient()
    deliver = _wrap(SlackLog(), client, tmp_path)
    deliver(_red())
    deliver(_recovery())
    assert len(client.calls) == 2
    assert "recovered" in client.calls[1][1]


def test_page_failure_never_alters_slack_result_and_notifies_once(tmp_path: Path) -> None:
    slack = SlackLog()
    client = FakeClient(
        PageResult(ok=False, error="connection failed: no route"),
        PageResult(ok=False, error="connection failed: no route"),
    )
    deliver = _wrap(slack, client, tmp_path)
    assert deliver(_red("item:a:1", label="a")) == (True, None)  # Slack outcome untouched
    assert deliver(_red("item:b:1", label="b")) == (True, None)
    assert slack.notices == ["paging failed: connection failed: no route"]  # once per pass
    assert len(client.calls) == 2  # a failed page is not marked paged (no false dedup)


# --- hourly cap --------------------------------------------------------------------------------


def test_cap_suppresses_at_max_and_audits(tmp_path: Path) -> None:
    recorder = Recorder()
    slack = SlackLog()
    client = FakeClient()
    deliver = _wrap(slack, client, tmp_path, _settings(max_pages_per_hour=2))
    with audit_scope(recorder, "cli:dash-run", "test"):  # type: ignore[arg-type]
        deliver(_red("item:a:1", label="a"))
        deliver(_red("item:b:1", label="b"))
        deliver(_red("item:c:1", label="c"))
    assert len(client.calls) == 2  # third page suppressed
    summaries = [s for _k, s, _p in recorder.events]
    assert summaries == ["page sent", "page sent", "page suppressed (hourly cap)"]
    assert all(p["path"] == "dash" for _k, _s, p in recorder.events)
    assert slack.notices == []  # suppression is not a failure — no notice
    state = json.loads((tmp_path / CAP_STATE_FILE).read_text())
    assert len(state["sent_us"]) == 2 and len(state["paged"]) == 2


def test_cap_window_slides_and_stale_dedup_ids_prune(tmp_path: Path) -> None:
    stale = NOW_US - 3_700_000_000  # >1h ago: outside the cap window
    recent = NOW_US - 60_000_000
    ancient = NOW_US - 25 * 3_600_000_000  # >24h: dedup id forgotten
    (tmp_path / CAP_STATE_FILE).write_text(
        json.dumps({"sent_us": [stale, recent], "paged": {"item:old:1": ancient}})
    )
    client = FakeClient()
    deliver = _wrap(SlackLog(), client, tmp_path, _settings(max_pages_per_hour=2))
    deliver(_red("item:a:1", label="a"))  # stale cap entry pruned ⇒ still under cap
    assert len(client.calls) == 1
    state = json.loads((tmp_path / CAP_STATE_FILE).read_text())
    assert stale not in state["sent_us"] and recent in state["sent_us"]
    assert "item:old:1" not in state["paged"]


def test_corrupt_cap_state_reads_as_empty(tmp_path: Path) -> None:
    (tmp_path / CAP_STATE_FILE).write_text("{not json")
    client = FakeClient()
    deliver = _wrap(SlackLog(), client, tmp_path)
    deliver(_red())
    assert len(client.calls) == 1


def test_wrapper_never_raises_even_if_paging_blows_up(tmp_path: Path) -> None:
    class ExplodingClient:
        def send_page(self, **kw):  # noqa: ANN003, ANN202
            raise RuntimeError("bug")

    slack = SlackLog()
    deliver = wrap_deliver(
        slack,
        _settings(),
        state_dir=tmp_path,
        notify=slack.notify,
        client=ExplodingClient(),  # type: ignore[arg-type]
        now_us=lambda: NOW_US,
        now=lambda: NOW,
    )
    assert deliver(_red()) == (True, None)  # Slack outcome survives the bug
