"""Unit (feature 095): parsing the ``[dash]`` section (+ ``[dash.pager]``, ``[[dash.wan_links]]``).

Asserts the section is None when absent (the feature stays inactive everywhere), the documented
defaults, custom values round-trip, and that every out-of-range bound fails at load — never
mid-cycle in the minute cron. Also pins the config↔dash signal-tuple mirror and the [dash.pager]
→ [slack] cross-section rule.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from obi.core import dash as dash_mod
from obi.core.config import _DASH_PAGEABLE_SIGNALS, _DASH_SIGNALS, ConfigError, load_config


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    logs = f'[logs]\ndb_path = "{tmp_path}/logs.db"\n\n'
    path.write_text(logs + body)
    path.chmod(0o600)
    return path


def test_signal_tuples_mirror() -> None:
    # config._DASH_SIGNALS is inlined to avoid a config→dash import cycle; keep them identical.
    assert _DASH_SIGNALS == dash_mod.SIGNALS


def test_absent_dash_is_none(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, ""))
    assert cfg.dash is None


def test_dash_defaults(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, "[dash]\n"))
    dash = cfg.dash
    assert dash is not None
    assert dash.enabled is False
    assert dash.state_dir.name == "dash"
    assert dash.ping_router == ""  # 137: no built-in reference router
    assert dash.ping_refs == ["8.8.8.8", "1.1.1.1"]
    assert dash.ping_rtt_warn_ms == 50.0
    assert dash.ping_loss_warn_pct == 25.0
    assert dash.ping_down_after == 2
    assert dash.netwatch_loss_warn_pct == 5.0
    assert dash.netwatch_rtt_warn_ms == 25.0
    assert dash.netwatch_loss_window == "5m"
    assert dash.flap_window == "10m"
    assert dash.internet_warn_pct == 70.0
    assert dash.internet_down_pct == 90.0
    assert dash.internet_links == []
    assert dash.pager.enabled is False
    assert dash.pager.mass_ratio == 0.80
    # 121: defaults to the PAGEABLE subset, not every signal — mesh and lighting render
    # on the board but must never page.
    assert dash.pager.signals == list(_DASH_PAGEABLE_SIGNALS)
    assert dash.pager.link_grouping is True  # feature 106: incidents on by default
    assert dash.pager.announce_hold == "0s"  # first symptom pages immediately
    assert dash.pager.extra_sites == []


def test_dash_full_round_trip(tmp_path: Path) -> None:
    cfg = load_config(
        _write(
            tmp_path,
            '[dash]\nenabled = true\nping_router = "core-gw-1"\n'
            'ping_refs = ["9.9.9.9"]\nping_rtt_warn_ms = 80\n'
            'ping_down_after = 3\nnetwatch_loss_window = "10m"\nflap_window = "5m"\n'
            "internet_warn_pct = 60\ninternet_down_pct = 85\n"
            '[[dash.internet_links]]\ndevice = "tamarack-internet-gw"\n'
            'interface = "sfp-sfpplus5-sycamore"\nlabel = "sycamore"\ncapacity_mbps = 2500\n'
            '[[dash.internet_links]]\ndevice = "tamarack-internet-gw"\n'
            'interface = "sfp-sfpplus6-spruce"\ncapacity_mbps = 250\n'
            '[dash.pager]\nmass_ratio = 0.5\nsignals = ["fleet", "internet"]\n'
            'link_grouping = false\nannounce_hold = "2m"\n'
            'extra_sites = ["Gunter", "joshua"]\n',
        )
    )
    dash = cfg.dash
    assert dash is not None
    assert dash.enabled is True
    assert dash.ping_router == "core-gw-1"
    assert dash.ping_refs == ["9.9.9.9"]
    assert dash.ping_rtt_warn_ms == 80.0
    assert dash.ping_down_after == 3
    assert dash.netwatch_loss_window == "10m"
    assert dash.flap_window == "5m"
    assert [link.interface for link in dash.internet_links] == [
        "sfp-sfpplus5-sycamore",
        "sfp-sfpplus6-spruce",
    ]
    assert dash.internet_links[0].label == "sycamore"
    assert dash.internet_links[1].label == ""  # label optional
    assert dash.internet_links[0].capacity_mbps == 2500.0
    assert dash.pager.mass_ratio == 0.5
    assert dash.pager.signals == ["fleet", "internet"]
    assert dash.pager.link_grouping is False
    assert dash.pager.announce_hold == "2m"
    assert dash.pager.extra_sites == ["gunter", "joshua"]  # lowercased at load


def test_scalar_internet_ref_coerced_to_list(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, '[dash]\nping_refs = "8.8.8.8"\n'))
    assert cfg.dash is not None
    assert cfg.dash.ping_refs == ["8.8.8.8"]


@pytest.mark.parametrize(
    "body",
    [
        '[dash]\nflap_window = "soon"\n',
        '[dash]\nnetwatch_loss_window = "a while"\n',
        "[dash]\nping_down_after = 0\n",
        "[dash]\nping_rtt_warn_ms = 0\n",
        "[dash]\ninternet_warn_pct = 120\n",
        "[dash]\ninternet_warn_pct = 95\ninternet_down_pct = 90\n",  # warn above down
        "[dash]\nping_refs = []\n",
        '[dash]\ninternet_links = "nope"\n',
        '[[dash.internet_links]]\ndevice = "gw"\ninterface = "sfp1"\n',  # capacity missing
        '[[dash.internet_links]]\ndevice = "gw"\ninterface = "sfp1"\ncapacity_mbps = 0\n',
        "[dash.pager]\nmass_ratio = 0.0\n",
        "[dash.pager]\nmass_ratio = 1.5\n",
        '[dash.pager]\nsignals = "fleet"\n',  # strict list (087 convention)
        '[dash.pager]\nsignals = ["fleet", "weather"]\n',  # unknown signal
        '[dash.pager]\nannounce_hold = "soon"\n',  # bad duration (106)
        '[dash.pager]\nextra_sites = "gunter"\n',  # strict list (106)
        "[dash.pager]\nextra_sites = [1, 2]\n",  # strings only (106)
    ],
)
def test_dash_rejects_bad_values(tmp_path: Path, body: str) -> None:
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, body))


def test_pager_enabled_requires_slack_token(tmp_path: Path) -> None:
    # Cross-section rule: the pager delivers over the Slack DM path — fail at load, not in cron.
    with pytest.raises(ConfigError, match=r"\[dash.pager\]"):
        load_config(_write(tmp_path, "[dash]\n[dash.pager]\nenabled = true\n"))


def test_icmp_include_uisp_ptp_default_and_override(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, "[icmp]\n"))
    assert cfg.icmp is not None and cfg.icmp.include_uisp_ptp is True
    cfg = load_config(_write(tmp_path, "[icmp]\ninclude_uisp_ptp = false\n"))
    assert cfg.icmp is not None and cfg.icmp.include_uisp_ptp is False


# --- 121: [dash.mesh] and [dash.lighting] -------------------------------------------------


def test_dash_mesh_and_lighting_default_when_absent(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, "[dash]\n"))
    assert cfg.dash is not None
    assert cfg.dash.mesh.heard_yellow_after == "2h"
    assert cfg.dash.mesh.heard_red_after == "12h"
    assert cfg.dash.mesh.fix_yellow_after == "6h"
    # Slower than the 1-minute cycle on purpose — the mesh port is exclusive and the locate
    # path needs it (see DashMeshSettings.read_interval).
    assert cfg.dash.mesh.read_interval == "5m"
    assert cfg.dash.lighting.timeout is None  # inherits [wled] timeout


def test_dash_mesh_thresholds_round_trip(tmp_path: Path) -> None:
    cfg = load_config(
        _write(
            tmp_path,
            "[dash]\n[dash.mesh]\nheard_yellow_after = '30m'\nheard_red_after = '4h'\n"
            "fix_yellow_after = '90m'\nread_interval = '10m'\n",
        )
    )
    assert cfg.dash is not None
    assert cfg.dash.mesh.heard_yellow_after == "30m"
    assert cfg.dash.mesh.heard_red_after == "4h"
    assert cfg.dash.mesh.fix_yellow_after == "90m"
    assert cfg.dash.mesh.read_interval == "10m"


@pytest.mark.parametrize(
    "key", ["heard_yellow_after", "heard_red_after", "fix_yellow_after", "read_interval"]
)
def test_dash_mesh_rejects_a_malformed_duration(tmp_path: Path, key: str) -> None:
    with pytest.raises(ConfigError, match=key):
        load_config(_write(tmp_path, f"[dash]\n[dash.mesh]\n{key} = 'soon'\n"))


def test_dash_lighting_timeout_round_trips_and_validates(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, "[dash]\n[dash.lighting]\ntimeout = 2.5\n"))
    assert cfg.dash is not None and cfg.dash.lighting.timeout == 2.5
    with pytest.raises(ConfigError, match="timeout"):
        load_config(_write(tmp_path, "[dash]\n[dash.lighting]\ntimeout = 0\n"))


def test_dash_mesh_and_lighting_reject_non_tables(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"\[dash.mesh\]"):
        load_config(_write(tmp_path, "[dash]\nmesh = 3\n"))
    with pytest.raises(ConfigError, match=r"\[dash.lighting\]"):
        load_config(_write(tmp_path, "[dash]\nlighting = 'on'\n"))


# --- [dash.status_band] (feature 128) ---------------------------------------------------

_LAYOUT = (
    '[wled.devices]\nportal = "https://portal.example"\n\n'
    "[wled.segments.portal]\nstatus = [0, 2, 4]\nmain = [1, 3, 5]\n\n"
)


def test_status_band_default_off(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, "[dash]\n"))
    assert cfg.dash is not None
    assert cfg.dash.status_band.enabled is False
    assert cfg.dash.status_band.timeout is None


def test_status_band_round_trips_with_layout(tmp_path: Path) -> None:
    cfg = load_config(
        _write(tmp_path, _LAYOUT + "[dash.status_band]\nenabled = true\ntimeout = 3.5\n")
    )
    assert cfg.dash is not None
    assert cfg.dash.status_band.enabled is True
    assert cfg.dash.status_band.timeout == 3.5


def test_status_band_rejects_bad_timeout(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"\[dash\.status_band\] timeout"):
        load_config(_write(tmp_path, _LAYOUT + "[dash.status_band]\ntimeout = 0\n"))


def test_status_band_rejects_non_table(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"\[dash\.status_band\] must be a table"):
        load_config(_write(tmp_path, '[dash]\nstatus_band = "yes"\n'))


def test_status_band_enabled_requires_a_segment_layout(tmp_path: Path) -> None:
    # The 128 cross-section rule: a half-configured autonomous writer fails at load,
    # never silently no-ops in the minute cron.
    with pytest.raises(ConfigError, match=r"\[dash\.status_band\] enabled .* no \[wled\.segments"):
        load_config(_write(tmp_path, "[dash.status_band]\nenabled = true\n"))
    with pytest.raises(ConfigError, match=r"\[dash\.status_band\] enabled"):
        load_config(
            _write(
                tmp_path,
                '[wled.devices]\nportal = "https://portal.example"\n\n'
                "[dash.status_band]\nenabled = true\n",
            )
        )
