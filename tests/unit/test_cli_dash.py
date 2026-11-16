"""Unit (feature 095): the `obi dash` CLI — run (cycle, guards, lock) and show (render, JSON).

Everything runs offline: an empty inventory gives the cycle zero probe targets, so `dash run`
exercises the full collect→evaluate→persist path without a single packet, and `dash show`
renders boards written straight through the core model.
"""

from __future__ import annotations

import fcntl
import json
from pathlib import Path

from typer.testing import CliRunner

from obi.cli.main import app
from obi.core import dash

runner = CliRunner()

NOW_US = 1_787_000_000_000_000


def _write_config(tmp_path: Path, *, dash_section: str) -> Path:
    config = tmp_path / "config.toml"
    config.write_text(
        "[logs]\n"
        f'db_path = "{tmp_path / "logs.db"}"\n'
        f'\n[inventory]\npath = "{tmp_path / "inventory.json"}"\n' + dash_section
    )
    config.chmod(0o600)
    return config


def _dash_section(tmp_path: Path, *, enabled: bool = True) -> str:
    return (
        f'\n[dash]\nenabled = {"true" if enabled else "false"}\nstate_dir = "{tmp_path / "dash"}"\n'
    )


# --- dash run ----------------------------------------------------------------------------------


def test_run_without_dash_section_exits_2_with_guidance(tmp_path: Path) -> None:
    config = _write_config(tmp_path, dash_section="")
    result = runner.invoke(app, ["dash", "run", "--scope", str(config)])
    assert result.exit_code == 2
    assert "dashboard not configured" in result.output


def test_run_disabled_is_a_clean_noop(tmp_path: Path) -> None:
    # Disabled-but-present is a parked cron, not an error (analysis finding U4).
    config = _write_config(tmp_path, dash_section=_dash_section(tmp_path, enabled=False))
    result = runner.invoke(app, ["dash", "run", "--scope", str(config)])
    assert result.exit_code == 0
    assert "disabled" in result.output
    assert not (tmp_path / "dash" / dash.BOARD_FILE).exists()  # no board write while parked


def test_run_offline_writes_an_honest_board(tmp_path: Path) -> None:
    # No inventory ⇒ zero targets ⇒ every tile is stale-with-hint — but the board IS written.
    config = _write_config(tmp_path, dash_section=_dash_section(tmp_path))
    result = runner.invoke(app, ["dash", "run", "--scope", str(config)])
    assert result.exit_code == 0
    board = dash.load_board(tmp_path / "dash")
    assert board is not None
    # Never green off missing data: the data tiles are stale, and the pipeline tile (US4)
    # names the never-run collectors, so the rollup lands at yellow.
    assert board.state in ("stale", "yellow")
    assert all(t.state != "green" or t.signal == "internet" for t in board.tiles)
    assert "no ICMP targets" in " ".join(board.notes)
    assert "dash:" in result.output


def test_run_emits_board_json(tmp_path: Path) -> None:
    config = _write_config(tmp_path, dash_section=_dash_section(tmp_path))
    result = runner.invoke(app, ["dash", "run", "--json", "--scope", str(config)])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert [t["signal"] for t in payload["tiles"]] == [
        s for s in dash.SIGNALS if s not in dash.OPTIONAL_SIGNALS
    ]  # 121: mesh/lighting are absent, not stale, when unconfigured


def test_run_skips_cleanly_when_lock_held(tmp_path: Path) -> None:
    config = _write_config(tmp_path, dash_section=_dash_section(tmp_path))
    state_dir = tmp_path / "dash"
    state_dir.mkdir()
    holder = (state_dir / "run.lock").open("a")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = runner.invoke(app, ["dash", "run", "--scope", str(config)])
    finally:
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()
    assert result.exit_code == 0
    assert "skipped" in result.output
    assert not (state_dir / dash.BOARD_FILE).exists()  # the skipping cycle wrote nothing


# --- dash show ---------------------------------------------------------------------------------


def _seed_board(state_dir: Path) -> dash.Board:
    tile = dash.Tile(
        signal="fleet",
        state="red",
        items=[
            dash.Item(
                key="tamarack-gw",
                label="tamarack-gw",
                state="red",
                detail="down 14m (172.18.89.1)",
            ),
            dash.Item(key="core-gw-1", label="core-gw-1", state="green", detail="up"),
        ],
        cadence_s=60,
        data_age_s=4.0,
    )
    board = dash.Board(generated_at_us=NOW_US, state="red", tiles=[tile], notes=["a note"])
    dash.write_board(state_dir, board)
    return board


def test_show_absent_board_prints_fixing_command(tmp_path: Path) -> None:
    config = _write_config(tmp_path, dash_section=_dash_section(tmp_path))
    result = runner.invoke(app, ["dash", "show", "--scope", str(config)])
    assert result.exit_code == 0
    assert "obi dash run" in result.output


def test_show_renders_tiles_and_attention(tmp_path: Path) -> None:
    config = _write_config(tmp_path, dash_section=_dash_section(tmp_path))
    _seed_board(tmp_path / "dash")
    result = runner.invoke(app, ["dash", "show", "--scope", str(config)])
    assert result.exit_code == 0
    assert "fleet" in result.output
    assert "tamarack-gw" in result.output  # the red item surfaces in the attention column
    assert "down 14m" in result.output
    assert "a note" in result.output
    assert "core-gw-1" not in result.output  # green items stay off the attention column


def test_show_json_dumps_board_verbatim(tmp_path: Path) -> None:
    config = _write_config(tmp_path, dash_section=_dash_section(tmp_path))
    seeded = _seed_board(tmp_path / "dash")
    result = runner.invoke(app, ["dash", "show", "--json", "--scope", str(config)])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == seeded.to_json()


def test_show_works_without_dash_section_via_default_dir(tmp_path: Path, monkeypatch) -> None:
    # show is read-only inspection: no [dash] section still renders the default-dir board.
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    config = _write_config(tmp_path, dash_section="")
    _seed_board(tmp_path / "state" / "obi" / "dash")
    result = runner.invoke(app, ["dash", "show", "--scope", str(config)])
    assert result.exit_code == 0
    assert "tamarack-gw" in result.output


# --- paging transport wiring (feature 102) -----------------------------------------------------


def _pager_config(tmp_path: Path, *, paging: bool, alerts_enabled: bool) -> Path:
    """A dash+pager config; optionally with the [paging] section and its alerts toggle."""

    section = _dash_section(tmp_path) + "\n[dash.pager]\nenabled = true\n"
    section += '\n[slack]\nbot_token = "xoxb-test"\napp_token = "xapp-test"\nallowlist = ["U1"]\n'
    if paging:
        section += (
            '\n[paging]\nurl = "https://pager.example.net/api.php"\nusername = "mira"\n'
            'password = "pw"\n'
            "\n[paging.alerts]\n"
            f"enabled = {'true' if alerts_enabled else 'false'}\n"
        )
        if alerts_enabled:
            section += 'people = ["101"]\n'
    return _write_config(tmp_path, dash_section=section)


class _StubNotifier:
    def deliver_status(self, text, label=None):  # noqa: ANN001, ANN202
        from obi.core.watch import DeliveryResult

        return DeliveryResult(delivered=1, failed=0)


def _stub_slack(monkeypatch) -> None:  # noqa: ANN001
    import obi.slack.notifier as notifier_mod

    monkeypatch.setattr(
        notifier_mod.SlackNotifier,
        "from_config",
        classmethod(lambda cls, config, **kw: _StubNotifier()),
    )


def test_run_composes_paging_when_both_enables_hold(tmp_path: Path, monkeypatch) -> None:
    import obi.paging.alerts as alerts_mod

    _stub_slack(monkeypatch)
    wrapped: list[object] = []
    real_wrap = alerts_mod.wrap_deliver

    def spy_wrap(slack_deliver, settings, **kw):  # noqa: ANN001, ANN003, ANN202
        wrapped.append(settings)
        return real_wrap(slack_deliver, settings, **kw)

    monkeypatch.setattr(alerts_mod, "wrap_deliver", spy_wrap)
    config = _pager_config(tmp_path, paging=True, alerts_enabled=True)
    result = runner.invoke(app, ["dash", "run", "--scope", str(config)])
    assert result.exit_code == 0, result.output
    assert len(wrapped) == 1  # the composite transport was built
    assert wrapped[0].alerts.people == ["101"]


def test_run_stays_slack_only_without_the_paging_enables(tmp_path: Path, monkeypatch) -> None:
    import obi.paging.alerts as alerts_mod

    _stub_slack(monkeypatch)
    monkeypatch.setattr(
        alerts_mod,
        "wrap_deliver",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not compose")),
    )
    for paging, alerts_enabled in ((False, False), (True, False)):
        config = _pager_config(tmp_path, paging=paging, alerts_enabled=alerts_enabled)
        result = runner.invoke(app, ["dash", "run", "--scope", str(config)])
        assert result.exit_code == 0, result.output


def test_show_renders_maintenance_banner_and_group(tmp_path: Path) -> None:
    # 135: maintenance items are neither attention nor ok; the banner lists the declaration.
    from obi.core.maintenance import MaintenanceSummary

    config = _write_config(tmp_path, dash_section=_dash_section(tmp_path))
    fleet = dash.Tile(
        signal="fleet",
        state="red",
        items=[
            dash.Item(
                key="joshua", label="rowan-ptp-joshua-st-05", state="red", detail="down 33d"
            ),
            dash.Item(
                key="magnolia-gw",
                label="magnolia-gw",
                state="yellow",
                detail="maintenance · down 33d",
                maintenance="config: teardown",
            ),
            dash.Item(key="core-gw-1", label="core-gw-1", state="green", detail="up"),
        ],
        cadence_s=60,
    )
    mesh = dash.Tile(
        signal="mesh",
        state="yellow",
        items=[
            dash.Item(
                key="radio",
                label="mesh radio",
                state="yellow",
                detail="maintenance · absent",
                maintenance="config",
            )
        ],
        cadence_s=300,
        maintenance="config",
    )
    board = dash.Board(
        generated_at_us=NOW_US,
        state="red",
        tiles=[fleet, mesh],
        maintenance=MaintenanceSummary(
            sites=["magnolia"], signals=["mesh"], reason="teardown", unmatched=["site shorline"]
        ),
    )
    dash.write_board(tmp_path / "dash", board)
    result = runner.invoke(app, ["dash", "show", "--scope", str(config)])
    assert result.exit_code == 0
    out = result.output
    assert "MAINT: sites magnolia; signals mesh — teardown" in out
    assert "matches nothing: site shorline" in out
    assert "maintenance (1): magnolia-gw" in out
    assert "1/2 ok" in out  # core-gw-1 ok of the 2 non-maintenance items
    assert "rowan-ptp-joshua-st-05" in out
    assert "maintenance (1): mesh radio" in out
