"""Unit tests: the optional ``[web]`` config section (feature 033, was ``[ar]`` in 022).

The section rename is HARD (fleet precedent: ``[mikrotik]`` → ``[routeros]``): a leftover
``[ar]`` section is a loud :class:`ConfigError` naming the rename — silently ignoring it
would silently drop the operator's port/voice/GNIS settings. Same for the retired
``[voice] web_port``/``web_bind`` keys (the 8444 talk page is gone; the origin is
``[web] port``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from obi.core.config import ConfigError, WebSettings, default_web_dir, load_config


def _write_config(tmp_path: Path, body: str) -> Path:
    config = tmp_path / "config.toml"
    config.write_text("[logs]\n" + body)  # [logs] satisfies the >=1-source gate
    config.chmod(0o600)
    return config


def test_web_defaults_when_section_absent(tmp_path: Path) -> None:
    config = load_config(_write_config(tmp_path, ""))
    assert config.web == WebSettings()
    assert config.web.export_path == default_web_dir() / "links.json"
    assert config.web.amsl_tolerance == 150.0
    assert config.web.default_mast_height == 10.0
    assert config.web.default_ground_elevation == 0.0  # 137: no built-in site elevation
    assert config.web.degraded_link_score == 0.5
    assert config.web.ground_overrides == {}
    assert config.web.port == 8443
    assert config.web.bind == "0.0.0.0"


def test_web_defaults_when_section_empty(tmp_path: Path) -> None:
    config = load_config(_write_config(tmp_path, "[web]\n"))
    assert config.web == WebSettings()


def test_web_state_dir_path_is_unchanged_by_the_rename() -> None:
    """R3: the on-disk state dir stays ``…/obi/ar`` — the accepted certificate and the
    durable elevation/DEM/GNIS caches must survive the CLI rename."""

    assert default_web_dir().parts[-2:] == ("obi", "ar")


def test_web_every_override(tmp_path: Path) -> None:
    config = load_config(
        _write_config(
            tmp_path,
            "\n".join(
                [
                    "[web]",
                    'export_path = "~/somewhere/geometry.json"',
                    "amsl_tolerance = 200.5",
                    "default_mast_height = 6",
                    "default_ground_elevation = 1200",
                    "degraded_link_score = 0.3",
                    "port = 9443",
                    'bind = "172.21.165.10"',
                    "[web.ground_overrides]",
                    '"Basswood Relay - East" = 1370.0',
                    '"BRC - NOC" = 1190',
                ]
            ),
        )
    )
    assert config.web.export_path == Path("~/somewhere/geometry.json").expanduser()
    assert config.web.amsl_tolerance == 200.5
    assert config.web.default_mast_height == 6.0
    assert config.web.default_ground_elevation == 1200.0
    assert config.web.degraded_link_score == 0.3
    assert config.web.port == 9443
    assert config.web.bind == "172.21.165.10"
    assert config.web.ground_overrides == {
        "Basswood Relay - East": 1370.0,
        "BRC - NOC": 1190.0,
    }


def test_web_non_numeric_ground_override_is_a_config_error(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path, '[web.ground_overrides]\n"Basswood Relay - East" = "high"\n'
    )
    with pytest.raises(ConfigError, match="ground_overrides"):
        load_config(config_path)


def test_web_gnis_states_default_empty(tmp_path: Path) -> None:
    config = load_config(_write_config(tmp_path, "[web]\n"))
    assert config.web.gnis_states == []


def test_web_gnis_states_normalize_to_upper(tmp_path: Path) -> None:
    config = load_config(_write_config(tmp_path, '[web]\ngnis_states = ["nv", "CA"]\n'))
    assert config.web.gnis_states == ["NV", "CA"]


def test_web_gnis_states_reject_non_state_codes(tmp_path: Path) -> None:
    for bad in ('gnis_states = "NV"', 'gnis_states = ["NEV"]', "gnis_states = [7]"):
        with pytest.raises(ConfigError, match="gnis_states"):
            load_config(_write_config(tmp_path, f"[web]\n{bad}\n"))


def test_web_voice_defaults_on(tmp_path: Path) -> None:
    """048: voice mounts by default (the --voice flag was removed); absent/empty sections are on."""

    assert load_config(_write_config(tmp_path, "")).web.voice is True
    assert load_config(_write_config(tmp_path, "[web]\n")).web.voice is True


def test_web_voice_opt_out(tmp_path: Path) -> None:
    """048: `[web] voice = false` is the explicit opt-out for a host that wants no voice engine."""

    config = load_config(_write_config(tmp_path, "[web]\nvoice = false\n"))
    assert config.web.voice is False


# --- the hard rename: loud errors, never silent drift (033 FR-008) ---------------------------


def test_leftover_ar_section_is_a_config_error_naming_the_rename(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"\[ar\].*renamed.*\[web\]"):
        load_config(_write_config(tmp_path, "[ar]\nport = 9443\n"))


def test_ar_section_alongside_web_is_still_an_error(tmp_path: Path) -> None:
    """Both sections present is just as wrong — the old one must be deleted, not shadowed."""

    with pytest.raises(ConfigError, match=r"\[ar\].*renamed.*\[web\]"):
        load_config(_write_config(tmp_path, "[web]\nport = 9443\n[ar]\nport = 8443\n"))


def test_retired_voice_web_port_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"web_port.*\[web\] port"):
        load_config(_write_config(tmp_path, "[voice]\nweb_port = 8444\n"))


def test_retired_voice_web_bind_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"web_bind.*\[web\] bind"):
        load_config(_write_config(tmp_path, '[voice]\nweb_bind = "0.0.0.0"\n'))


def test_kept_voice_web_engine_keys_still_parse(tmp_path: Path) -> None:
    """``web_idle_minutes``/``web_max_seconds`` shape the voice ENGINE (conversation expiry,
    longest recording), not the hosting — they stay in ``[voice]`` and keep working."""

    config = load_config(
        _write_config(tmp_path, "[voice]\nweb_idle_minutes = 45\nweb_max_seconds = 60\n")
    )
    assert config.voice.web_idle_minutes == 45
    assert config.voice.web_max_seconds == 60


def test_web_ospf_layout_path_defaults_none_and_parses(tmp_path: Path) -> None:
    """086: unset ⇒ None (blessed layout off, computed seeds exactly as before); a string
    parses as an expanded Path."""

    assert load_config(_write_config(tmp_path, "")).web.ospf_layout_path is None
    assert load_config(_write_config(tmp_path, "[web]\n")).web.ospf_layout_path is None
    config = load_config(
        _write_config(tmp_path, '[web]\nospf_layout_path = "~/ospf-layout.json"\n')
    )
    assert config.web.ospf_layout_path == Path("~/ospf-layout.json").expanduser()


def test_web_ospf_layout_path_non_string_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="ospf_layout_path"):
        load_config(_write_config(tmp_path, "[web]\nospf_layout_path = 7\n"))


def test_web_include_siklu_defaults_on_and_parses_off(tmp_path: Path) -> None:
    """044: the e-band merge is opt-OUT — configured [siklu] radios should appear in the
    viewer without extra config; include_siklu = false disables the export step."""

    assert load_config(_write_config(tmp_path, "")).web.include_siklu is True
    assert load_config(_write_config(tmp_path, "[web]\n")).web.include_siklu is True
    off = load_config(_write_config(tmp_path, "[web]\ninclude_siklu = false\n"))
    assert off.web.include_siklu is False


def test_web_site_towers_default_empty(tmp_path: Path) -> None:
    """097: unset ⇒ {} (towers off, per-device placement exactly as before)."""

    assert load_config(_write_config(tmp_path, "")).web.site_towers == {}
    assert load_config(_write_config(tmp_path, "[web]\n")).web.site_towers == {}


def test_web_site_towers_accept_meters_and_unit_strings(tmp_path: Path) -> None:
    """097: bare numbers are meters; strings carry a unit — feet convert at exactly
    0.3048 m/ft (the operator quotes big towers in feet, small masts in meters)."""

    config = load_config(
        _write_config(
            tmp_path,
            "[web.site_towers]\n"
            '"BRC - NOC" = "60 ft"\n'
            '"Tamarack" = "100ft"\n'
            '"Ironbark\'s Chestnut" = "5 m"\n'
            '"Basswood Relay - West" = 2\n'
            '"Hotel" = 10.5\n',
        )
    )
    towers = config.web.site_towers
    assert towers["BRC - NOC"] == pytest.approx(18.288)
    assert towers["Tamarack"] == pytest.approx(30.48)
    assert towers["Ironbark's Chestnut"] == pytest.approx(5.0)
    assert towers["Basswood Relay - West"] == pytest.approx(2.0)
    assert towers["Hotel"] == pytest.approx(10.5)


def test_web_site_towers_reject_invalid_heights(tmp_path: Path) -> None:
    """097: a silently-dropped tower would quietly scatter a site's radios again — every
    bad entry is a ConfigError naming the site."""

    for bad in (
        '"Tamarack" = "100"',  # unitless string is ambiguous
        '"Tamarack" = "60 yd"',  # unknown unit
        '"Tamarack" = 0',  # not a height
        '"Tamarack" = -5',
        '"Tamarack" = "0 ft"',
        '"Tamarack" = true',  # boolean is not a height
        '"Tamarack" = ["60 ft"]',
    ):
        with pytest.raises(ConfigError, match="site_towers"):
            load_config(_write_config(tmp_path, f"[web.site_towers]\n{bad}\n"))


def test_web_site_towers_non_table_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="site_towers"):
        load_config(_write_config(tmp_path, "[web]\nsite_towers = 5\n"))


def test_web_brc_follows_the_brc_section_and_parses_off(tmp_path: Path) -> None:
    """098 + 137: the city-grid step runs iff the city plan is configured ([brc]); an explicit
    brc = false still skips it entirely."""

    brc = "[brc]\norigin_lat = 45.0\norigin_lon = -100.0\nyear = 2030\n"
    assert load_config(_write_config(tmp_path, "")).web.brc is False
    assert load_config(_write_config(tmp_path, "[web]\n")).web.brc is False
    assert load_config(_write_config(tmp_path, brc)).web.brc is True
    assert load_config(_write_config(tmp_path, brc + "[web]\nbrc = false\n")).web.brc is False
    assert load_config(_write_config(tmp_path, "[web]\nbrc = false\n")).web.brc is False
