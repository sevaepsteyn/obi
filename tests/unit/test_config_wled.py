"""Unit (feature 103): parsing the ``[wled]`` section — the lighting write allowlist.

Asserts the section is None when absent/empty (the feature stays cleanly inactive), the
documented defaults, custom values round-trip, URL normalization, and that a malformed
allowlist fails at load — never at write time. Also pins the snapshot-path XDG helper.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from obi.core.config import ConfigError, default_wled_snapshots_path, load_config


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    logs = f'[logs]\ndb_path = "{tmp_path}/logs.db"\n\n'
    path.write_text(logs + body)
    path.chmod(0o600)
    return path


def test_absent_wled_is_none(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, ""))
    assert cfg.wled is None


def test_wled_without_devices_is_none(tmp_path: Path) -> None:
    # A bare/tuning-only section without an allowlist means "no lighting device configured".
    cfg = load_config(_write(tmp_path, "[wled]\ntimeout = 3.0\n"))
    assert cfg.wled is None


def test_wled_empty_devices_is_none(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, "[wled]\ndevices = {}\n"))
    assert cfg.wled is None


def test_wled_devices_parsed_with_defaults(tmp_path: Path) -> None:
    cfg = load_config(
        _write(
            tmp_path,
            """
[wled.devices]
portal = "https://portal-gw.tailnet.example.net"
""",
        )
    )
    assert cfg.wled is not None
    assert cfg.wled.devices == {"portal": "https://portal-gw.tailnet.example.net"}
    assert cfg.wled.timeout == 5.0
    assert cfg.wled.verify_tls is True


def test_wled_custom_values_and_url_normalization(tmp_path: Path) -> None:
    cfg = load_config(
        _write(
            tmp_path,
            """
[wled]
timeout = 2.5
verify_tls = false

[wled.devices]
portal = "http://172.21.165.64/"
""",
        )
    )
    assert cfg.wled is not None
    # Trailing slash is stripped so the driver can append /json/... paths uniformly.
    assert cfg.wled.devices == {"portal": "http://172.21.165.64"}
    assert cfg.wled.timeout == 2.5
    assert cfg.wled.verify_tls is False


def test_wled_rejects_non_http_url(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="portal"):
        load_config(_write(tmp_path, '[wled.devices]\nportal = "172.21.165.64"\n'))


def test_wled_rejects_non_table_devices(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="devices"):
        load_config(_write(tmp_path, '[wled]\ndevices = ["https://portal.example"]\n'))


def test_snapshots_path_honors_xdg_state_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert default_wled_snapshots_path() == tmp_path / "state" / "obi" / "wled_snapshots.json"


# --- [wled.segments.<device>] (feature 128) ---------------------------------------------

_PORTAL = '[wled.devices]\nportal = "https://portal.example"\n\n'


def test_segments_absent_is_empty(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, _PORTAL))
    assert cfg.wled is not None
    assert cfg.wled.segments == {}


def test_segments_parsed(tmp_path: Path) -> None:
    cfg = load_config(
        _write(tmp_path, _PORTAL + "[wled.segments.portal]\nstatus = [0, 2, 4]\nmain = [1, 3, 5]\n")
    )
    assert cfg.wled is not None
    layout = cfg.wled.segments["portal"]
    assert layout.status == (0, 2, 4)
    assert layout.main == (1, 3, 5)


def test_segments_unknown_device_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"\[wled\.segments\.tower\] does not match"):
        load_config(
            _write(tmp_path, _PORTAL + "[wled.segments.tower]\nstatus = [0]\nmain = [1]\n")
        )


def test_segments_empty_role_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"\[wled\.segments\.portal\] status"):
        load_config(
            _write(tmp_path, _PORTAL + "[wled.segments.portal]\nstatus = []\nmain = [1]\n")
        )


def test_segments_missing_role_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"\[wled\.segments\.portal\] main"):
        load_config(_write(tmp_path, _PORTAL + "[wled.segments.portal]\nstatus = [0]\n"))


def test_segments_non_int_ids_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"\[wled\.segments\.portal\] status"):
        load_config(
            _write(tmp_path, _PORTAL + '[wled.segments.portal]\nstatus = ["0"]\nmain = [1]\n')
        )


def test_segments_negative_ids_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"\[wled\.segments\.portal\] main .* >= 0"):
        load_config(
            _write(tmp_path, _PORTAL + "[wled.segments.portal]\nstatus = [0]\nmain = [-1]\n")
        )


def test_segments_overlap_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"disjoint.*both claim: 2"):
        load_config(
            _write(
                tmp_path, _PORTAL + "[wled.segments.portal]\nstatus = [0, 2]\nmain = [2, 3]\n"
            )
        )
