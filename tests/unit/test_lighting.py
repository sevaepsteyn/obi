"""Unit (feature 103): the lighting engine — gates, snapshots, revert, dry-run.

Everything runs against a fake in-memory device (no network, never the real portal). The
audit trail is a real AuditTrail on a tmp path so the records the constitution demands
(Principle V: every write with its pre-change snapshot) are asserted as written, not assumed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from obi.core.audit import AuditTrail
from obi.core.config import SegmentLayout, WledSettings
from obi.core.drivers.wled import WledEffects, WledInfo, WledPalettes, WledResult, _parse_state
from obi.core.lighting import (
    Lighting,
    describe_state,
    resolve_color,
    resolve_effect,
    resolve_palette,
    status_band_matches,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "wled"
EFFECTS = ["Solid", "Blink", "Breathe", "Fire 2012", "Fireworks", "Gradient"]
PALETTES = ["Default", "Ocean", "Icefire", "Aurora", "Aurora 2", "Sunset"]


def _state_doc() -> dict[str, Any]:
    return json.loads((FIXTURES / "state.json").read_text())


class FakeWled:
    """In-memory device: three segments, merge-on-apply, togglable live flag and failures."""

    def __init__(
        self,
        state: dict[str, Any] | None = None,
        *,
        live: bool = False,
        live_source: str = "",
        fail: str | None = None,
    ) -> None:
        self.state_doc = state if state is not None else _state_doc()
        self.live = live
        self.live_source = live_source
        self.fail = fail  # "info" | "state" | "effects" | "palettes" | "apply" | None
        self.applied: list[dict[str, Any]] = []

    def info(self, base_url: str) -> WledResult:
        if self.fail == "info":
            return WledResult(ok=False, error="could not connect to the device")
        return WledResult(
            ok=True,
            value=WledInfo(
                name="Dig-Quad-AR",
                version="0.15.3",
                uptime_s=3600,
                led_count=720,
                power_estimate_mw=7700,
                wifi_rssi=-57,
                live=self.live,
                live_source=self.live_source,
            ),
        )

    def state(self, base_url: str) -> WledResult:
        if self.fail == "state":
            return WledResult(ok=False, error="device returned HTTP 500")
        state = _parse_state(self.state_doc)
        assert state is not None
        return WledResult(ok=True, value=state)

    def effects(self, base_url: str) -> WledResult:
        if self.fail == "effects":
            return WledResult(ok=False, error="device returned HTTP 502")
        return WledResult(ok=True, value=WledEffects(names=list(EFFECTS)))

    def palettes(self, base_url: str) -> WledResult:
        if self.fail == "palettes":
            return WledResult(ok=False, error="device returned HTTP 502")
        return WledResult(ok=True, value=WledPalettes(names=list(PALETTES)))

    def apply(self, base_url: str, partial_state: dict[str, Any]) -> WledResult:
        if self.fail == "apply":
            return WledResult(ok=False, error="device did not respond within 1s")
        self.applied.append(partial_state)
        merged = {
            **self.state_doc,
            **{k: v for k, v in partial_state.items() if k not in ("v", "seg")},
        }
        if "seg" in partial_state:
            by_id = {s["id"]: s for s in merged.get("seg", []) if isinstance(s.get("id"), int)}
            for change in partial_state["seg"]:
                seg_id = change.get("id")
                if seg_id in by_id:
                    by_id[seg_id] = {**by_id[seg_id], **change}
            merged["seg"] = list(by_id.values())
        self.state_doc = merged
        state = _parse_state(merged)
        assert state is not None
        return WledResult(ok=True, value=state)


def _engine(tmp_path: Path, fake: FakeWled, devices: dict[str, str] | None = None) -> Lighting:
    settings = WledSettings(devices=devices or {"portal": "https://portal.test"})
    return Lighting(
        settings,
        AuditTrail(tmp_path / "audit.jsonl"),
        driver=fake,
        snapshots_path=tmp_path / "wled_snapshots.json",
    )


def _audit_lines(tmp_path: Path) -> list[dict[str, Any]]:
    path = tmp_path / "audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# --- resolution helpers -------------------------------------------------------------------------


def test_resolve_color_names_and_hex() -> None:
    assert resolve_color("purple") == (160, 32, 240)
    assert resolve_color("  Amber ") == (255, 160, 0)
    assert resolve_color("#FF00ff") == (255, 0, 255)
    assert resolve_color("00ff00") == (0, 255, 0)
    assert resolve_color("blurple") is None
    assert resolve_color("#12345") is None


def test_resolve_effect_exact_substring_ambiguous_and_fuzzy() -> None:
    assert resolve_effect("fire 2012", EFFECTS) == (3, [])
    # Unique substring resolves; ambiguous substring suggests instead of guessing.
    assert resolve_effect("gradi", EFFECTS) == (5, [])
    effect_id, suggestions = resolve_effect("fire", EFFECTS)
    assert effect_id is None and set(suggestions) == {"Fire 2012", "Fireworks"}
    # Fuzzy close-match still only suggests.
    effect_id, suggestions = resolve_effect("Fire 2013", EFFECTS)
    assert effect_id is None and "Fire 2012" in suggestions
    assert resolve_effect("polka dot zebra", EFFECTS)[0] is None


def test_resolve_palette_exact_spaceless_ambiguous_and_fuzzy() -> None:
    assert resolve_palette("icefire", PALETTES) == (2, [])
    assert resolve_palette("Aurora", PALETTES) == (3, [])
    # Spaceless exact: the radio-call-24 verbatim phrasing resolves deterministically.
    assert resolve_palette("ice fire", PALETTES) == (2, [])
    # Ambiguous substring suggests instead of guessing.
    palette_id, suggestions = resolve_palette("auro", PALETTES)
    assert palette_id is None and set(suggestions) == {"Aurora", "Aurora 2"}
    # Fuzzy near-miss only suggests (SC-002: no silent pick from fuzzy candidates).
    palette_id, suggestions = resolve_palette("icefyre", PALETTES)
    assert palette_id is None and "Icefire" in suggestions
    assert resolve_palette("polka dot zebra", PALETTES) == (None, [])


# --- set_look: gates in order -------------------------------------------------------------------


def test_set_color_applies_to_all_segments_and_audits_snapshot(tmp_path: Path) -> None:
    fake = FakeWled()
    before = _state_doc()
    out = _engine(tmp_path, fake).set_look(color="purple", reason="make the portal purple")
    assert "portal is now" in out and "Say 'portal revert'" in out  # 137: the device's own name
    assert len(fake.applied) == 1
    sent = fake.applied[0]
    segment_ids = [seg["id"] for seg in before["seg"]]
    # Color targets every segment uniformly, and color-only selects Solid so it shows.
    assert sent["seg"] == [
        {"id": seg_id, "col": [[160, 32, 240]], "fx": 0} for seg_id in segment_ids
    ]
    assert sent["on"] is True
    records = _audit_lines(tmp_path)
    assert records and records[-1]["action"] == "wled_set"
    assert records[-1]["outcome"] == "ok"
    # The pre-change state rides in the audit record verbatim (Principle V).
    assert records[-1]["params"]["snapshot"] == before


def test_set_refuses_non_allowlisted_device_before_contact(tmp_path: Path) -> None:
    fake = FakeWled()
    out = _engine(tmp_path, fake).set_look("core-gw-1", color="red")
    assert "not an allowlisted lighting device" in out
    assert fake.applied == []
    records = _audit_lines(tmp_path)
    assert records[-1]["action"] == "wled_set" and records[-1]["outcome"] == "denied"


def test_set_refuses_while_live_stream_in_control(tmp_path: Path) -> None:
    fake = FakeWled(live=True, live_source="E1.31")
    out = _engine(tmp_path, fake).set_look(color="red")
    assert "live show" in out and "E1.31" in out
    assert fake.applied == []
    assert _audit_lines(tmp_path)[-1]["outcome"] == "denied"


def test_set_dry_run_writes_nothing_and_previews(tmp_path: Path) -> None:
    fake = FakeWled()
    out = _engine(tmp_path, fake).set_look(color="red", brightness_pct=50, dry_run=True)
    assert out.startswith("Dry-run") and "Nothing changed" in out
    assert fake.applied == []
    assert not (tmp_path / "wled_snapshots.json").exists()
    record = _audit_lines(tmp_path)[-1]
    assert record["outcome"] == "ok" and record["params"]["dry_run"] is True


def test_set_unreachable_is_honest_and_audited(tmp_path: Path) -> None:
    fake = FakeWled(fail="info")
    out = _engine(tmp_path, fake).set_look(color="red")
    assert "unreachable" in out and "Nothing changed" in out
    assert _audit_lines(tmp_path)[-1]["outcome"] == "unreachable"


def test_set_without_snapshot_refuses_to_write(tmp_path: Path) -> None:
    fake = FakeWled(fail="state")
    out = _engine(tmp_path, fake).set_look(color="red")
    assert "not writing without a snapshot" in out
    assert fake.applied == []


def test_set_unknown_effect_suggests_and_writes_nothing(tmp_path: Path) -> None:
    fake = FakeWled()
    out = _engine(tmp_path, fake).set_look(effect="fire")
    assert "Nothing changed" in out and "Fire 2012" in out and "Fireworks" in out
    assert fake.applied == []


def test_set_brightness_and_power_mappings(tmp_path: Path) -> None:
    fake = FakeWled()
    engine = _engine(tmp_path, fake)
    engine.set_look(brightness_pct=50)
    assert fake.applied[-1] == {"bri": 128, "on": True}
    engine.set_look(power=False)
    # A plain power-off is exactly {"on": false} — nothing else rides along.
    assert fake.applied[-1] == {"on": False}


def test_set_nothing_requested(tmp_path: Path) -> None:
    out = _engine(tmp_path, FakeWled()).set_look()
    assert "Nothing to change" in out


def test_set_apply_failure_is_honest(tmp_path: Path) -> None:
    fake = FakeWled(fail="apply")
    out = _engine(tmp_path, fake).set_look(color="red")
    assert "failed" in out
    assert _audit_lines(tmp_path)[-1]["outcome"] == "error"


# --- feature 107: palette, effect settings, color slots -----------------------------------------


def test_set_palette_applies_to_all_segments_and_names_it(tmp_path: Path) -> None:
    fake = FakeWled()
    out = _engine(tmp_path, fake).set_look(palette="ice fire")
    assert "'Icefire'" in out
    sent = fake.applied[-1]
    assert all(seg["pal"] == 2 for seg in sent["seg"])
    # A palette request alone never touches the effect or colors.
    assert all("fx" not in seg and "col" not in seg for seg in sent["seg"])
    record = _audit_lines(tmp_path)[-1]
    assert record["outcome"] == "ok" and record["params"]["requested"]["palette"] == "ice fire"


def test_set_palette_miss_suggests_writes_nothing_and_audits(tmp_path: Path) -> None:
    fake = FakeWled()
    out = _engine(tmp_path, fake).set_look(palette="zebra stripes")
    assert "no palette named" in out and "Nothing changed" in out
    assert fake.applied == []
    # 107 research D7: the refusal leaves an action record (radio-call-24 left none).
    record = _audit_lines(tmp_path)[-1]
    assert record["action"] == "wled_set" and record["outcome"] == "error"
    assert "no palette named" in record["detail"]


def test_set_palette_catalog_read_failure_is_honest_and_audited(tmp_path: Path) -> None:
    fake = FakeWled(fail="palettes")
    out = _engine(tmp_path, fake).set_look(palette="icefire")
    assert "could not read the palette catalog" in out
    assert fake.applied == []
    record = _audit_lines(tmp_path)[-1]
    assert record["outcome"] == "error" and "palette catalog" in record["detail"]


def test_set_effect_catalog_read_failure_is_audited(tmp_path: Path) -> None:
    fake = FakeWled(fail="effects")
    out = _engine(tmp_path, fake).set_look(effect="fireworks")
    assert "could not read the effect catalog" in out
    record = _audit_lines(tmp_path)[-1]
    assert record["action"] == "wled_set" and record["outcome"] == "error"


def test_set_unknown_color_is_audited(tmp_path: Path) -> None:
    fake = FakeWled()
    out = _engine(tmp_path, fake).set_look(color="blurple")
    assert "I don't know the color" in out
    assert fake.applied == []
    record = _audit_lines(tmp_path)[-1]
    assert record["outcome"] == "error" and "blurple" in record["detail"]


def test_set_palette_on_solid_notes_invisibility(tmp_path: Path) -> None:
    state = _state_doc()
    for seg in state["seg"]:
        seg["fx"] = 0
    fake = FakeWled(state)
    out = _engine(tmp_path, fake).set_look(palette="icefire")
    assert "'Icefire'" in out and "won't show the palette" in out
    # The palette IS applied — legal but invisible, said honestly.
    assert all(seg["pal"] == 2 for seg in fake.applied[-1]["seg"])


def test_set_speed_and_intensity_touch_only_those_fields(tmp_path: Path) -> None:
    fake = FakeWled()
    out = _engine(tmp_path, fake).set_look(speed_pct=50, intensity_pct=100)
    sent = fake.applied[-1]
    for seg in sent["seg"]:
        assert set(seg) == {"id", "sx", "ix"}
        assert seg["sx"] == 128 and seg["ix"] == 255
    assert "speed 50%" in out and "intensity 100%" in out


def test_set_effect_with_settings_is_one_atomic_change(tmp_path: Path) -> None:
    fake = FakeWled()
    engine = _engine(tmp_path, fake)
    engine.set_look(effect="fire 2012", speed_pct=10)
    assert len(fake.applied) == 1
    for seg in fake.applied[0]["seg"]:
        assert seg["fx"] == 3 and seg["sx"] == 26
    assert len([r for r in _audit_lines(tmp_path) if r["action"] == "wled_set"]) == 1


def test_custom_sliders_map_to_device_scales(tmp_path: Path) -> None:
    fake = FakeWled()
    _engine(tmp_path, fake).set_look(custom1_pct=50, custom2_pct=100, custom3_pct=100)
    seg = fake.applied[-1]["seg"][0]
    assert seg["c1"] == 128 and seg["c2"] == 255
    assert seg["c3"] == 31  # c3 runs 0-31 (research D2), not 0-255


def test_out_of_range_percents_clamp(tmp_path: Path) -> None:
    fake = FakeWled()
    _engine(tmp_path, fake).set_look(speed_pct=999)
    assert fake.applied[-1]["seg"][0]["sx"] == 255


def test_dry_run_would_send_carries_new_fields(tmp_path: Path) -> None:
    fake = FakeWled()
    out = _engine(tmp_path, fake).set_look(palette="icefire", speed_pct=30, dry_run=True)
    assert out.startswith("Dry-run") and '"pal": 2' in out and '"sx": 76' in out
    assert fake.applied == []


def test_two_color_request_composes_slots_from_before_state(tmp_path: Path) -> None:
    fake = FakeWled()
    before_cols = _state_doc()["seg"][0]["col"]
    _engine(tmp_path, fake).set_look(effect="fire 2012", color="red", color2="white")
    seg = fake.applied[-1]["seg"][0]
    # Requested slots land; the unspecified tertiary keeps its before-state value.
    assert seg["col"] == [[255, 0, 0], [255, 255, 255], before_cols[2]]
    assert seg["fx"] == 3


def test_color2_alone_preserves_primary_and_never_forces_solid(tmp_path: Path) -> None:
    fake = FakeWled()
    before_cols = _state_doc()["seg"][0]["col"]
    _engine(tmp_path, fake).set_look(color2="blue")
    seg = fake.applied[-1]["seg"][0]
    assert seg["col"] == [before_cols[0], [0, 0, 255], before_cols[2]]
    assert "fx" not in seg  # the Solid rule is keyed to the PRIMARY slot only


# --- snapshots & revert (US2) -------------------------------------------------------------------


def test_snapshot_persists_before_write_and_survives_restart(tmp_path: Path) -> None:
    fake = FakeWled()
    before = _state_doc()
    _engine(tmp_path, fake).set_look(color="purple")
    # A NEW engine instance (fresh process) still holds the snapshot from disk.
    held = _engine(tmp_path, FakeWled()).held_snapshot("portal")
    assert held is not None
    assert held.state == before
    assert held.saved_at != "?"


def test_revert_restores_snapshot_verbatim_and_is_itself_revertible(tmp_path: Path) -> None:
    fake = FakeWled()
    before = _state_doc()
    engine = _engine(tmp_path, fake)
    engine.set_look(color="purple", reason="party time")
    changed = dict(fake.state_doc)
    out = engine.revert(reason="put it back")
    assert "restored" in out
    # The revert POSTs the held snapshot VERBATIM (plus nothing).
    assert fake.applied[-1] == before
    # …and the overwritten (purple) look became the new snapshot: revert twice returns.
    held = engine.held_snapshot("portal")
    assert held is not None and held.state == changed
    records = _audit_lines(tmp_path)
    assert records[-1]["action"] == "wled_revert" and records[-1]["outcome"] == "ok"
    assert records[-1]["params"]["snapshot_restored"] == before


def test_revert_with_nothing_held(tmp_path: Path) -> None:
    fake = FakeWled()
    out = _engine(tmp_path, fake).revert()
    assert "Nothing to revert" in out
    assert fake.applied == []


def test_revert_dry_run_and_live_gate(tmp_path: Path) -> None:
    fake = FakeWled()
    engine = _engine(tmp_path, fake)
    engine.set_look(color="purple")
    applied_count = len(fake.applied)
    out = engine.revert(dry_run=True)
    assert out.startswith("Dry-run") and len(fake.applied) == applied_count
    fake.live = True
    out = engine.revert()
    assert "live show" in out and len(fake.applied) == applied_count


# --- status -------------------------------------------------------------------------------------


def test_status_reports_look_live_and_held_snapshot(tmp_path: Path) -> None:
    fake = FakeWled()
    engine = _engine(tmp_path, fake)
    out = engine.status()
    assert "Dig-Quad-AR" in out and "WLED 0.15.3" in out and "720 LEDs" in out
    assert "revert available" not in out
    engine.set_look(color="purple")
    out = engine.status()
    assert "revert available" in out
    fake.live = True
    fake.live_source = "E1.31"
    assert "IN CONTROL" in engine.status()


def test_status_unreachable(tmp_path: Path) -> None:
    out = _engine(tmp_path, FakeWled(fail="info")).status()
    assert "unreachable" in out


def test_describe_state_names_effect_from_catalog() -> None:
    state = _parse_state(
        {"on": True, "bri": 255, "seg": [{"id": 0, "col": [[255, 0, 0]], "fx": 3}]}
    )
    assert state is not None
    text = describe_state(state, EFFECTS)
    assert "100%" in text and "#FF0000" in text and "'Fire 2012'" in text
    off = _parse_state({"on": False, "bri": 10, "seg": []})
    assert off is not None and describe_state(off) == "off"


def test_describe_state_names_palette_and_levels() -> None:
    state = _parse_state(
        {
            "on": True,
            "bri": 255,
            "seg": [{"id": 0, "col": [[255, 0, 0]], "fx": 3, "pal": 2, "sx": 128, "ix": 64}],
        }
    )
    assert state is not None
    text = describe_state(state, EFFECTS, PALETTES)
    assert "palette 'Icefire'" in text and "speed 50%" in text and "intensity 25%" in text
    # Without the palette catalog the id degrades to a number, never a crash.
    assert "palette #2" in describe_state(state, EFFECTS)
    # The Default palette (0) and settings on Solid (fx 0) are elided as noise.
    solid = _parse_state(
        {
            "on": True,
            "bri": 255,
            "seg": [{"id": 0, "col": [[255, 0, 0]], "fx": 0, "pal": 0, "sx": 90}],
        }
    )
    assert solid is not None
    quiet = describe_state(solid, EFFECTS, PALETTES)
    assert "palette" not in quiet and "speed" not in quiet


def test_status_names_palette_and_levels(tmp_path: Path) -> None:
    state = _state_doc()
    state["seg"][0]["pal"] = 2
    out = _engine(tmp_path, FakeWled(state)).status()
    assert "palette 'Icefire'" in out
    assert "speed 50%" in out and "intensity 6%" in out  # fixture sx=128, ix=16


def test_status_degrades_when_palette_catalog_unreadable(tmp_path: Path) -> None:
    state = _state_doc()
    state["seg"][0]["pal"] = 2
    out = _engine(tmp_path, FakeWled(state, fail="palettes")).status()
    assert "look:" in out and "palette #2" in out  # look still reported, id not a name


# --- 128: segment-layout scoping (US1 — operator writes leave the status bands alone) -----------


def _state_doc_6seg() -> dict[str, Any]:
    return json.loads((FIXTURES / "state_6seg.json").read_text())


_LAYOUT = SegmentLayout(status=(0, 2, 4), main=(1, 3, 5))


def _layout_engine(tmp_path: Path, fake: FakeWled) -> Lighting:
    settings = WledSettings(devices={"portal": "https://portal.test"}, segments={"portal": _LAYOUT})
    return Lighting(
        settings,
        AuditTrail(tmp_path / "audit.jsonl"),
        driver=fake,
        snapshots_path=tmp_path / "wled_snapshots.json",
    )


def _seg_by_id(doc: dict[str, Any]) -> dict[int, dict[str, Any]]:
    return {s["id"]: s for s in doc.get("seg", []) if isinstance(s.get("id"), int)}


def test_scoped_set_targets_main_segments_only(tmp_path: Path) -> None:
    fake = FakeWled(_state_doc_6seg())
    engine = _layout_engine(tmp_path, fake)
    out = engine.set_look(color="blue")
    assert "portal is now" in out
    sent_ids = [s["id"] for s in fake.applied[-1]["seg"]]
    assert sent_ids == [1, 3, 5]
    after = _seg_by_id(fake.state_doc)
    # Status bands byte-identical to the fixture; main segments changed.
    fixture = _seg_by_id(_state_doc_6seg())
    for sid in (0, 2, 4):
        assert after[sid] == fixture[sid]
    for sid in (1, 3, 5):
        assert after[sid]["col"][0] == [0, 0, 255]


def test_scoped_dry_run_names_main_ids_only(tmp_path: Path) -> None:
    fake = FakeWled(_state_doc_6seg())
    engine = _layout_engine(tmp_path, fake)
    out = engine.set_look(color="blue", dry_run=True)
    assert "Dry-run" in out
    assert '"id": 1' in out and '"id": 3' in out and '"id": 5' in out
    assert '"id": 0' not in out and '"id": 2' not in out and '"id": 4' not in out
    assert fake.applied == []


def test_scoped_sparse_slot_base_comes_from_first_main_segment(tmp_path: Path) -> None:
    fake = FakeWled(_state_doc_6seg())
    engine = _layout_engine(tmp_path, fake)
    engine.set_look(color2="purple")
    col = fake.applied[-1]["seg"][0]["col"]
    # Base slots come from segment 1 (first MAIN), not segment 0 (a status band).
    assert col[0] == [0, 200, 0]
    assert col[1] == [160, 32, 240]
    assert col[2] == [255, 92, 174]


def test_scoped_revert_leaves_current_status_color(tmp_path: Path) -> None:
    fake = FakeWled(_state_doc_6seg())
    engine = _layout_engine(tmp_path, fake)
    engine.set_look(color="blue")  # snapshot captures the fixture look
    # The dash (or anyone) changes a status band AFTER the snapshot:
    by_id = _seg_by_id(fake.state_doc)
    by_id[0]["col"] = [[255, 0, 0], [0, 0, 0], [0, 0, 0]]
    out = engine.revert()
    assert "restored" in out
    sent_ids = {s["id"] for s in fake.applied[-1]["seg"]}
    assert sent_ids == {1, 3, 5}
    after = _seg_by_id(fake.state_doc)
    # Status band keeps its CURRENT (post-snapshot) color — not the snapshot's.
    assert after[0]["col"][0] == [255, 0, 0]
    # Main segments returned to the fixture look.
    fixture = _seg_by_id(_state_doc_6seg())
    for sid in (1, 3, 5):
        assert after[sid]["col"] == fixture[sid]["col"]
        assert after[sid]["fx"] == fixture[sid]["fx"]


def test_scoped_revert_of_revert_round_trips_main_look(tmp_path: Path) -> None:
    fake = FakeWled(_state_doc_6seg())
    engine = _layout_engine(tmp_path, fake)
    engine.set_look(color="blue")
    engine.revert()
    out = engine.revert()  # back to blue
    assert "restored" in out
    after = _seg_by_id(fake.state_doc)
    for sid in (1, 3, 5):
        assert after[sid]["col"][0] == [0, 0, 255]
    sent_ids = {s["id"] for s in fake.applied[-1]["seg"]}
    assert sent_ids == {1, 3, 5}


def test_no_layout_keeps_legacy_uniform_behavior(tmp_path: Path) -> None:
    # Regression pin (FR-004): without a declared layout, set walks EVERY segment and
    # revert POSTs the held snapshot verbatim — byte-identical to pre-128 behavior.
    fake = FakeWled(_state_doc_6seg())
    engine = _engine(tmp_path, fake)
    engine.set_look(color="blue")
    assert [s["id"] for s in fake.applied[-1]["seg"]] == [0, 1, 2, 3, 4, 5]
    held = json.loads((tmp_path / "wled_snapshots.json").read_text())["portal"]["state"]
    engine.revert()
    assert fake.applied[-1] == held


# --- 128: the status-band write path (US2 — dash/CLI only) --------------------------------------


def test_status_band_green_targets_status_ids_solid_emerald(tmp_path: Path) -> None:
    fake = FakeWled(_state_doc_6seg())
    engine = _layout_engine(tmp_path, fake)
    out = engine.set_status_band(look="green", reason="test", actor="dash")
    assert "green" in out
    sent = fake.applied[-1]["seg"]
    assert [s["id"] for s in sent] == [0, 2, 4]
    for entry in sent:
        assert entry["col"] == [[0, 210, 40]]
        assert entry["fx"] == 0
    records = [r for r in _audit_lines(tmp_path) if r["action"] == "wled_status_set"]
    assert records and records[-1]["outcome"] == "ok"
    assert records[-1]["actor"] == "dash"


def test_status_band_red_blinks_by_catalog_name(tmp_path: Path) -> None:
    fake = FakeWled(_state_doc_6seg())
    engine = _layout_engine(tmp_path, fake)
    engine.set_status_band(look="red", reason="test", actor="dash")
    sent = fake.applied[-1]["seg"]
    assert [s["id"] for s in sent] == [0, 2, 4]
    for entry in sent:
        assert entry["col"][0] == [255, 0, 0]
        assert entry["fx"] == EFFECTS.index("Blink")  # resolved by NAME, never hardcoded
        assert entry["sx"] > 128  # brisk — alarming, not lazy


def test_status_band_off_turns_only_status_segments_off(tmp_path: Path) -> None:
    fake = FakeWled(_state_doc_6seg())
    engine = _layout_engine(tmp_path, fake)
    engine.set_status_band(look="off", reason="test", actor=None)
    sent = fake.applied[-1]["seg"]
    assert [(s["id"], s["on"]) for s in sent] == [(0, False), (2, False), (4, False)]
    assert all(set(entry) == {"id", "on"} for entry in sent)  # no look fields sent


def test_status_band_unknown_look_refused_with_vocabulary(tmp_path: Path) -> None:
    fake = FakeWled(_state_doc_6seg())
    engine = _layout_engine(tmp_path, fake)
    out = engine.set_status_band(look="chartreuse", reason="test", actor=None)
    assert "green" in out and "yellow" in out and "red" in out and "off" in out
    assert fake.applied == []


def test_status_band_requires_a_layout(tmp_path: Path) -> None:
    fake = FakeWled(_state_doc_6seg())
    engine = _engine(tmp_path, fake)  # no layout declared
    out = engine.set_status_band(look="green", reason="test", actor=None)
    assert "layout" in out.lower()
    assert fake.applied == []


def test_status_band_yields_to_live_show(tmp_path: Path) -> None:
    fake = FakeWled(_state_doc_6seg(), live=True, live_source="E1.31")
    engine = _layout_engine(tmp_path, fake)
    out = engine.set_status_band(look="red", reason="test", actor="dash")
    assert "live show" in out
    assert fake.applied == []
    records = [r for r in _audit_lines(tmp_path) if r["action"] == "wled_status_set"]
    assert records and records[-1]["outcome"] == "denied"


def test_status_band_dry_run_writes_nothing(tmp_path: Path) -> None:
    fake = FakeWled(_state_doc_6seg())
    engine = _layout_engine(tmp_path, fake)
    out = engine.set_status_band(look="yellow", reason="test", actor=None, dry_run=True)
    assert "Dry-run" in out and '"id": 0' in out
    assert fake.applied == []


def test_status_band_never_touches_the_snapshot_store(tmp_path: Path) -> None:
    fake = FakeWled(_state_doc_6seg())
    engine = _layout_engine(tmp_path, fake)
    engine.set_look(color="blue")  # seeds the operator snapshot
    before = (tmp_path / "wled_snapshots.json").read_text()
    engine.set_status_band(look="red", reason="test", actor="dash")
    engine.set_status_band(look="green", reason="test", actor="dash")
    assert (tmp_path / "wled_snapshots.json").read_text() == before


def test_scoped_palette_note_reads_the_main_segment_not_a_status_band(tmp_path: Path) -> None:
    # Regression (radio Icefire, 2026-09-02): under a layout, seg[0] is a SOLID status band.
    # A palette set that lands on the ANIMATED main segments must NOT warn "solid won't show
    # the palette" — the confirmation must describe the main segment the look landed on.
    fake = FakeWled(_state_doc_6seg())  # main segs 1/3/5 have fx 112 (animated)
    engine = _layout_engine(tmp_path, fake)
    out = engine.set_look(palette="Ocean")
    assert "won't show the palette" not in out
    assert "palette 'Ocean'" in out  # the confirmation describes the animated main area


def test_scoped_palette_note_still_fires_when_main_is_solid(tmp_path: Path) -> None:
    # The note is still correct when the MAIN segments really are Solid.
    doc = _state_doc_6seg()
    for seg in doc["seg"]:
        if seg["id"] in (1, 3, 5):
            seg["fx"] = 0
    fake = FakeWled(doc)
    engine = _layout_engine(tmp_path, fake)
    out = engine.set_look(palette="Ocean")
    assert "won't show the palette" in out


def test_scoped_confirmation_describes_the_main_segment(tmp_path: Path) -> None:
    # "now …" must narrate the pattern area, not the status band's solid green.
    fake = FakeWled(_state_doc_6seg())
    engine = _layout_engine(tmp_path, fake)
    out = engine.set_look(color="blue")
    assert "#0000FF" in out  # the main segments' new color, not the status band's


def test_status_describes_the_main_segment_not_a_status_band(tmp_path: Path) -> None:
    # Regression (operator, 2026-09-02): "what's the portal doing" answered "solid green" —
    # the STATUS band — while the main segments ran an animated look. status() must read the
    # first MAIN segment under a declared layout, exactly like the set_look confirmation.
    fake = FakeWled(_state_doc_6seg())  # status 0/2/4: fx 0 #00FF00; main 1/3/5: fx 112 #00C800
    out = _layout_engine(tmp_path, fake).status()
    assert "effect #112" in out and "#00C800" in out  # the main segments' animated look
    assert "'Solid'" not in out and "#00FF00" not in out  # not the status band


def test_everything_follows_a_different_declared_layout(tmp_path: Path) -> None:
    # US3/SC-006: the ids are pure config — nothing in the engine assumes 0/2/4 vs 1/3/5.
    fake = FakeWled(_state_doc_6seg())
    settings = WledSettings(
        devices={"portal": "https://portal.test"},
        segments={"portal": SegmentLayout(status=(5,), main=(0, 1))},
    )
    engine = Lighting(
        settings,
        AuditTrail(tmp_path / "audit.jsonl"),
        driver=fake,
        snapshots_path=tmp_path / "wled_snapshots.json",
    )
    engine.set_look(color="blue")
    assert [s["id"] for s in fake.applied[-1]["seg"]] == [0, 1]
    engine.set_status_band(look="green", reason="test", actor=None)
    assert [s["id"] for s in fake.applied[-1]["seg"]] == [5]
    engine.revert()
    assert {s["id"] for s in fake.applied[-1]["seg"]} == {0, 1}


def test_status_band_matches_judges_drift(tmp_path: Path) -> None:
    fake = FakeWled(_state_doc_6seg())
    engine = _layout_engine(tmp_path, fake)
    # Fixture bands are pure green (0,255,0) — NOT the vocabulary emerald: drift.
    assert status_band_matches(fake.state_doc, _LAYOUT.status, "green") is False
    engine.set_status_band(look="green", reason="test", actor="dash")
    assert status_band_matches(fake.state_doc, _LAYOUT.status, "green") is True
    assert status_band_matches(fake.state_doc, _LAYOUT.status, "red") is False
    engine.set_status_band(look="red", reason="test", actor="dash")
    assert status_band_matches(fake.state_doc, _LAYOUT.status, "red") is True
    assert status_band_matches(fake.state_doc, _LAYOUT.status, "green") is False
    engine.set_status_band(look="off", reason="test", actor="dash")
    assert status_band_matches(fake.state_doc, _LAYOUT.status, "off") is True
    # Missing status segments ⇒ never "matching" (the reconcile step must notice).
    assert status_band_matches({"seg": []}, _LAYOUT.status, "green") is False
