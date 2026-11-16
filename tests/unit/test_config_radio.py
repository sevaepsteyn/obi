"""Unit (feature 088): parsing the ``[radio]`` section.

Asserts absent ⇒ None, calibrated defaults (criticalrf 2026-08-19 reference), custom values,
out-of-range bounds failing at load (not mid-call), and the cross-section rule that enabling
TX or shadow-TX requires a resolvable chat model key.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from obi.core.config import ConfigError, RadioSettings, load_config


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    logs = f'[logs]\ndb_path = "{tmp_path}/logs.db"\n\n'
    path.write_text(logs + body)
    path.chmod(0o600)
    return path


def test_absent_radio_is_none(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, ""))
    assert cfg.radio is None


def test_radio_defaults_are_the_calibrated_reference(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, "[radio]\nftdi_serial = 'FX00TEST'\n"))
    r = cfg.radio
    assert r is not None
    assert r.audio_name == "USB PnP Sound Device"
    assert r.ftdi_serial == "FX00TEST"  # 137: the cable is identified from config only
    assert r.serial_port is None
    assert r.sample_rate == 48000
    assert r.squelch_dbfs == -45.0
    assert r.min_active_ms == 250
    assert r.hang_ms == 1200
    assert r.clear_window_ms == 700
    assert r.lead_ms == 300  # deliberately not the 1 s human guideline
    assert r.tail_ms == 200
    assert r.tx_cap_s == 15
    assert r.patience_s == 60
    assert r.call_timeout_s == 45
    assert r.compose_deadline_s == 20
    assert r.max_tx_per_minute == 6
    assert r.max_tx_per_hour == 60
    # 092: full scale. NO digital attenuation — the trim is the host output gain (0.90).
    # The 0.298/0.45/0.64 ladder chased an overdrive ceiling the 2026-08-21 drive-response
    # measurement showed does not exist (mic AGC compresses ~2.5:1 above a noise gate).
    assert r.tx_gain == 1.0
    assert r.tx_enabled is False  # THE gate: default off
    assert r.shadow_tx is False
    assert "obi" in r.name_variants and len(r.name_variants) >= 3
    assert r.operator_handles == []
    assert r.ack_text == "go for obi"
    assert r.unavailable_text == "unable to comply, obi out"
    assert r.say_again_text == "ten nine, say again"
    assert r.stt_model == "mlx-community/whisper-large-v3-turbo"
    assert r.tts_voice is None and r.tts_rate is None
    assert r.model is None  # None ⇒ inherit [chat].model (watch pattern)
    assert r.max_tokens == 600
    assert r.live_calls == 3
    assert r.slack_user is None
    assert r.db_path.name == "radio.db"
    assert r.clips_dir.name == "clips"
    assert r.retention_days == 0  # keep everything (the season behavior)


def test_radio_custom_values(tmp_path: Path) -> None:
    cfg = load_config(
        _write(
            tmp_path,
            "[radio]\n"
            'audio_name = "Other Codec"\nftdi_serial = "ZZ99"\nserial_port = "/dev/cu.x"\n'
            "squelch_dbfs = -50.5\nmin_active_ms = 300\nhang_ms = 900\nclear_window_ms = 1500\n"
            "lead_ms = 500\ntail_ms = 100\ntx_cap_s = 10\npatience_s = 45\ncall_timeout_s = 30\n"
            "compose_deadline_s = 15\nmax_tx_per_minute = 3\nmax_tx_per_hour = 20\n"
            "tx_gain = 0.5\ntx_enabled = false\nshadow_tx = false\n"
            'name_variants = ["obi", "toby"]\noperator_handles = ["nono", "Mira"]\n'
            'ack_text = "go ahead for obi"\nstt_model = "mlx-community/whisper-small"\n'
            'tts_voice = "Samantha"\ntts_rate = 170\nmodel = "claude-opus-5"\nmax_tokens = 400\n'
            'live_calls = 1\nslack_user = "U0FAKEDCE78"\nretention_days = 30\n',
        )
    )
    r = cfg.radio
    assert r is not None
    assert r.audio_name == "Other Codec"
    assert r.serial_port == "/dev/cu.x"
    assert r.squelch_dbfs == -50.5
    assert r.clear_window_ms == 1500
    assert r.tx_cap_s == 10
    assert r.tx_gain == 0.5
    assert r.name_variants == ["obi", "toby"]
    assert r.operator_handles == ["nono", "Mira"]
    assert r.ack_text == "go ahead for obi"
    assert r.tts_voice == "Samantha" and r.tts_rate == 170
    assert r.model == "claude-opus-5" and r.max_tokens == 400
    assert r.live_calls == 1
    assert r.slack_user == "U0FAKEDCE78"
    assert r.retention_days == 30


def test_radio_092_defaults(tmp_path: Path) -> None:
    """The TX loudness stage, the operator-request TTL, and the host-gain reference.

    The host gains are the calibrated reference obi compares the LIVE codec against; they
    are configuration rather than constants because the reference is hardware-specific
    (docs/development/radio-audio-chain.md). The loudness TARGETS are deliberately NOT
    here — they are module constants in radio/loudness.py, because they are the one thing
    that was actually validated by an on-air A/B.
    """

    cfg = load_config(_write(tmp_path, "[radio]\nftdi_serial = 'FX00TEST'\n"))
    r = cfg.radio
    assert r is not None
    assert r.tx_loudness is True
    assert r.operator_tx_ttl_s == 120
    assert r.host_input_gain == 0.0  # any boost clips: the PTT click peaks at -0.2 dBFS
    assert r.host_output_gain == 0.90  # -2.9 dB, the calibrated TX drive
    assert r.host_gain_tolerance == 0.02


def test_radio_093_defaults(tmp_path: Path) -> None:
    r = load_config(_write(tmp_path, "[radio]\nftdi_serial = 'FX00TEST'\n")).radio
    assert r is not None
    assert r.garble_close_text == "unable to copy, out"


def test_radio_093_custom_garble_close_text(tmp_path: Path) -> None:
    r = load_config(
        _write(tmp_path, '[radio]\nftdi_serial = "FX00TEST"\ngarble_close_text = "nothing heard"\n')
    ).radio
    assert r is not None
    assert r.garble_close_text == "nothing heard"


def test_radio_092_custom_values(tmp_path: Path) -> None:
    body = (
        "[radio]\nftdi_serial = 'FX00TEST'\n"
        "tx_loudness = false\n"
        "operator_tx_ttl_s = 45\n"
        "host_input_gain = 0.1\n"
        "host_output_gain = 1.0\n"
        "host_gain_tolerance = 0.05\n"
    )
    r = load_config(_write(tmp_path, body)).radio
    assert r is not None
    assert r.tx_loudness is False
    assert r.operator_tx_ttl_s == 45
    assert r.host_input_gain == 0.1
    assert r.host_output_gain == 1.0
    assert r.host_gain_tolerance == 0.05


def test_radio_stage_helper(tmp_path: Path) -> None:
    assert RadioSettings().stage() == "shadow"
    assert RadioSettings(shadow_tx=True).stage() == "shadow-tx"
    assert RadioSettings(tx_enabled=True).stage() == "live"
    # tx_enabled wins: shadow_tx is only meaningful while tx is off.
    assert RadioSettings(tx_enabled=True, shadow_tx=True).stage() == "live"


@pytest.mark.parametrize(
    "bad",
    [
        "squelch_dbfs = 0",  # must be negative dBFS
        "squelch_dbfs = -130",  # below any real floor
        "tx_gain = 0",  # (0, 1]
        "tx_gain = 1.5",
        "operator_tx_ttl_s = 0",  # 092: positive
        "operator_tx_ttl_s = -5",
        "host_input_gain = -0.1",  # 092: [0.0, 1.0]
        "host_input_gain = 1.1",
        "host_output_gain = -0.1",
        "host_output_gain = 1.1",
        "host_gain_tolerance = 0",  # 092: (0.0, 0.5]
        "host_gain_tolerance = 0.6",
        "tx_cap_s = 0",
        "tx_cap_s = 120",  # cap is capped: a 2-minute transmission is never legitimate
        "min_active_ms = 0",
        "hang_ms = 0",
        "clear_window_ms = 0",
        "lead_ms = -1",  # 0 is allowed; negative is not
        "tail_ms = -1",
        "patience_s = 0",
        "call_timeout_s = 0",
        "compose_deadline_s = 0",
        "max_tx_per_minute = 0",
        "max_tx_per_hour = 0",
        "live_calls = -1",
        "retention_days = -1",
        "max_tokens = 0",
        "name_variants = []",  # trigger would be unable to fire, ever
        'stt_model = ""',
        "tts_rate = 0",
        "sample_rate = 0",
    ],
)
def test_radio_out_of_range_raises(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, f"[radio]\nftdi_serial = 'FX00TEST'\n{bad}\n"))


def test_radio_tx_requires_chat_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Cross-section rule: TX / shadow-TX compose replies with the cloud model — a config that
    # enables them with no resolvable key must fail at load, not mid-call on the air.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, "[radio]\nftdi_serial = 'FX00TEST'\ntx_enabled = true\n"))
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, "[radio]\nftdi_serial = 'FX00TEST'\nshadow_tx = true\n"))


def test_radio_tx_allowed_with_chat_key_in_config(tmp_path: Path) -> None:
    body = '[chat]\napi_key = "sk-test"\n\n[radio]\nftdi_serial = "FX00TEST"\ntx_enabled = true\n'
    cfg = load_config(_write(tmp_path, body))
    assert cfg.radio is not None and cfg.radio.tx_enabled is True


def test_radio_tx_allowed_with_env_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-env")
    cfg = load_config(_write(tmp_path, "[radio]\nftdi_serial = 'FX00TEST'\nshadow_tx = true\n"))
    assert cfg.radio is not None and cfg.radio.shadow_tx is True


def test_radio_listener_needs_no_chat_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Pure listener (both TX modes off) is fully local — no model key required.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg = load_config(_write(tmp_path, "[radio]\nftdi_serial = 'FX00TEST'\n"))
    assert cfg.radio is not None


def test_defaults_constructible_without_toml() -> None:
    r = RadioSettings()
    assert r.tx_enabled is False
    assert r.stage() == "shadow"


def test_radio_known_callers(tmp_path: Path) -> None:
    # Layer-1 roster (field lesson 2026-08-19): known channel callsigns feed the STT
    # vocabulary prompt and the fuzzy caller matcher. Casing is preserved — the prompt
    # wants "ZFX"/"Za-zu" as spoken-about, matching lowercases on its own.
    cfg = load_config(
        _write(
            tmp_path,
            '[radio]\nftdi_serial = "FX00TEST"\n'
            'known_callers = ["Cat", "Sawdust", "Za-zu", "ZFX"]\n',
        )
    )
    assert cfg.radio is not None
    assert cfg.radio.known_callers == ["Cat", "Sawdust", "Za-zu", "ZFX"]


def test_radio_known_callers_default_empty(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, "[radio]\nftdi_serial = 'FX00TEST'\n"))
    assert cfg.radio is not None
    assert cfg.radio.known_callers == []


def test_radio_vocab_terms(tmp_path: Path) -> None:
    cfg = load_config(
        _write(
            tmp_path,
            '[radio]\nftdi_serial = "FX00TEST"\nvocab_terms = ["NOC", "manzanita", "mountain"]\n',
        )
    )
    assert cfg.radio is not None
    assert cfg.radio.vocab_terms == ["NOC", "manzanita", "mountain"]
    assert RadioSettings().vocab_terms == []


def test_radio_splitter_settings(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, "[radio]\nftdi_serial = 'FX00TEST'\n"))
    assert cfg.radio is not None
    assert cfg.radio.inter_tx_floor_dbfs == -60.0
    assert cfg.radio.split_quiet_ms == 300
    cfg = load_config(
        _write(
            tmp_path,
            "[radio]\nftdi_serial = 'FX00TEST'\ninter_tx_floor_dbfs = -65\nsplit_quiet_ms = 400\n",
        )
    )
    assert cfg.radio is not None
    assert cfg.radio.inter_tx_floor_dbfs == -65.0
    assert cfg.radio.split_quiet_ms == 400


def test_radio_floor_must_be_below_squelch(tmp_path: Path) -> None:
    # A floor at/above squelch would split on every speech pause — refused at load.
    with pytest.raises(ConfigError):
        load_config(
            _write(tmp_path, "[radio]\nftdi_serial = 'FX00TEST'\ninter_tx_floor_dbfs = -45\n")
        )
    with pytest.raises(ConfigError):
        load_config(
            _write(
                tmp_path,
                "[radio]\nftdi_serial = 'FX00TEST'\nsquelch_dbfs = -50\n"
                "inter_tx_floor_dbfs = -48\n",
            )
        )
    with pytest.raises(ConfigError):
        load_config(
            _write(tmp_path, "[radio]\nftdi_serial = 'FX00TEST'\ninter_tx_floor_dbfs = 5\n")
        )
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, "[radio]\nftdi_serial = 'FX00TEST'\nsplit_quiet_ms = 0\n"))


def test_radio_standby_and_pronunciations(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, "[radio]\nftdi_serial = 'FX00TEST'\n"))
    assert cfg.radio is not None
    assert cfg.radio.standby_text == "stand by"
    assert cfg.radio.standby_after_s == 4
    assert cfg.radio.pronunciations == {}  # 137: no built-in site pronunciation
    cfg = load_config(
        _write(
            tmp_path,
            '[radio]\nftdi_serial = "FX00TEST"\nstandby_text = "one moment"\nstandby_after_s = 6\n'
            '[radio.pronunciations]\nNOC = "knock"\nUISP = "you isp"\n',
        )
    )
    assert cfg.radio is not None
    assert cfg.radio.standby_text == "one moment"
    assert cfg.radio.standby_after_s == 6
    assert cfg.radio.pronunciations == {"NOC": "knock", "UISP": "you isp"}


def test_radio_standby_after_zero_is_valid(tmp_path: Path) -> None:
    # 0 = standby as soon as composing starts (operator direction 2026-08-20).
    cfg = load_config(_write(tmp_path, "[radio]\nftdi_serial = 'FX00TEST'\nstandby_after_s = 0\n"))
    assert cfg.radio is not None
    assert cfg.radio.standby_after_s == 0


def test_radio_speech_aliases(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, "[radio]\nftdi_serial = 'FX00TEST'\n"))
    assert cfg.radio is not None
    assert cfg.radio.speech_aliases == {}  # 137: no built-in site alias
    cfg = load_config(
        _write(
            tmp_path,
            '[radio]\nftdi_serial = "FX00TEST"\n[radio.speech_aliases]\nknock = "NOC"\n'
            'north = "NOC"\n',
        )
    )
    assert cfg.radio is not None
    assert cfg.radio.speech_aliases == {"knock": "NOC", "north": "NOC"}


def test_radio_slack_call_notes(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, "[radio]\nftdi_serial = 'FX00TEST'\n"))
    assert cfg.radio is not None
    assert cfg.radio.slack_call_notes is True  # per-call DMs are the spec'd default
    cfg = load_config(
        _write(tmp_path, "[radio]\nftdi_serial = 'FX00TEST'\nslack_call_notes = false\n")
    )
    assert cfg.radio is not None
    assert cfg.radio.slack_call_notes is False


def test_radio_caller_aliases(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, "[radio]\nftdi_serial = 'FX00TEST'\n"))
    assert cfg.radio is not None
    assert cfg.radio.caller_aliases == {}  # site-specific — no defaults
    cfg = load_config(
        _write(
            tmp_path, '[radio]\nftdi_serial = "FX00TEST"\n[radio.caller_aliases]\nseven = "Mira"\n'
        )
    )
    assert cfg.radio is not None
    assert cfg.radio.caller_aliases == {"seven": "Mira"}


def test_radio_name_variants_weak_default(tmp_path: Path) -> None:
    # Feature 109: the weak tier is seeded from mined mis-hearings (ovi/obini/okie).
    # These fire only on a doubled hail — "okie" collides with "okie dokie" otherwise.
    cfg = load_config(_write(tmp_path, "[radio]\nftdi_serial = 'FX00TEST'\n"))
    assert cfg.radio is not None
    assert cfg.radio.name_variants_weak == ["ovi", "obini", "okie"]


def test_radio_name_variants_weak_custom(tmp_path: Path) -> None:
    cfg = load_config(
        _write(
            tmp_path, '[radio]\nftdi_serial = "FX00TEST"\nname_variants_weak = ["ovi", "toby"]\n'
        )
    )
    assert cfg.radio is not None
    assert cfg.radio.name_variants_weak == ["ovi", "toby"]


def test_radio_name_variants_weak_may_be_empty(tmp_path: Path) -> None:
    # Unlike name_variants (the trigger could never fire), an empty weak tier is a
    # legitimate opt-out — strong variants still fire.
    cfg = load_config(
        _write(tmp_path, "[radio]\nftdi_serial = 'FX00TEST'\nname_variants_weak = []\n")
    )
    assert cfg.radio is not None
    assert cfg.radio.name_variants_weak == []


def test_radio_sign_offs(tmp_path: Path) -> None:
    # Feature 109: channel-specific sign-off phrases join the built-in clearing shapes.
    cfg = load_config(
        _write(
            tmp_path,
            '[radio]\nftdi_serial = "FX00TEST"\nsign_offs = ["back on IT", "gone riding"]\n',
        )
    )
    assert cfg.radio is not None
    assert cfg.radio.sign_offs == ["back on IT", "gone riding"]


def test_radio_sign_offs_default_empty(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, "[radio]\nftdi_serial = 'FX00TEST'\n"))
    assert cfg.radio is not None
    assert cfg.radio.sign_offs == []


def test_radio_watchdog_defaults(tmp_path: Path) -> None:
    # Feature 113: capture watchdog on by default; thresholds are the field-derived
    # reference (specs/113-radio-capture-watchdog/data-model.md).
    cfg = load_config(_write(tmp_path, "[radio]\nftdi_serial = 'FX00TEST'\n"))
    r = cfg.radio
    assert r is not None
    assert r.watchdog is True
    assert r.watchdog_flatline_dbfs == -100.0
    assert r.watchdog_flatline_s == 120
    assert r.watchdog_fade_window == 20
    assert r.watchdog_fade_min == 10
    assert r.watchdog_fade_peak_dbfs == -10.0
    assert r.watchdog_fade_span_s == 43200
    assert r.watchdog_floor_s == 600
    assert r.watchdog_max_reopens == 2
    assert r.watchdog_escalation_s == 3600
    assert r.watchdog_stuck_s == 300


def test_radio_watchdog_custom_and_disable(tmp_path: Path) -> None:
    cfg = load_config(
        _write(
            tmp_path,
            "[radio]\nftdi_serial = 'FX00TEST'\n"
            "watchdog = false\n"
            "watchdog_flatline_s = 60\n"
            "watchdog_fade_peak_dbfs = -6.0\n"
            "watchdog_floor_s = 0\n",
        )
    )
    r = cfg.radio
    assert r is not None
    assert r.watchdog is False  # W5: disabled must round-trip exactly
    assert r.watchdog_flatline_s == 60
    assert r.watchdog_fade_peak_dbfs == -6.0
    assert r.watchdog_floor_s == 0


@pytest.mark.parametrize(
    "bad",
    [
        "watchdog_flatline_dbfs = 3.0",
        "watchdog_flatline_dbfs = -130.0",
        "watchdog_fade_peak_dbfs = 0.0",
        "watchdog_flatline_s = 0",
        "watchdog_floor_s = -1",
        "watchdog_fade_window = 5\nwatchdog_fade_min = 6",
    ],
)
def test_radio_watchdog_out_of_range_raises(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, f"[radio]\nftdi_serial = 'FX00TEST'\n{bad}\n"))


# --- feature 122: preventive capture reopen keys --------------------------------------------


def test_radio_capture_reopen_defaults(tmp_path: Path) -> None:
    # Operator cadence decision 2026-09-01: per-transmission on by default, with the
    # 2 h backstop replacing the external 04:20/16:20 restarts.
    cfg = load_config(_write(tmp_path, "[radio]\nftdi_serial = 'FX00TEST'\n"))
    r = cfg.radio
    assert r is not None
    assert r.capture_reopen_after_tx is True
    assert r.capture_reopen_floor_s == 60
    assert r.capture_reopen_quiet_ms == 2000
    assert r.capture_reopen_max_uptime_s == 7200


def test_radio_capture_reopen_custom_and_off(tmp_path: Path) -> None:
    cfg = load_config(
        _write(
            tmp_path,
            "[radio]\nftdi_serial = 'FX00TEST'\n"
            "capture_reopen_after_tx = false\n"
            "capture_reopen_floor_s = 300\n"
            "capture_reopen_quiet_ms = 5000\n"
            "capture_reopen_max_uptime_s = 0\n",
        )
    )
    r = cfg.radio
    assert r is not None
    # R10: after_tx off + backstop 0 = the feature fully off; must round-trip exactly.
    assert r.capture_reopen_after_tx is False
    assert r.capture_reopen_floor_s == 300
    assert r.capture_reopen_quiet_ms == 5000
    assert r.capture_reopen_max_uptime_s == 0


@pytest.mark.parametrize(
    "bad",
    [
        "capture_reopen_floor_s = 9",  # below the never-machine-gun floor
        "capture_reopen_floor_s = 0",
        "capture_reopen_quiet_ms = 0",
        "capture_reopen_quiet_ms = 20000",  # cannot exceed the LevelMonitor's 10 s ring
        "capture_reopen_max_uptime_s = -1",
        "capture_reopen_max_uptime_s = 30",  # 0 < backstop < floor contradicts the floor
    ],
)
def test_radio_capture_reopen_out_of_range_raises(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, f"[radio]\nftdi_serial = 'FX00TEST'\n{bad}\n"))


def test_radio_124_outbound_defaults(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, "[radio]\nftdi_serial = 'FX00TEST'\n"))
    r = cfg.radio
    assert r is not None
    assert r.outbound_ack_window_s == 30
    assert r.outbound_max_rehails == 2
    assert r.outbound_ttl_s == 600
    assert r.outbound_max_chars == 320


def test_radio_124_outbound_custom_values(tmp_path: Path) -> None:
    cfg = load_config(
        _write(
            tmp_path,
            "[radio]\nftdi_serial = 'FX00TEST'\noutbound_ack_window_s = 20\n"
            "outbound_max_rehails = 0\n"
            "outbound_ttl_s = 300\noutbound_max_chars = 200\n",
        )
    )
    r = cfg.radio
    assert r is not None
    assert r.outbound_ack_window_s == 20
    assert r.outbound_max_rehails == 0  # 0 = a single hail, no re-hails
    assert r.outbound_ttl_s == 300
    assert r.outbound_max_chars == 200


@pytest.mark.parametrize(
    "bad",
    [
        "outbound_ack_window_s = 0",
        "outbound_max_rehails = -1",
        "outbound_ttl_s = 0",
        "outbound_max_chars = 0",
    ],
)
def test_radio_124_outbound_out_of_range_raises(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, f"[radio]\nftdi_serial = 'FX00TEST'\n{bad}\n"))


# -- feature 133: garble-triggered reopen keys --------------------------------------------


def test_radio_garble_reopen_defaults(tmp_path: Path) -> None:
    # Operator decisions 2026-09-07: on by default; a fresh stream (< 60 s) is not
    # suspected; at most six executed garble reopens per rolling hour.
    cfg = load_config(_write(tmp_path, "[radio]\nftdi_serial = 'FX00TEST'\n"))
    r = cfg.radio
    assert r is not None
    assert r.garble_reopen is True
    assert r.garble_reopen_min_age_s == 60
    assert r.garble_reopen_max_per_hour == 6


def test_radio_garble_reopen_custom_and_off_without_watchdog(tmp_path: Path) -> None:
    # Independent of `watchdog` (absent here = the deployed false); G10: off must
    # round-trip exactly — it is the rollback line.
    cfg = load_config(
        _write(
            tmp_path,
            "[radio]\nftdi_serial = 'FX00TEST'\n"
            "garble_reopen = false\n"
            "garble_reopen_min_age_s = 0\n"
            "garble_reopen_max_per_hour = 1\n",
        )
    )
    r = cfg.radio
    assert r is not None
    assert r.garble_reopen is False
    assert r.garble_reopen_min_age_s == 0
    assert r.garble_reopen_max_per_hour == 1


@pytest.mark.parametrize(
    "bad",
    [
        "garble_reopen_min_age_s = -1",
        "garble_reopen_max_per_hour = 0",  # the cap must allow at least one
    ],
)
def test_radio_garble_reopen_out_of_range_raises(tmp_path: Path, bad: str) -> None:
    key = bad.split(" = ")[0]
    with pytest.raises(ConfigError, match=key):
        load_config(_write(tmp_path, f"[radio]\nftdi_serial = 'FX00TEST'\n{bad}\n"))
