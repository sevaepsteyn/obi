"""Unit (feature 111): the MeshLocator engine — policy, limiter, resolution, rendering.

Everything runs offline through the driver's injectable connection factory over the
2026-08-28 fixtures; the tests count transmissions on the fake, so "exactly one send" and
"nothing transmits on refusal" are pinned literally. The clock is injected: NOW is chosen
so the tracker's stored fix is fresh, TUMA was heard a minute ago, and the attached
radio's own fix sits slightly in the future (the clamp case).
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from obi.core import meshstore
from obi.core.audit import AuditTrail
from obi.core.brcaddr import CityModel
from obi.core.config import MeshtasticSettings
from obi.core.drivers.meshtastic import MeshDriver
from obi.core.mesh import MAX_ANSWER_CHARS, MeshLocator
from tests.fixtures.brc.origin import ORIGIN

_FIXTURES = Path(__file__).parent.parent / "fixtures" / "meshtastic"

#: Chosen so: tracker fix (…3616) is 284 s old (fresh), TUMA lastHeard (…3799) is 101 s
#: ago, and the self node's fix (…4606) is in the future (device clock skew — clamp).
_NOW = 1_787_983_900.0

_DIAL_ONLY_MODEL = CityModel(
    origin=ORIGIN, az12_deg=45.0, orientation_source="plaza-fit", segments=(), anchors=()
)


class _Connection:
    def __init__(self, position_reply: dict[str, Any] | None) -> None:
        raw = json.loads((_FIXTURES / "nodes.json").read_text())
        self.my_node_id = str(raw["myNodeId"])
        self.nodes: dict[str, dict[str, Any]] = dict(raw["nodes"])
        self.position_reply = position_reply
        self.position_requests: list[str] = []

    def read_nodes(self) -> tuple[str, dict[str, dict[str, Any]]]:
        return self.my_node_id, self.nodes

    def request_position(self, node_id: str, channel_index: int) -> dict[str, Any] | None:
        # 125: lookups are purely passive — reaching this from ANY locate path is a bug.
        raise AssertionError("a lookup transmitted (125: the engine is passive)")

    def close(self) -> None:
        pass


def _tuma_reply() -> dict[str, Any]:
    raw = json.loads((_FIXTURES / "position_response.json").read_text())
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def _locator(
    tmp_path: Path,
    *,
    reply: dict[str, Any] | None = None,
    settings: MeshtasticSettings | None = None,
    now: float = _NOW,
) -> tuple[MeshLocator, _Connection]:
    connection = _Connection(reply)
    driver = MeshDriver(
        settings or MeshtasticSettings(),
        connection_factory=lambda port, timeout: connection,
        find_ports=lambda: ["/dev/cu.usbmodem11301"],
    )
    locator = MeshLocator(
        settings or MeshtasticSettings(),
        AuditTrail(tmp_path / "audit.jsonl"),
        driver=driver,
        model_loader=lambda: _DIAL_ONLY_MODEL,
        state_dir=tmp_path / "state",
        now=lambda: now,
    )
    return locator, connection


def _locate(locator: MeshLocator, ref: str) -> str:
    return locator.locate(ref, reason="test question", actor=None)


def _audit_lines(tmp_path: Path) -> list[dict[str, Any]]:
    path = tmp_path / "audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


# --- US1: passive answers ----------------------------------------------------------------


def test_fresh_stored_position_answers_passively(tmp_path: Path) -> None:
    locator, connection = _locator(tmp_path)
    answer = _locate(locator, "AshH")
    # 126: a position-bearing answer is ONE sentence — no age words while current, no
    # last-heard/battery bookkeeping (the operator's 2026-09-02 directive).
    assert answer == "Good Sport (AshH) is at 4:55 and 2,660 feet from the Man."
    assert connection.position_requests == []
    assert _audit_lines(tmp_path) == []  # passive answers need no action row


def test_self_answers_from_own_info_with_no_mesh_traffic(tmp_path: Path) -> None:
    locator, connection = _locator(tmp_path)
    answer = _locate(locator, "MIRA")
    # (Long and short names that differ only by case collapse to the long form.)
    assert answer == "Mira is at 6:15 and 2,670 feet from the Man."
    assert connection.position_requests == []


def test_every_answer_fits_the_radio_budget(tmp_path: Path) -> None:
    locator, _ = _locator(tmp_path, reply=_tuma_reply())
    for ref in ("AshH", "MIRA", "TUMA", "0303", "nonesuch", "s"):
        assert len(_locate(locator, ref)) <= MAX_ANSWER_CHARS


def test_no_radio_is_honest(tmp_path: Path) -> None:
    locator, _ = _locator(tmp_path)
    driver = MeshDriver(MeshtasticSettings(), find_ports=lambda: [])
    locator._driver = driver  # noqa: SLF001 - swap the seam, keep everything else
    answer = _locate(locator, "TUMA")
    assert answer == (
        "The mesh radio isn't reachable (no Meshtastic USB device found on this machine)."
    )


def test_busy_port_is_honest(tmp_path: Path) -> None:
    def busy(port: str, timeout: float) -> _Connection:
        raise RuntimeError("could not exclusively lock port: Resource busy")

    locator, _ = _locator(tmp_path)
    locator._driver = MeshDriver(  # noqa: SLF001
        MeshtasticSettings(), connection_factory=busy, find_ports=lambda: ["/dev/x"]
    )
    answer = _locate(locator, "TUMA")
    assert answer.startswith("The mesh radio is busy (")


# --- US2 (reworked by 125): lookups are passive; the limiter is the daemon's now ---------


def test_missing_position_answers_passively_with_no_transmission(tmp_path: Path) -> None:
    locator, connection = _locator(tmp_path)
    answer = _locate(locator, "TUMA")
    assert connection.position_requests == []
    assert answer.startswith(
        "Tooma 9831 (TUMA) has no known position — no fresher position has been heard."
    )
    assert _audit_lines(tmp_path) == []  # no request ⇒ no request audit rows


def test_stale_position_answers_passively_honestly_aged(tmp_path: Path) -> None:
    stale_now = 1_787_983_616.0 + 7200.0  # tracker's fix is now 2 h old
    locator, connection = _locator(tmp_path, now=stale_now)
    answer = _locate(locator, "AshH")
    assert connection.position_requests == []
    # 126: the stale sentence is the WHOLE answer — honest age, nothing else.
    assert answer == (
        "Good Sport (AshH) was at 4:55 and 2,660 feet from the Man as of 2 hours ago — "
        "no fresher position has been heard."
    )


def test_positionless_node_reports_last_heard_and_likely_nearby(tmp_path: Path) -> None:
    locator, connection = _locator(tmp_path)
    answer = _locate(locator, "TUMA")
    assert connection.position_requests == []
    assert "- Last heard 1 minute ago, direct, strong signal (SNR 13.5) — likely nearby." in answer


# The durable budget itself still lives in mesh.py; the daemon is its consumer now
# (test_mesh_daemon pins the auto pass). These pin the class directly.


def test_limiter_state_is_durable_across_instances(tmp_path: Path) -> None:
    from obi.core.mesh import _RateLimiter

    settings = MeshtasticSettings()
    first = _RateLimiter(settings, tmp_path / "state", lambda: _NOW)
    first.record_send("!91378e3a")
    second = _RateLimiter(settings, tmp_path / "state", lambda: _NOW + 1)
    assert second.denial("!91378e3a") is not None  # the second PROCESS sees the send


def test_limiter_hourly_cap_spans_nodes(tmp_path: Path) -> None:
    from obi.core.mesh import _RateLimiter

    settings = MeshtasticSettings(hourly_tx_cap=1)
    limiter = _RateLimiter(settings, tmp_path / "state", lambda: _NOW)
    limiter.record_send("!91378e3a")
    assert "hourly mesh-transmission cap (1)" in (limiter.denial("!ed210303") or "")


def test_corrupt_limiter_state_is_treated_as_empty(tmp_path: Path) -> None:
    from obi.core.mesh import _RateLimiter

    state = tmp_path / "state"
    state.mkdir(parents=True)
    (state / "ratelimit.json").write_text("{not json")
    limiter = _RateLimiter(MeshtasticSettings(), state, lambda: _NOW)
    assert limiter.denial("!91378e3a") is None


# --- US3: forgiving resolution -----------------------------------------------------------


def test_lowercase_long_name_substring_resolves(tmp_path: Path) -> None:
    locator, _ = _locator(tmp_path)
    assert _locate(locator, "tooma").startswith("Tooma 9831 (TUMA)")


def test_node_id_resolves(tmp_path: Path) -> None:
    locator, _ = _locator(tmp_path)
    assert _locate(locator, "!91378e3a").startswith("Tooma 9831 (TUMA)")


def test_ambiguity_lists_candidates_and_transmits_nothing(tmp_path: Path) -> None:
    locator, connection = _locator(tmp_path, reply=_tuma_reply())
    answer = _locate(locator, "s")
    assert answer.startswith("Several mesh nodes match 's': ")
    assert "which one?" in answer
    assert connection.position_requests == []


def test_miss_offers_closest_names_and_transmits_nothing(tmp_path: Path) -> None:
    locator, connection = _locator(tmp_path, reply=_tuma_reply())
    answer = _locate(locator, "tomma")
    assert answer.startswith("No mesh node matches 'tomma' — closest are ")
    assert "Tooma 9831 (TUMA)" in answer
    assert connection.position_requests == []


def test_default_named_node_is_locatable(tmp_path: Path) -> None:
    locator, connection = _locator(tmp_path)
    answer = _locate(locator, "0303")
    assert answer.startswith(
        "Meshtastic 0303 (0303) has no known position — no fresher position has been heard."
    )
    assert connection.position_requests == []


def test_display_name_replaces_the_device_label(tmp_path: Path) -> None:
    # Field 2026-08-29: the answer should say "Tooma", not "Tooma 9831 (TUMA)".
    settings = MeshtasticSettings(display_names={"!91378e3a": "Tooma"})
    locator, _ = _locator(tmp_path, settings=settings)
    answer = _locate(locator, "tooma")
    assert answer.startswith("Tooma has no known position")
    assert "9831" not in answer and "TUMA" not in answer


def test_operator_alias_resolves_case_insensitively(tmp_path: Path) -> None:
    settings = MeshtasticSettings(aliases={"art car": "!91378e3a"})
    locator, connection = _locator(tmp_path, settings=settings)
    answer = _locate(locator, "Art Car")
    assert answer.startswith("Tooma 9831 (TUMA)")
    assert connection.position_requests == []


def test_alias_beats_fuzzy_matching(tmp_path: Path) -> None:
    # An alias is an operator decision — it wins even when the word would fuzzy-match
    # some other node's name.
    settings = MeshtasticSettings(aliases={"tooma": "!ed210303"})
    locator, _ = _locator(tmp_path, reply=None, settings=settings)
    assert _locate(locator, "tooma").startswith("Meshtastic 0303 (0303)")


def test_alias_to_a_node_the_radio_cannot_hear_is_honest(tmp_path: Path) -> None:
    settings = MeshtasticSettings(aliases={"art car": "!deadbeef"})
    locator, connection = _locator(tmp_path, reply=None, settings=settings)
    answer = _locate(locator, "art car")
    assert answer.startswith("No mesh node matches '!deadbeef' (alias 'art car')")
    assert connection.position_requests == []


# --- report_only allowlist (operator decision 2026-08-29: only Tooma, for now) -----------


def test_report_only_allows_the_listed_node_via_alias(tmp_path: Path) -> None:
    settings = MeshtasticSettings(
        aliases={"tooma": "!91378e3a"},
        display_names={"!91378e3a": "Tooma"},
        report_only=["!91378e3a"],
    )
    locator, connection = _locator(tmp_path, settings=settings)
    assert _locate(locator, "tooma").startswith("Tooma has no known position")
    assert connection.position_requests == []


def test_report_only_refuses_other_stations_without_leaking(tmp_path: Path) -> None:
    settings = MeshtasticSettings(display_names={"!91378e3a": "Tooma"}, report_only=["!91378e3a"])
    locator, connection = _locator(tmp_path, settings=settings)
    answer = _locate(locator, "AshH")
    assert "enabled only for Tooma" in answer
    # No leak: not the other station's names, not its position, no mesh traffic.
    assert "Good Sport" not in answer and "AshH is at" not in answer
    assert "feet from the Man" not in answer
    assert connection.position_requests == []


def test_report_only_restricts_the_fuzzy_and_suggestion_universe(tmp_path: Path) -> None:
    settings = MeshtasticSettings(display_names={"!91378e3a": "Tooma"}, report_only=["!91378e3a"])
    locator, connection = _locator(tmp_path, reply=None, settings=settings)
    # "0304" would fuzzy-hit "Meshtastic 0303" in the open universe; restricted, it must not.
    answer = _locate(locator, "0304")
    assert "0303" not in answer
    assert "enabled only for Tooma" in answer
    assert connection.position_requests == []


def test_report_only_even_covers_self(tmp_path: Path) -> None:
    # Strict by request: ONLY Tooma — the operator widens the list when they choose.
    settings = MeshtasticSettings(report_only=["!91378e3a"])
    locator, connection = _locator(tmp_path, settings=settings)
    answer = _locate(locator, "MIRA")
    assert "Mira is at" not in answer and "6:15" not in answer
    assert connection.position_requests == []


def test_empty_report_only_is_unrestricted(tmp_path: Path) -> None:
    locator, _ = _locator(tmp_path)  # default settings: no allowlist
    assert _locate(locator, "AshH").startswith("Good Sport (AshH) is at")


# --- fuzzy resolution (the radio callsign-match shape; field 2026-08-29: "toomer") -------


def test_alias_matches_squeezed(tmp_path: Path) -> None:
    # "artcar" ≡ "art car" — the callsign matcher's squeeze, so spacing never matters.
    settings = MeshtasticSettings(aliases={"art car": "!91378e3a"})
    locator, _ = _locator(tmp_path, settings=settings)
    assert _locate(locator, "ArtCar").startswith("Tooma 9831 (TUMA)")


def test_near_miss_fuzzy_resolves_like_a_callsign(tmp_path: Path) -> None:
    # "toma" scores 0.89 against the "tooma" alias — above the 0.75 caller threshold,
    # unambiguous, so it answers instead of refusing with suggestions.
    settings = MeshtasticSettings(aliases={"tooma": "!91378e3a"})
    locator, _ = _locator(tmp_path, settings=settings)
    assert _locate(locator, "toma").startswith("Tooma 9831 (TUMA)")


def test_fuzzy_near_tie_between_nodes_refuses(tmp_path: Path) -> None:
    # Two alias keys one edit apart, pointing at DIFFERENT nodes: a wrong node is worse
    # than none (the callsign matcher's ambiguity margin).
    settings = MeshtasticSettings(aliases={"tooma": "!91378e3a", "toomt": "!ed210303"})
    locator, connection = _locator(tmp_path, reply=None, settings=settings)
    answer = _locate(locator, "toomc")
    assert answer.startswith("Several mesh nodes")
    assert connection.position_requests == []


def test_below_threshold_still_gets_suggestions_not_a_guess(tmp_path: Path) -> None:
    # "zzzzq" clears nothing; the suggestions path is unchanged.
    locator, connection = _locator(tmp_path, reply=None)
    answer = _locate(locator, "zzzzq")
    assert answer.startswith("No mesh node matches")
    assert connection.position_requests == []


# --- 117: the last-known fallback --------------------------------------------------------
# A node with recorded history answers "was last known at …" from the store when the live
# path dead-ends; without history, today's refusals stand verbatim; live answers are
# byte-identical with history present. The store path is XDG-isolated per test (conftest).

_GHOST_ID = "!feedf00d"  # allowlisted but absent from the nodes.json fixture


def _ghost_settings() -> MeshtasticSettings:
    return MeshtasticSettings(
        report_only=[_GHOST_ID],
        aliases={"ghost": _GHOST_ID},
        display_names={_GHOST_ID: "Ghost"},
    )


def _seed_history(
    settings: MeshtasticSettings,
    *,
    node_id: str = _GHOST_ID,
    fix_time_s: int = int(_NOW) - 10_800,  # 3 hours before the injected clock
    address: str = "6:25 & Esplanade",
) -> None:
    conn = meshstore.connect(settings.positions_db_path)
    meshstore.record_fix(
        conn,
        meshstore.RecordedFix(
            node_id=node_id,
            latitude=40.7797341,
            longitude=-114.8227661,
            altitude_m=1191,
            fix_time_s=fix_time_s,
            observed_at_s=fix_time_s + 5,
            hops_away=0,
            snr_db=11.75,
            address=address,
        ),
    )
    conn.close()


def test_absent_node_with_history_answers_last_known(tmp_path: Path) -> None:
    settings = _ghost_settings()
    _seed_history(settings)
    locator, connection = _locator(tmp_path, settings=settings)
    answer = _locate(locator, "ghost")
    assert answer == (
        "Ghost was last known at 6:25 & Esplanade, 3 hours ago — "
        "it isn't in the radio's node table right now."
    )
    assert connection.position_requests == []  # the fallback never transmits


def test_absent_node_without_history_keeps_todays_refusal(tmp_path: Path) -> None:
    locator, _ = _locator(tmp_path, settings=_ghost_settings())
    answer = _locate(locator, "ghost")
    assert answer == (
        "No mesh node matches '!feedf00d' (alias 'ghost') — mesh location is enabled "
        "only for !feedf00d right now."
    )


def test_unreachable_radio_with_history_answers_last_known(tmp_path: Path) -> None:
    settings = _ghost_settings()
    _seed_history(settings)
    locator, _ = _locator(tmp_path, settings=settings)
    locator._driver = MeshDriver(settings, find_ports=lambda: [])  # noqa: SLF001
    answer = _locate(locator, "ghost")
    assert answer == (
        "Ghost was last known at 6:25 & Esplanade, 3 hours ago — the mesh radio isn't "
        "reachable (no Meshtastic USB device found on this machine)."
    )


def test_busy_radio_with_history_answers_last_known(tmp_path: Path) -> None:
    settings = _ghost_settings()
    _seed_history(settings)

    def busy(port: str, timeout: float) -> _Connection:
        raise RuntimeError("could not exclusively lock port: Resource busy")

    locator, _ = _locator(tmp_path, settings=settings)
    locator._driver = MeshDriver(  # noqa: SLF001
        settings, connection_factory=busy, find_ports=lambda: ["/dev/x"]
    )
    answer = _locate(locator, "ghost")
    assert answer.startswith(
        "Ghost was last known at 6:25 & Esplanade, 3 hours ago — the mesh radio is busy ("
    )


def test_unreachable_radio_without_allowlisted_needle_keeps_todays_prose(
    tmp_path: Path,
) -> None:
    settings = _ghost_settings()
    _seed_history(settings)  # history exists, but "TUMA" is not resolvable from config
    locator, _ = _locator(tmp_path, settings=settings)
    locator._driver = MeshDriver(settings, find_ports=lambda: [])  # noqa: SLF001
    answer = _locate(locator, "TUMA")
    assert answer == (
        "The mesh radio isn't reachable (no Meshtastic USB device found on this machine)."
    )


def test_positionless_node_with_history_appends_last_known(tmp_path: Path) -> None:
    settings = MeshtasticSettings(report_only=["!91378e3a"])
    _seed_history(settings, node_id="!91378e3a")
    locator, connection = _locator(tmp_path, settings=settings)
    answer = _locate(locator, "TUMA")
    assert answer.startswith(
        "Tooma 9831 (TUMA) has no live position — no fresher position has been heard. "
        "Last known: 6:25 & Esplanade, 3 hours ago."
    )
    assert "- Last heard 1 minute ago, direct, strong signal (SNR 13.5)" in answer
    assert connection.position_requests == []


def test_positionless_node_without_history_is_unchanged(tmp_path: Path) -> None:
    settings = MeshtasticSettings(report_only=["!91378e3a"])
    locator, _ = _locator(tmp_path, settings=settings)
    answer = _locate(locator, "TUMA")
    assert answer.startswith(
        "Tooma 9831 (TUMA) has no known position — no fresher position has been heard."
    )


def test_live_answers_are_byte_identical_with_history_present(tmp_path: Path) -> None:
    settings = MeshtasticSettings()
    _seed_history(settings, node_id="!38568530", address="9:00 & K")
    locator, _ = _locator(tmp_path, settings=settings)
    answer = _locate(locator, "AshH")
    assert answer.startswith("Good Sport (AshH) is at 4:55 and 2,660 feet from the Man")
    assert "Last known" not in answer and "9:00 & K" not in answer


def test_fallback_answers_fit_the_radio_budget(tmp_path: Path) -> None:
    settings = _ghost_settings()
    _seed_history(settings, address="6:25 and 2,360 feet from the Man")
    locator, _ = _locator(tmp_path, settings=settings)
    locator._driver = MeshDriver(settings, find_ports=lambda: [])  # noqa: SLF001
    assert len(_locate(locator, "ghost")) <= MAX_ANSWER_CHARS


# --- 117: record on sight (US2) ----------------------------------------------------------
# Positions are harvested from every node-table read for report_only nodes only, dedupe
# holds across re-reads, a store problem never breaks an answer, and the sweep pass is
# structurally unable to transmit.


def _stored_rows(settings: MeshtasticSettings) -> list[tuple]:  # type: ignore[type-arg]
    try:
        conn = meshstore.connect_readonly(settings.positions_db_path)
    except meshstore.StoreError:
        return []
    rows = conn.execute(
        "SELECT node_id, fix_time_s, address FROM position_fix ORDER BY id"
    ).fetchall()
    conn.close()
    return [tuple(row) for row in rows]


def test_locate_records_allowlisted_fixes_on_sight(tmp_path: Path) -> None:
    settings = MeshtasticSettings(report_only=["!38568530"])  # the tracker with a stored fix
    locator, _ = _locator(tmp_path, settings=settings)
    _locate(locator, "AshH")
    rows = _stored_rows(settings)
    assert len(rows) == 1
    node_id, fix_time_s, address = rows[0]
    assert node_id == "!38568530"
    assert fix_time_s == 1_787_983_616
    assert address == "4:55 and 2,660 feet from the Man"  # rendered at record time


def test_non_allowlisted_nodes_are_never_recorded(tmp_path: Path) -> None:
    settings = MeshtasticSettings()  # empty report_only: nothing may be recorded
    locator, _ = _locator(tmp_path, settings=settings)
    _locate(locator, "AshH")
    assert _stored_rows(settings) == []


def test_rereading_an_unchanged_table_adds_no_rows(tmp_path: Path) -> None:
    settings = MeshtasticSettings(report_only=["!38568530"])
    locator, _ = _locator(tmp_path, settings=settings)
    _locate(locator, "AshH")
    _locate(locator, "AshH")
    assert len(_stored_rows(settings)) == 1


def test_store_trouble_never_breaks_an_answer(tmp_path: Path) -> None:
    # positions_db_path pointing at a DIRECTORY: connect raises StoreError; the answer
    # must be the normal one regardless.
    bad = tmp_path / "not-a-db"
    bad.mkdir()
    settings = MeshtasticSettings(report_only=["!38568530"], positions_db_path=bad)
    locator, _ = _locator(tmp_path, settings=settings)
    answer = _locate(locator, "AshH")
    assert answer.startswith("Good Sport (AshH) is at 4:55 and 2,660 feet from the Man")


def test_record_pass_records_and_cannot_transmit(tmp_path: Path) -> None:
    settings = MeshtasticSettings(report_only=["!38568530"])
    locator, connection = _locator(tmp_path, settings=settings)

    def forbidden(node_id: str, channel_index: int) -> dict[str, Any] | None:
        raise AssertionError("the sweep must never transmit")

    connection.request_position = forbidden  # type: ignore[method-assign]
    outcome = locator.record_pass()
    assert outcome.recorded == 1
    assert outcome.tracked == 1
    assert outcome.skipped is None and outcome.error is None
    again = locator.record_pass()
    assert again.recorded == 0 and again.tracked == 1  # dedupe on the second sweep


def test_record_pass_skips_quietly_without_a_radio(tmp_path: Path) -> None:
    settings = MeshtasticSettings(report_only=["!38568530"])
    locator, _ = _locator(tmp_path, settings=settings)
    locator._driver = MeshDriver(settings, find_ports=lambda: [])  # noqa: SLF001
    outcome = locator.record_pass()
    assert outcome.recorded == 0
    assert outcome.skipped is not None and "isn't reachable" in outcome.skipped


def test_record_pass_skips_quietly_on_a_busy_port(tmp_path: Path) -> None:
    settings = MeshtasticSettings(report_only=["!38568530"])

    def busy(port: str, timeout: float) -> _Connection:
        raise RuntimeError("could not exclusively lock port: Resource busy")

    locator, _ = _locator(tmp_path, settings=settings)
    locator._driver = MeshDriver(  # noqa: SLF001
        settings, connection_factory=busy, find_ports=lambda: ["/dev/x"]
    )
    outcome = locator.record_pass()
    assert outcome.recorded == 0
    assert outcome.skipped is not None and "busy" in outcome.skipped


def test_record_pass_surfaces_a_store_failure(tmp_path: Path) -> None:
    bad = tmp_path / "not-a-db"
    bad.mkdir()
    settings = MeshtasticSettings(report_only=["!38568530"], positions_db_path=bad)
    locator, _ = _locator(tmp_path, settings=settings)
    outcome = locator.record_pass()
    assert outcome.error is not None


# --- 121: a refusal reads as a refusal, not as silence ------------------------------------


# --- 126: art-installation landmarks -----------------------------------------------------

# Good Sport's fixture fix; art coordinates are nudged due north of it by known feet.
_ASH_LAT, _ASH_LON = 40.7761230, -114.8176930


def _feet_north(feet: float) -> float:
    return math.degrees(feet / 3.280839895 / 6_371_008.8)


def _art_file(tmp_path: Path, *pieces: tuple[str, float, float]) -> Path:
    path = tmp_path / "art.json"
    path.write_text(
        json.dumps(
            [
                {"name": name, "location": {"gps_latitude": lat, "gps_longitude": lon}}
                for name, lat, lon in pieces
            ]
        )
    )
    return path


def test_position_near_art_names_the_piece(tmp_path: Path) -> None:
    art = _art_file(tmp_path, ("Head Maze", _ASH_LAT + _feet_north(200.0), _ASH_LON))
    locator, _ = _locator(tmp_path, settings=MeshtasticSettings(art_path=art))
    assert _locate(locator, "AshH") == (
        "Good Sport (AshH) is at 4:55 and 2,660 feet from the Man, near Head Maze."
    )


def test_nearest_of_two_qualifying_pieces_wins(tmp_path: Path) -> None:
    art = _art_file(
        tmp_path,
        ("Far Thing", _ASH_LAT + _feet_north(350.0), _ASH_LON),
        ("Head Maze", _ASH_LAT + _feet_north(200.0), _ASH_LON),
    )
    locator, _ = _locator(tmp_path, settings=MeshtasticSettings(art_path=art))
    assert _locate(locator, "AshH").endswith(", near Head Maze.")


def test_art_beyond_the_radius_is_not_referenced(tmp_path: Path) -> None:
    art = _art_file(tmp_path, ("Head Maze", _ASH_LAT + _feet_north(1000.0), _ASH_LON))
    locator, _ = _locator(tmp_path, settings=MeshtasticSettings(art_path=art))
    assert _locate(locator, "AshH") == ("Good Sport (AshH) is at 4:55 and 2,660 feet from the Man.")


def test_stale_answer_carries_the_landmark_too(tmp_path: Path) -> None:
    art = _art_file(tmp_path, ("Head Maze", _ASH_LAT + _feet_north(200.0), _ASH_LON))
    locator, _ = _locator(
        tmp_path,
        settings=MeshtasticSettings(art_path=art),
        now=1_787_983_616.0 + 7200.0,
    )
    assert _locate(locator, "AshH") == (
        "Good Sport (AshH) was at 4:55 and 2,660 feet from the Man, near Head Maze "
        "as of 2 hours ago — no fresher position has been heard."
    )


def test_corrupt_art_file_degrades_to_no_reference(tmp_path: Path) -> None:
    art = tmp_path / "art.json"
    art.write_text("{ not json")
    locator, _ = _locator(tmp_path, settings=MeshtasticSettings(art_path=art))
    assert _locate(locator, "AshH") == ("Good Sport (AshH) is at 4:55 and 2,660 feet from the Man.")


def test_long_art_name_still_fits_the_budget(tmp_path: Path) -> None:
    art = _art_file(tmp_path, ("X" * 700, _ASH_LAT + _feet_north(200.0), _ASH_LON))
    locator, _ = _locator(tmp_path, settings=MeshtasticSettings(art_path=art))
    assert len(_locate(locator, "AshH")) <= MAX_ANSWER_CHARS
