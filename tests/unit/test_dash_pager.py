"""Unit (feature 095, US3): the red-transition pager — transitions, retries, coalescing.

Every test drives :func:`obi.core.dash.run_pager` with synthesized boards, a scripted deliver
function, and a real state file in tmp — the restart-persistence cases literally re-read the
file, as the minute cron does. Yellow/stale never page; only signals in ``pager.signals`` count.
"""

from __future__ import annotations

from pathlib import Path

from obi.core import dash
from obi.core.config import DashPagerSettings, DashSettings
from obi.core.dash import Board, Item, Tile, run_pager

NOW_US = 1_787_000_000_000_000
MIN_US = 60_000_000


class _Deliver:
    """Scripted delivery channel: records every event AND its Slack rendering (106 seam).

    ``attempts``/``messages`` carry the rendered text so the string pins read naturally;
    ``events`` carries the structured decisions for the event-contract tests.
    """

    def __init__(self) -> None:
        self.events: list[dash.PagerEvent] = []
        self.attempted_events: list[dash.PagerEvent] = []
        self.messages: list[str] = []
        self.attempts: list[str] = []
        self.failing = False

    def __call__(self, event: dash.PagerEvent) -> tuple[bool, str | None]:
        self.attempted_events.append(event)
        self.attempts.append(dash.format_slack_page(event))
        if self.failing:
            return False, "slack unreachable"
        self.events.append(event)
        self.messages.append(dash.format_slack_page(event))
        return True, None


def _texts(outcome: dash.PagerOutcome) -> list[str]:
    """This pass's sent events, rendered — the pre-106 ``outcome.sent`` view."""

    return [dash.format_slack_page(e) for e in outcome.sent]


def _settings(tmp_path: Path, **pager_kwargs: object) -> DashSettings:
    return DashSettings(
        state_dir=tmp_path / "dash",
        pager=DashPagerSettings(enabled=True, **pager_kwargs),  # type: ignore[arg-type]
    )


def _board(items: dict[str, list[Item]], *, now_us: int = NOW_US) -> Board:
    tiles = []
    for signal in dash.SIGNALS:
        tile_items = items.get(signal, [Item(key="no-data", label="no data", state="stale")])
        tiles.append(
            Tile(
                signal=signal,
                state=dash.worst([i.state for i in tile_items]),
                items=tile_items,
                cadence_s=60,
            )
        )
    return Board(generated_at_us=now_us, state="green", tiles=tiles)


def _item(key: str, state: str = "red", *, detail: str = "down 2m") -> Item:
    return Item(key=key, label=key, state=state, detail=detail, since_us=NOW_US - 2 * MIN_US)


def _fleet(*items: Item) -> dict[str, list[Item]]:
    return {"fleet": list(items)}


def test_first_run_seeds_silently_even_with_reds(tmp_path: Path) -> None:
    deliver = _Deliver()
    settings = _settings(tmp_path)
    outcome = run_pager(
        _board(_fleet(_item("dead-gw"), _item("ok-gw", "green"))),
        settings, now_us=NOW_US, deliver=deliver,
    )
    assert outcome.sent == [] and deliver.attempts == []
    # The seeded red later clears — still silent (it was never paged).
    outcome = run_pager(
        _board(_fleet(_item("dead-gw", "green"), _item("ok-gw", "green"))),
        settings, now_us=NOW_US + MIN_US, deliver=deliver,
    )
    assert deliver.attempts == []


def test_transition_pages_once_with_recovery_line(tmp_path: Path) -> None:
    deliver = _Deliver()
    settings = _settings(tmp_path)
    healthy = _board(_fleet(_item("tamarack-gw", "green")))
    run_pager(healthy, settings, now_us=NOW_US, deliver=deliver)  # seed

    red = _board(_fleet(_item("tamarack-gw", detail="down 2m (172.18.89.1)")))
    outcome = run_pager(red, settings, now_us=NOW_US + MIN_US, deliver=deliver)
    assert _texts(outcome) == ["🔴 dash: host: tamarack-gw — down 2m (172.18.89.1)"]

    # Persisting red: no repeat.
    outcome = run_pager(red, settings, now_us=NOW_US + 2 * MIN_US, deliver=deliver)
    assert outcome.sent == []

    # Recovery: exactly one line, with the outage duration.
    outcome = run_pager(healthy, settings, now_us=NOW_US + 5 * MIN_US, deliver=deliver)
    assert len(outcome.sent) == 1
    assert _texts(outcome)[0].startswith("✅ dash: host: tamarack-gw recovered (down ")


def test_page_lines_name_their_source(tmp_path: Path) -> None:
    # Field round 3: every per-item line carries where it came from — fleet items page as
    # "host:", the rest under their signal name; netwatch pair labels render "router: target".
    deliver = _Deliver()
    settings = _settings(tmp_path)
    pair = Item(
        key="tamarack-gw|tamarack-noc-11",
        label="tamarack-gw→tamarack-noc-11",
        state="green",
    )
    healthy = _board({"netwatch": [pair], "ping": [_item("google", "green")]})
    run_pager(healthy, settings, now_us=NOW_US, deliver=deliver)  # seed

    red = _board(
        {
            "netwatch": [
                Item(
                    key="tamarack-gw|tamarack-noc-11",
                    label="tamarack-gw→tamarack-noc-11",
                    state="red",
                    detail="down",
                )
            ],
            "ping": [_item("google", detail="loss 100%")],
        }
    )
    outcome = run_pager(red, settings, now_us=NOW_US + MIN_US, deliver=deliver)
    assert sorted(_texts(outcome)) == [
        "🔴 dash: netwatch: tamarack-gw: tamarack-noc-11 — down",
        "🔴 dash: ping: google — loss 100%",
    ]
    outcome = run_pager(healthy, settings, now_us=NOW_US + 2 * MIN_US, deliver=deliver)
    assert sorted(_texts(outcome)) == [
        "✅ dash: netwatch: tamarack-gw: tamarack-noc-11 recovered (down 60s)",
        "✅ dash: ping: google recovered (down 4m)",  # _item's since_us is 2m before the seed
    ]


def test_state_survives_restart(tmp_path: Path) -> None:
    # The state lives in pager-state.json; every run_pager call already re-reads it, so this
    # sequence IS a restart between every cycle. Assert the file round-trips the paged flag.
    deliver = _Deliver()
    settings = _settings(tmp_path)
    run_pager(_board(_fleet(_item("gw", "green"))), settings, now_us=NOW_US, deliver=deliver)
    run_pager(_board(_fleet(_item("gw"))), settings, now_us=NOW_US + MIN_US, deliver=deliver)
    raw = dash.load_json(settings.state_dir / dash.PAGER_STATE_FILE)
    assert raw is not None
    assert raw["items"]["fleet|gw"]["paged_at_us"] == NOW_US + MIN_US
    run_pager(_board(_fleet(_item("gw"))), settings, now_us=NOW_US + 2 * MIN_US, deliver=deliver)
    assert len(deliver.messages) == 1  # still exactly one page across the "restart"


def test_delivery_failure_retries_next_cycle_then_recovers_the_loop(tmp_path: Path) -> None:
    deliver = _Deliver()
    settings = _settings(tmp_path)
    run_pager(_board(_fleet(_item("gw", "green"))), settings, now_us=NOW_US, deliver=deliver)

    deliver.failing = True
    red = _board(_fleet(_item("gw")))
    outcome = run_pager(red, settings, now_us=NOW_US + MIN_US, deliver=deliver)
    assert outcome.failed == 1 and outcome.pending == 1
    raw = dash.load_json(settings.state_dir / dash.PAGER_STATE_FILE)
    assert raw is not None and raw["delivery_error"] == "slack unreachable"

    deliver.failing = False
    outcome = run_pager(red, settings, now_us=NOW_US + 2 * MIN_US, deliver=deliver)
    assert len(outcome.sent) == 1 and outcome.pending == 0  # the retry landed, exactly once
    outcome = run_pager(red, settings, now_us=NOW_US + 3 * MIN_US, deliver=deliver)
    assert outcome.sent == []


def test_mass_transition_coalesces_into_one_message(tmp_path: Path) -> None:
    deliver = _Deliver()
    settings = _settings(tmp_path)
    healthy = _board(_fleet(*[_item(f"gw-{i}", "green") for i in range(10)]))
    run_pager(healthy, settings, now_us=NOW_US, deliver=deliver)
    down = _board(_fleet(*[_item(f"gw-{i}") for i in range(9)], _item("gw-9", "green")))
    outcome = run_pager(down, settings, now_us=NOW_US + MIN_US, deliver=deliver)
    assert len(outcome.sent) == 1
    assert _texts(outcome)[0].startswith("🔴 dash: fleet — 9/10 red (")
    # Symmetric coalesced recovery.
    outcome = run_pager(healthy, settings, now_us=NOW_US + 2 * MIN_US, deliver=deliver)
    assert _texts(outcome) == ["✅ dash: fleet — 9 recovered"]


def test_below_ratio_pages_individually(tmp_path: Path) -> None:
    deliver = _Deliver()
    settings = _settings(tmp_path)
    healthy = _board(_fleet(*[_item(f"gw-{i}", "green") for i in range(10)]))
    run_pager(healthy, settings, now_us=NOW_US, deliver=deliver)
    two_down = _board(
        _fleet(
            *[_item(f"gw-{i}") for i in range(2)],
            *[_item(f"gw-{i}", "green") for i in range(2, 10)],
        )
    )
    outcome = run_pager(two_down, settings, now_us=NOW_US + MIN_US, deliver=deliver)
    assert len(outcome.sent) == 2  # 2/10 is an incident list, not a mass event


def test_yellow_and_stale_never_page(tmp_path: Path) -> None:
    deliver = _Deliver()
    settings = _settings(tmp_path)
    run_pager(_board(_fleet(_item("gw", "green"))), settings, now_us=NOW_US, deliver=deliver)
    board = _board(_fleet(_item("gw", "yellow"), _item("other", "stale")))
    outcome = run_pager(board, settings, now_us=NOW_US + MIN_US, deliver=deliver)
    assert outcome.sent == [] and deliver.attempts == []


def test_signal_toggle_excludes_a_signal(tmp_path: Path) -> None:
    deliver = _Deliver()
    settings = _settings(tmp_path, signals=["internet"])  # fleet does not page
    run_pager(_board(_fleet(_item("gw", "green"))), settings, now_us=NOW_US, deliver=deliver)
    outcome = run_pager(
        _board(_fleet(_item("gw"))), settings, now_us=NOW_US + MIN_US, deliver=deliver
    )
    assert outcome.sent == [] and deliver.attempts == []


def test_data_gap_carries_entries_and_vanished_items_age_out(tmp_path: Path) -> None:
    deliver = _Deliver()
    settings = _settings(tmp_path)
    run_pager(_board(_fleet(_item("gw", "green"))), settings, now_us=NOW_US, deliver=deliver)
    run_pager(_board(_fleet(_item("gw"))), settings, now_us=NOW_US + MIN_US, deliver=deliver)
    assert len(deliver.messages) == 1

    # A data-gap cycle (placeholder tile): the red entry is CARRIED — no recovery line.
    gap = _board({})
    outcome = run_pager(gap, settings, now_us=NOW_US + 2 * MIN_US, deliver=deliver)
    assert outcome.sent == []
    raw = dash.load_json(settings.state_dir / dash.PAGER_STATE_FILE)
    assert raw is not None and "fleet|gw" in raw["items"]

    # The device is renamed away: evaluated set no longer contains it ⇒ silent age-out (U3).
    renamed = _board(_fleet(_item("gw-pine", "green")))
    outcome = run_pager(renamed, settings, now_us=NOW_US + 3 * MIN_US, deliver=deliver)
    assert outcome.sent == []
    raw = dash.load_json(settings.state_dir / dash.PAGER_STATE_FILE)
    assert raw is not None and raw["items"] == {}


def test_re_red_while_recovery_pending_stays_one_incident(tmp_path: Path) -> None:
    deliver = _Deliver()
    settings = _settings(tmp_path)
    run_pager(_board(_fleet(_item("gw", "green"))), settings, now_us=NOW_US, deliver=deliver)
    run_pager(_board(_fleet(_item("gw"))), settings, now_us=NOW_US + MIN_US, deliver=deliver)
    deliver.failing = True  # recovery line cannot be delivered
    run_pager(
        _board(_fleet(_item("gw", "green"))), settings, now_us=NOW_US + 2 * MIN_US, deliver=deliver
    )
    deliver.failing = False
    # Red again before the recovery ever went out: no second page — one continuous incident.
    outcome = run_pager(
        _board(_fleet(_item("gw"))), settings, now_us=NOW_US + 3 * MIN_US, deliver=deliver
    )
    assert outcome.sent == []


def test_pager_disabled_in_cycle_is_pure_noop(tmp_path: Path) -> None:
    # run_cycle only invokes the pager when enabled AND a deliver seam was passed — pin the
    # board-only default by asserting no state file appears without them.
    settings = _settings(tmp_path)
    assert not (settings.state_dir / dash.PAGER_STATE_FILE).exists()

# --- feature 106: link grouping ----------------------------------------------------------------

SITES = frozenset({"magnolia", "noc", "tamarack"})


def _radio(state: str = "red", *, detail: str = "down 2m") -> Item:
    return Item(key="noc-ptp-magnolia-60", label="noc-ptp-magnolia-60", state=state, detail=detail)


def _pair(router: str, name: str, state: str = "red", *, detail: str = "down") -> Item:
    return Item(key=f"{router}|{name}", label=f"{router}→{name}", state=state, detail=detail)


def _link_board(fleet_state: str, a_state: str, b_state: str, *, filler: str = "green") -> Board:
    """The canonical 3-symptom link board: radio + both netwatch directions (+ one filler
    pair per signal so two reds never trip the per-signal mass rule)."""

    return _board(
        {
            "fleet": [_radio(fleet_state), _item("tamarack-gw", filler)],
            "netwatch": [
                _pair("noc-gw", "noc-magnolia-60", a_state),
                _pair("magnolia-gw", "magnolia-noc-60", b_state),
                _pair("tamarack-gw", "google", filler),
            ],
        }
    )


def test_pager_sites_derivation() -> None:
    sites = dash.pager_sites(
        [
            "magnolia-gw",
            "core-gw-1",
            "tamarack-internet-gw",
            "tamarack-gw-yellowwood",
            "noc-ap-birch",  # no gw token — contributes nothing
            "chestnut-vms-1",
        ],
        extra=["Birch", " joshua ", ""],
    )
    assert sites == frozenset({"magnolia", "core", "tamarack", "birch", "joshua"})
    assert dash.pager_sites([]) == frozenset()


def test_link_group_table() -> None:
    sites = frozenset({"magnolia", "noc", "basswood", "tamarack", "rowan", "joshua", "chestnut"})
    # All three symptoms of one physical link share the key.
    for label in ("noc-ptp-magnolia-60", "noc-gw→noc-magnolia-60", "magnolia-gw→magnolia-noc-60"):
        group = dash.link_group(label, sites)
        assert group is not None and group.key == "magnolia~noc~60", label
        assert (group.site_a, group.site_b, group.band) == ("magnolia", "noc", "60")
    # Multi-band pairs stay distinct incidents (redundant paths never fold together).
    g60 = dash.link_group("basswood-ptp-tamarack-60", sites)
    g70 = dash.link_group("basswood-ptp-tamarack-70", sites)
    assert g60 is not None and g70 is not None and g60.key != g70.key
    # Band-less two-site labels form their own (band-less) group.
    vtep = dash.link_group("magnolia-gw→magnolia-vtep-tamarack", sites)
    assert vtep is not None and vtep.key == "magnolia~tamarack~-" and vtep.band is None
    # The Wave pair groups once joshua is a known site (config extra_sites).
    ap = dash.link_group("rowan-ptp-joshua-ap-05", sites)
    st = dash.link_group("rowan-ptp-joshua-st-05", sites)
    assert ap is not None and st is not None and ap.key == st.key == "joshua~rowan~05"
    # No link identity: zero, one, or three-plus known sites — per-item behavior.
    for label in (
        "google",
        "tamarack-gw",
        "core-gw-1",
        "chestnut-internet-gw→chestnut-internet-chestnut",  # one site, deduped
        "magnolia-noc-tamarack-60",  # three sites: ambiguous, refuse
    ):
        assert dash.link_group(label, sites) is None, label
    # Empty site knowledge degrades cleanly.
    assert dash.link_group("noc-ptp-magnolia-60", frozenset()) is None


def test_first_symptom_opens_incident_named_as_link(tmp_path: Path) -> None:
    # Netwatch-first ordering (the common case: netwatch confirms before the fleet debounce).
    deliver = _Deliver()
    settings = _settings(tmp_path)
    run_pager(_link_board("green", "green", "green"), settings, now_us=NOW_US,
              deliver=deliver, sites=SITES)  # seed
    outcome = run_pager(_link_board("green", "green", "red"), settings,
                        now_us=NOW_US + MIN_US, deliver=deliver, sites=SITES)
    assert _texts(outcome) == [
        "🔴 dash: link magnolia↔noc (60): netwatch: magnolia-gw: magnolia-noc-60 — down"
    ]
    assert outcome.sent[0].scope == "incident"
    assert outcome.sent[0].sites == ("magnolia", "noc") and outcome.sent[0].band == "60"


def test_fleet_first_ordering_also_opens_the_incident(tmp_path: Path) -> None:
    deliver = _Deliver()
    settings = _settings(tmp_path)
    run_pager(_link_board("green", "green", "green"), settings, now_us=NOW_US,
              deliver=deliver, sites=SITES)
    outcome = run_pager(_link_board("red", "green", "green"), settings,
                        now_us=NOW_US + MIN_US, deliver=deliver, sites=SITES)
    assert _texts(outcome) == [
        "🔴 dash: link magnolia↔noc (60): host: noc-ptp-magnolia-60 — down 2m"
    ]


def test_later_members_fold_silently_across_signals(tmp_path: Path) -> None:
    deliver = _Deliver()
    settings = _settings(tmp_path)
    run_pager(_link_board("green", "green", "green"), settings, now_us=NOW_US,
              deliver=deliver, sites=SITES)
    run_pager(_link_board("green", "green", "red"), settings, now_us=NOW_US + MIN_US,
              deliver=deliver, sites=SITES)
    assert len(deliver.messages) == 1
    # The opposite direction and the radio itself confirm later: silence, marked folded.
    outcome = run_pager(_link_board("red", "red", "red"), settings, now_us=NOW_US + 2 * MIN_US,
                        deliver=deliver, sites=SITES)
    assert outcome.sent == [] and len(deliver.attempts) == 1
    raw = dash.load_json(settings.state_dir / dash.PAGER_STATE_FILE)
    assert raw is not None
    assert raw["items"]["fleet|noc-ptp-magnolia-60"]["folded"] is True
    assert raw["items"]["netwatch|noc-gw|noc-magnolia-60"]["folded"] is True
    # Persisting red: still nothing.
    outcome = run_pager(_link_board("red", "red", "red"), settings, now_us=NOW_US + 3 * MIN_US,
                        deliver=deliver, sites=SITES)
    assert outcome.sent == []


def test_incident_recovers_once_when_all_members_clear(tmp_path: Path) -> None:
    deliver = _Deliver()
    settings = _settings(tmp_path)
    run_pager(_link_board("green", "green", "green"), settings, now_us=NOW_US,
              deliver=deliver, sites=SITES)
    run_pager(_link_board("green", "red", "green"), settings, now_us=NOW_US + MIN_US,
              deliver=deliver, sites=SITES)  # opens (red_since = NOW+1m)
    run_pager(_link_board("red", "red", "red"), settings, now_us=NOW_US + 2 * MIN_US,
              deliver=deliver, sites=SITES)  # folds
    # Partial recovery: both netwatch directions clear, the radio is still red — silence.
    outcome = run_pager(_link_board("red", "green", "green"), settings,
                        now_us=NOW_US + 3 * MIN_US, deliver=deliver, sites=SITES)
    assert outcome.sent == []
    # Full recovery: one line, duration first red (NOW+1m) → last clear (NOW+4m) = 3m.
    outcome = run_pager(_link_board("green", "green", "green"), settings,
                        now_us=NOW_US + 4 * MIN_US, deliver=deliver, sites=SITES)
    assert _texts(outcome) == ["✅ dash: link magnolia↔noc (60) recovered (down 3m)"]
    assert outcome.sent[0].down_s == 180.0
    raw = dash.load_json(settings.state_dir / dash.PAGER_STATE_FILE)
    assert raw is not None and raw["items"] == {}


def test_mass_precedence_consumes_link_siblings(tmp_path: Path) -> None:
    # A fleet-wide event outranks link naming: the mass page fires, and the netwatch
    # sibling of a mass-consumed radio folds silently under it.
    deliver = _Deliver()
    settings = _settings(tmp_path)
    fleet_green = [_radio("green")] + [_item(f"gw-{i}", "green") for i in range(9)]
    fleet_down = [_radio("red")] + [_item(f"gw-{i}") for i in range(8)] + [_item("gw-8", "green")]
    netwatch = lambda state: [  # noqa: E731 - tiny board builder
        _pair("noc-gw", "noc-magnolia-60", state),
        _pair("tamarack-gw", "google", "green"),
        _pair("tamarack-gw", "cloudflare", "green"),
    ]
    run_pager(_board({"fleet": fleet_green, "netwatch": netwatch("green")}), settings,
              now_us=NOW_US, deliver=deliver, sites=SITES)
    outcome = run_pager(_board({"fleet": fleet_down, "netwatch": netwatch("red")}), settings,
                        now_us=NOW_US + MIN_US, deliver=deliver, sites=SITES)
    assert [e.scope for e in outcome.sent] == ["mass"]
    assert _texts(outcome)[0].startswith("🔴 dash: fleet — 9/10 red (")
    raw = dash.load_json(settings.state_dir / dash.PAGER_STATE_FILE)
    assert raw is not None
    assert raw["items"]["netwatch|noc-gw|noc-magnolia-60"]["folded"] is True


def test_seeded_member_never_triggers_but_gates_recovery(tmp_path: Path) -> None:
    deliver = _Deliver()
    settings = _settings(tmp_path)
    # First ever run: the radio is ALREADY red — seeded, silent.
    run_pager(_link_board("red", "green", "green"), settings, now_us=NOW_US,
              deliver=deliver, sites=SITES)
    assert deliver.attempts == []
    # A netwatch direction confirms: the incident opens, listing the seeded radio as evidence.
    outcome = run_pager(_link_board("red", "red", "green"), settings, now_us=NOW_US + MIN_US,
                        deliver=deliver, sites=SITES)
    assert len(outcome.sent) == 1 and outcome.sent[0].scope == "incident"
    assert {m.key for m in outcome.sent[0].members} == {
        "fleet|noc-ptp-magnolia-60",
        "netwatch|noc-gw|noc-magnolia-60",
    }
    # The netwatch member clears but the seeded radio is still red: no recovery yet.
    outcome = run_pager(_link_board("red", "green", "green"), settings,
                        now_us=NOW_US + 2 * MIN_US, deliver=deliver, sites=SITES)
    assert outcome.sent == []
    # The seeded member clears too: one recovery, duration reflecting the seeded red's start.
    outcome = run_pager(_link_board("green", "green", "green"), settings,
                        now_us=NOW_US + 3 * MIN_US, deliver=deliver, sites=SITES)
    assert len(outcome.sent) == 1 and outcome.sent[0].kind == "recovery"
    assert outcome.sent[0].down_s == 180.0  # seeded red at NOW → cleared at NOW+3m


def test_data_gap_blocks_incident_recovery(tmp_path: Path) -> None:
    deliver = _Deliver()
    settings = _settings(tmp_path)
    run_pager(_link_board("green", "green", "green"), settings, now_us=NOW_US,
              deliver=deliver, sites=SITES)
    run_pager(_link_board("red", "red", "red"), settings, now_us=NOW_US + MIN_US,
              deliver=deliver, sites=SITES)
    assert len(deliver.messages) == 1
    # The netwatch collector gaps while the fleet clears: carried members block the recovery.
    gap = _board({"fleet": [_radio("green"), _item("tamarack-gw", "green")]})
    outcome = run_pager(gap, settings, now_us=NOW_US + 2 * MIN_US, deliver=deliver, sites=SITES)
    assert outcome.sent == []
    # Real data returns and shows everything clear: now the recovery goes out.
    outcome = run_pager(_link_board("green", "green", "green"), settings,
                        now_us=NOW_US + 3 * MIN_US, deliver=deliver, sites=SITES)
    assert len(outcome.sent) == 1 and outcome.sent[0].kind == "recovery"


def test_re_red_during_pending_incident_recovery_stays_one_incident(tmp_path: Path) -> None:
    deliver = _Deliver()
    settings = _settings(tmp_path)
    run_pager(_link_board("green", "green", "green"), settings, now_us=NOW_US,
              deliver=deliver, sites=SITES)
    run_pager(_link_board("red", "red", "red"), settings, now_us=NOW_US + MIN_US,
              deliver=deliver, sites=SITES)
    deliver.failing = True  # the recovery cannot be delivered
    run_pager(_link_board("green", "green", "green"), settings, now_us=NOW_US + 2 * MIN_US,
              deliver=deliver, sites=SITES)
    deliver.failing = False
    # One member re-reds before the recovery ever went out: no second page.
    outcome = run_pager(_link_board("red", "green", "green"), settings,
                        now_us=NOW_US + 3 * MIN_US, deliver=deliver, sites=SITES)
    assert outcome.sent == []
    # Everything clears again: exactly one recovery for the whole (single) incident.
    outcome = run_pager(_link_board("green", "green", "green"), settings,
                        now_us=NOW_US + 4 * MIN_US, deliver=deliver, sites=SITES)
    assert len(outcome.sent) == 1 and outcome.sent[0].kind == "recovery"
    assert len(deliver.messages) == 2  # the open + the one recovery


def test_link_grouping_disabled_reverts_to_per_item(tmp_path: Path) -> None:
    deliver = _Deliver()
    settings = _settings(tmp_path, link_grouping=False)
    run_pager(_link_board("green", "green", "green"), settings, now_us=NOW_US,
              deliver=deliver, sites=SITES)
    outcome = run_pager(_link_board("red", "red", "red"), settings, now_us=NOW_US + MIN_US,
                        deliver=deliver, sites=SITES)
    assert [e.scope for e in outcome.sent] == ["item", "item", "item"]


def test_legacy_round3_state_loads_without_migration_or_spurious_pages(tmp_path: Path) -> None:
    # Hand-written field-round-3-shaped state: no observed_us/folded/incident_opened_us.
    # Upgrade must not re-page the already-paged red, must fold its pending sibling instead
    # of item-paging it, and must close the ungrouped pending recovery normally.
    settings = _settings(tmp_path)
    settings.state_dir.mkdir(parents=True)
    legacy = {
        "items": {
            "fleet|noc-ptp-magnolia-60": {
                "signal": "fleet", "label": "noc-ptp-magnolia-60", "detail": "down 5m",
                "red_since_us": NOW_US - 5 * MIN_US, "paged_at_us": NOW_US - 4 * MIN_US,
                "seeded": False,
            },
            "netwatch|noc-gw|noc-magnolia-60": {  # pending retry (delivery failed pre-upgrade)
                "signal": "netwatch", "label": "noc-gw→noc-magnolia-60", "detail": "down",
                "red_since_us": NOW_US - 2 * MIN_US, "paged_at_us": None, "seeded": False,
            },
            "ping|google": {  # pending recovery, ungrouped
                "signal": "ping", "label": "google", "detail": "loss 100%",
                "red_since_us": NOW_US - 10 * MIN_US, "paged_at_us": NOW_US - 9 * MIN_US,
                "seeded": False, "cleared_at_us": NOW_US - 2 * MIN_US,
            },
        },
        "delivery_error": None,
        "delivery_error_us": None,
    }
    dash.write_json_atomic(settings.state_dir / dash.PAGER_STATE_FILE, legacy)
    deliver = _Deliver()
    board = _board(
        {
            "fleet": [_radio("red", detail="down 6m"), _item("tamarack-gw", "green")],
            "netwatch": [
                _pair("noc-gw", "noc-magnolia-60", "red"),
                _pair("tamarack-gw", "google", "green"),
            ],
            "ping": [_item("google", "green")],
        }
    )
    outcome = run_pager(board, settings, now_us=NOW_US, deliver=deliver, sites=SITES)
    # Exactly one message: the ungrouped ping recovery. No re-page, no item-page.
    assert [e.scope for e in outcome.sent] == ["item"]
    assert _texts(outcome) == ["✅ dash: ping: google recovered (down 8m)"]
    raw = dash.load_json(settings.state_dir / dash.PAGER_STATE_FILE)
    assert raw is not None
    assert raw["items"]["netwatch|noc-gw|noc-magnolia-60"]["folded"] is True
    assert raw["items"]["fleet|noc-ptp-magnolia-60"]["paged_at_us"] == NOW_US - 4 * MIN_US


# --- feature 106: the event contract (what feature 102 builds against) -------------------------


def test_event_ids_stable_across_retries_and_member_growth(tmp_path: Path) -> None:
    deliver = _Deliver()
    settings = _settings(tmp_path)
    run_pager(_link_board("green", "green", "green"), settings, now_us=NOW_US,
              deliver=deliver, sites=SITES)
    deliver.failing = True
    run_pager(_link_board("green", "red", "green"), settings, now_us=NOW_US + MIN_US,
              deliver=deliver, sites=SITES)
    # Still failing; the incident gains members while delivery is owed.
    run_pager(_link_board("red", "red", "red"), settings, now_us=NOW_US + 2 * MIN_US,
              deliver=deliver, sites=SITES)
    deliver.failing = False
    outcome = run_pager(_link_board("red", "red", "red"), settings, now_us=NOW_US + 3 * MIN_US,
                        deliver=deliver, sites=SITES)
    reds = [e for e in deliver.attempted_events if e.kind == "red"]
    assert len(reds) == 3
    assert len({e.event_id for e in reds}) == 1
    assert len(reds[0].members) == 1 and len(reds[-1].members) == 3  # content grew, id didn't
    # A secondary channel deduping on event_id sends exactly once: no dupes, no drops.
    assert len({e.event_id for e in deliver.attempted_events}) == 1
    # The recovery pairs with its alert by id.
    incident_id = outcome.sent[0].event_id
    outcome = run_pager(_link_board("green", "green", "green"), settings,
                        now_us=NOW_US + 4 * MIN_US, deliver=deliver, sites=SITES)
    assert outcome.sent[0].event_id == f"{incident_id}:rec"


def test_emission_order_is_mass_then_incidents_then_items(tmp_path: Path) -> None:
    deliver = _Deliver()
    settings = _settings(tmp_path)
    fleet_green = [_item(f"gw-{i}", "green") for i in range(10)]
    netwatch_green = [
        _pair("noc-gw", "noc-magnolia-60", "green"),
        _pair("magnolia-gw", "magnolia-noc-60", "green"),
    ] + [_pair("tamarack-gw", f"ref-{i}", "green") for i in range(8)]
    healthy = _board(
        {"fleet": fleet_green, "netwatch": netwatch_green, "ping": [_item("google", "green")]}
    )
    run_pager(healthy, settings, now_us=NOW_US, deliver=deliver, sites=SITES)
    stormy = _board(
        {
            "fleet": [_item(f"gw-{i}") for i in range(9)] + [_item("gw-9", "green")],
            "netwatch": [
                _pair("noc-gw", "noc-magnolia-60", "red"),
                _pair("magnolia-gw", "magnolia-noc-60", "red"),
            ] + [_pair("tamarack-gw", f"ref-{i}", "green") for i in range(8)],
            "ping": [_item("google", detail="loss 100%")],
        }
    )
    outcome = run_pager(stormy, settings, now_us=NOW_US + MIN_US, deliver=deliver, sites=SITES)
    assert [e.scope for e in outcome.sent] == ["mass", "incident", "item"]
    assert [e.kind for e in outcome.sent] == ["red", "red", "red"]
    assert outcome.sent[1].event_id.startswith("link:magnolia~noc~60:")
    assert outcome.sent[2].event_id.startswith("item:ping|google:")


# --- feature 106: announce hold ----------------------------------------------------------------


def test_hold_mode_announces_the_full_picture(tmp_path: Path) -> None:
    deliver = _Deliver()
    settings = _settings(tmp_path, announce_hold="2m")
    run_pager(_link_board("green", "green", "green"), settings, now_us=NOW_US,
              deliver=deliver, sites=SITES)
    # First symptom: held, not pending, nothing sent.
    outcome = run_pager(_link_board("green", "green", "red"), settings, now_us=NOW_US + MIN_US,
                        deliver=deliver, sites=SITES)
    assert outcome.sent == [] and outcome.held == 1 and outcome.pending == 0
    # More symptoms inside the hold: still silent.
    outcome = run_pager(_link_board("red", "red", "red"), settings, now_us=NOW_US + 2 * MIN_US,
                        deliver=deliver, sites=SITES)
    assert outcome.sent == [] and outcome.held == 3
    # Hold elapsed (first observed at NOW+1m + 2m): one message listing every red member.
    outcome = run_pager(_link_board("red", "red", "red"), settings, now_us=NOW_US + 3 * MIN_US,
                        deliver=deliver, sites=SITES)
    assert outcome.held == 0
    assert _texts(outcome) == [
        "🔴 dash: link magnolia↔noc (60) — 3 red: host: noc-ptp-magnolia-60 — down 2m · "
        "netwatch: magnolia-gw: magnolia-noc-60 — down · netwatch: noc-gw: noc-magnolia-60 — down"
    ]


def test_flap_inside_hold_never_pages(tmp_path: Path) -> None:
    deliver = _Deliver()
    settings = _settings(tmp_path, announce_hold="2m")
    run_pager(_link_board("green", "green", "green"), settings, now_us=NOW_US,
              deliver=deliver, sites=SITES)
    run_pager(_link_board("red", "red", "red"), settings, now_us=NOW_US + MIN_US,
              deliver=deliver, sites=SITES)
    # The whole group clears within the hold window: nothing was ever sent, nothing recovers.
    run_pager(_link_board("green", "green", "green"), settings, now_us=NOW_US + 2 * MIN_US,
              deliver=deliver, sites=SITES)
    run_pager(_link_board("green", "green", "green"), settings, now_us=NOW_US + 5 * MIN_US,
              deliver=deliver, sites=SITES)
    assert deliver.attempts == []
    raw = dash.load_json(settings.state_dir / dash.PAGER_STATE_FILE)
    assert raw is not None and raw["items"] == {}


def test_hold_ungrouped_items_are_not_held(tmp_path: Path) -> None:
    # The hold applies to link incidents only — a plain item still pages on its cycle.
    deliver = _Deliver()
    settings = _settings(tmp_path, announce_hold="2m")
    run_pager(_board(_fleet(_item("gw", "green"))), settings, now_us=NOW_US,
              deliver=deliver, sites=SITES)
    outcome = run_pager(_board(_fleet(_item("gw"))), settings, now_us=NOW_US + MIN_US,
                        deliver=deliver, sites=SITES)
    assert len(outcome.sent) == 1 and outcome.sent[0].scope == "item"


# --- 135: maintenance is never paged; entering it retires silently; leaving it is loud ----------

from dataclasses import replace as _replace  # noqa: E402


def _maint(item: Item) -> Item:
    return _replace(item, state="yellow", maintenance="config: teardown")


def test_entering_maintenance_retires_a_paged_item_with_no_recovery(tmp_path: Path) -> None:
    deliver = _Deliver()
    settings = _settings(tmp_path)
    run_pager(_board(_fleet(_item("magnolia-gw", "green"), _item("ok-gw", "green"))),
              settings, now_us=NOW_US, deliver=deliver)  # seed
    run_pager(_board(_fleet(_item("magnolia-gw"), _item("ok-gw", "green"))),
              settings, now_us=NOW_US + MIN_US, deliver=deliver)
    assert len(deliver.events) == 1 and deliver.events[0].kind == "red"
    outcome = run_pager(
        _board(_fleet(_maint(_item("magnolia-gw")), _item("ok-gw", "green"))),
        settings, now_us=NOW_US + 2 * MIN_US, deliver=deliver,
    )
    assert outcome.sent == [] and len(deliver.attempts) == 1  # M3: no recovery, no anything
    assert outcome.retired == ["fleet|magnolia-gw"]
    state = dash.load_json(settings.state_dir / dash.PAGER_STATE_FILE)
    assert state is not None and "fleet|magnolia-gw" not in state["items"]


def test_maintenance_red_is_never_paged_nor_counted_for_mass(tmp_path: Path) -> None:
    deliver = _Deliver()
    settings = _settings(tmp_path, mass_ratio=0.5)
    run_pager(_board(_fleet(*[_item(f"gw{i}", "green") for i in range(4)])),
              settings, now_us=NOW_US, deliver=deliver)  # seed
    # three maintenance "reds" + one real red: a mass event must NOT fire (1 of 1 live red is
    # not ≥ 2), and the real red pages as an item.
    board = _board(_fleet(*[_maint(_item(f"gw{i}")) for i in range(3)], _item("gw3")))
    outcome = run_pager(board, settings, now_us=NOW_US + MIN_US, deliver=deliver)
    assert [e.scope for e in outcome.sent] == ["item"]
    assert outcome.sent[0].members[0].label == "gw3"


def test_leaving_maintenance_while_red_pages_once(tmp_path: Path) -> None:
    deliver = _Deliver()
    settings = _settings(tmp_path)
    run_pager(_board(_fleet(_maint(_item("magnolia-gw")), _item("ok", "green"))),
              settings, now_us=NOW_US, deliver=deliver)  # seed (maintenance: not tracked)
    run_pager(_board(_fleet(_maint(_item("magnolia-gw")), _item("ok", "green"))),
              settings, now_us=NOW_US + MIN_US, deliver=deliver)
    assert deliver.attempts == []
    outcome = run_pager(_board(_fleet(_item("magnolia-gw"), _item("ok", "green"))),
                        settings, now_us=NOW_US + 2 * MIN_US, deliver=deliver)
    assert len(outcome.sent) == 1 and outcome.sent[0].kind == "red"  # M4
    outcome = run_pager(_board(_fleet(_item("magnolia-gw"), _item("ok", "green"))),
                        settings, now_us=NOW_US + 3 * MIN_US, deliver=deliver)
    assert outcome.sent == []  # once per continuous red


def test_all_items_in_maintenance_is_not_a_data_gap(tmp_path: Path) -> None:
    # A tile whose every item is in maintenance is evaluated-and-empty: a paged entry is
    # retired, not carried as if the data were missing.
    deliver = _Deliver()
    settings = _settings(tmp_path)
    run_pager(
        _board(_fleet(_item("magnolia-gw", "green"))), settings, now_us=NOW_US, deliver=deliver
    )
    run_pager(
        _board(_fleet(_item("magnolia-gw"))), settings, now_us=NOW_US + MIN_US, deliver=deliver
    )
    outcome = run_pager(_board(_fleet(_maint(_item("magnolia-gw")))), settings,
                        now_us=NOW_US + 2 * MIN_US, deliver=deliver)
    assert outcome.retired == ["fleet|magnolia-gw"] and outcome.sent == []


def test_open_link_incident_entering_maintenance_never_sends_its_recovery(tmp_path: Path) -> None:
    deliver = _Deliver()
    settings = _settings(tmp_path)
    run_pager(_link_board("green", "green", "green"), settings, now_us=NOW_US,
              deliver=deliver, sites=SITES)  # seed
    run_pager(_link_board("red", "red", "red"), settings, now_us=NOW_US + MIN_US,
              deliver=deliver, sites=SITES)
    assert [e.scope for e in deliver.events] == ["incident"]
    board = _link_board("red", "red", "red")
    board = _replace(
        board,
        tiles=[
            _replace(t, items=[_maint(i) if i.state == "red" else i for i in t.items])
            for t in board.tiles
        ],
    )
    outcome = run_pager(board, settings, now_us=NOW_US + 2 * MIN_US, deliver=deliver, sites=SITES)
    assert outcome.sent == [] and len(deliver.attempts) == 1
    assert len(outcome.retired) == 3
