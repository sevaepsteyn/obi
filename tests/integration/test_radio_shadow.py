"""Integration (feature 088, US3): the rollout stages — shadow decisions and shadow-TX.

SC-002's standing assertion lives here: in every stage with transmit disabled, the PTT is
never touched and no audio is ever played, while the full call flow still runs so drafts
are logged (shadow) or DM'd in real time (shadow-TX). Stage flips are config-only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from obi.core.config import RadioSettings
from tests.integration.test_radio_exchange import (
    _CALL_Q_TRANSCRIPTS,
    _CALL_THEN_QUESTION,
    Harness,
    _harness,
)


def _shadow_harness(tmp_path: Path, *, shadow_tx: bool, composer: Any) -> Harness:
    return _harness(
        tmp_path,
        _CALL_THEN_QUESTION,
        _CALL_Q_TRANSCRIPTS,
        composer=composer,
        tx_enabled=False,
        shadow_tx=shadow_tx,
    )


def test_stage_strings_are_config_derived() -> None:
    assert RadioSettings().stage() == "shadow"
    assert RadioSettings(shadow_tx=True).stage() == "shadow-tx"
    assert RadioSettings(tx_enabled=True).stage() == "live"
    assert RadioSettings(tx_enabled=True, shadow_tx=True).stage() == "live"  # tx wins


def test_shadow_stage_logs_drafts_and_never_keys(tmp_path: Path) -> None:
    h = _shadow_harness(tmp_path, shadow_tx=False, composer=lambda *a, **k: "up, over")
    # SC-002: zero RF — the PTT was never touched, nothing was played.
    assert h.ptt.key_calls == 0 and not h.ptt.keyed
    assert h.player.played == []
    # The full flow still ran: ack + answer drafts recorded with shadow dispositions.
    assert h.outbound() == [("ack", "shadow_logged"), ("answer", "shadow_logged")]
    # No kind='obi' transmissions exist — the feed shows only real channel traffic.
    assert all(r.kind != "obi" for r in h.rows())
    # No draft DMs in plain shadow (stage 0 is silent toward Slack about drafts).
    assert not any("draft" in n for n in h.notifier.notes)


def test_shadow_tx_stage_dms_the_drafts_in_real_time(tmp_path: Path) -> None:
    h = _shadow_harness(tmp_path, shadow_tx=True, composer=lambda *a, **k: "up, over")
    assert h.ptt.key_calls == 0 and h.player.played == []
    assert h.outbound() == [("ack", "shadow_dmed"), ("answer", "shadow_dmed")]
    drafts = [n for n in h.notifier.notes if "shadow-TX draft" in n]
    assert len(drafts) == 2
    assert any("go for obi" in n for n in drafts)
    assert any("up, over" in n for n in drafts)


def test_shadow_stage_records_trigger_decisions_for_tuning(tmp_path: Path) -> None:
    h = _shadow_harness(tmp_path, shadow_tx=False, composer=lambda *a, **k: "up, over")
    fired = [r for r in h.rows() if r.trigger_fired]
    assert len(fired) == 1
    assert fired[0].trigger_reason == "name_at_start"
    assert fired[0].caller == "nono"
