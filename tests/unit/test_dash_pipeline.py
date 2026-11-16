"""Unit (feature 095, US4): the pipeline tile — "is obi itself seeing the network?".

Evaluator rules over synthesized feeds (quiet ⇒ yellow, very quiet ⇒ red, never-run hint,
pre-judged extras) and the collector over a tmp state dir: log mtimes, the 085 sidecar count,
and the pager's own delivery health.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from obi.core import dash
from obi.core.dash import PipelineFeed

NOW_US = 1_787_000_000_000_000


def _feed(
    name: str = "metrics", *, age: float | None = 60.0, rule: float = 960.0, **kw
) -> PipelineFeed:
    return PipelineFeed(name=name, age_s=age, stale_after_s=rule, **kw)


def test_fresh_quiet_and_dead_feeds() -> None:
    tile = dash._eval_pipeline(
        [
            _feed("metrics", age=120.0),
            _feed("logs-sync", age=1200.0),  # past the rule ⇒ yellow
            _feed("dash", age=4000.0, rule=240.0),  # past 3× the rule ⇒ red
            _feed("watch", age=None),  # never run
        ],
        now_us=NOW_US,
    )
    states = {item.key: item.state for item in tile.items}
    assert states == {"metrics": "green", "logs-sync": "yellow", "dash": "red", "watch": "yellow"}
    assert tile.state == "red"
    dead = next(item for item in tile.items if item.key == "dash")
    assert "quiet 67m" in dead.detail and "expected ≤ 4m" in dead.detail
    never = next(item for item in tile.items if item.key == "watch")
    assert "cron" in never.detail


def test_prejudged_extras_bypass_the_age_rule() -> None:
    tile = dash._eval_pipeline(
        [
            _feed("configs-capture", age=None, state="yellow", detail="2/33 capture-stale"),
            _feed("pager", age=None, state="yellow", detail="delivery failing: slack unreachable"),
        ],
        now_us=NOW_US,
    )
    assert all(item.state == "yellow" for item in tile.items)
    assert "2/33 capture-stale" in tile.items[0].detail


def _config(tmp_path: Path):
    from obi.core.config import load_config

    config = tmp_path / "config.toml"
    config.write_text(
        f'[logs]\ndb_path = "{tmp_path}/logs.db"\n'
        f'[dash]\nstate_dir = "{tmp_path}/dash"\n'
        "[watch.config_captures]\n"
        f'repo_path = "{tmp_path}/config-repo"\nstale_after = "1d"\n'
    )
    config.chmod(0o600)
    return load_config(config)


def test_collector_reads_sidecar_and_pager_state(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    state_dir = tmp_path / "state" / "obi"
    state_dir.mkdir(parents=True)
    now_s = NOW_US / 1_000_000
    # One fresh collector log; the rest read "never run".
    (state_dir / "metrics.log").write_text("x")
    os.utime(state_dir / "metrics.log", (now_s - 60, now_s - 60))

    # The 085 sidecar lives BESIDE the repo (capture_state_path = repo parent), as live.
    repo = tmp_path / "config-repo"
    repo.mkdir()
    fresh_iso = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now_s - 3600))
    stale_iso = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now_s - 3 * 86400))
    (tmp_path / "config-captures.json").write_text(
        json.dumps(
            {
                "core-gw-1": {"last_success_at": fresh_iso},
                "wingnut-gw": {"last_success_at": stale_iso},
                "pine-gw": {"last_attempt": "failed"},
            }
        )
    )

    config = _config(tmp_path)
    assert config.dash is not None
    # The pager wrote a delivery error last cycle.
    dash.write_json_atomic(
        config.dash.state_dir / dash.PAGER_STATE_FILE,
        {"items": {}, "delivery_error": "slack unreachable", "delivery_error_us": NOW_US},
    )

    feeds = {f.name: f for f in dash.collect_pipeline_feeds(config, config.dash, now_us=NOW_US)}
    assert feeds["metrics"].age_s is not None and feeds["metrics"].age_s < 120
    assert feeds["dash"].age_s is None  # never run in this isolated state dir
    captures = feeds["configs-capture"]
    assert captures.state == "yellow" and "1/2 capture-stale" in captures.detail
    pager = feeds["pager"]
    assert pager.state == "yellow" and "slack unreachable" in pager.detail


def test_collector_without_sidecar_emits_only_collectors(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    config = _config(tmp_path)
    assert config.dash is not None
    feeds = {f.name: f for f in dash.collect_pipeline_feeds(config, config.dash, now_us=NOW_US)}
    assert "configs-capture" not in feeds  # empty sidecar ⇒ nothing tracked ⇒ no item
    assert "pager" not in feeds
    assert "dash" in feeds and "metrics" in feeds  # the COLLECTORS registry drives the list


def test_sidecar_capture_path_respects_configured_stale_after(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'[logs]\ndb_path = "{tmp_path}/logs.db"\n'
        f'[dash]\nstate_dir = "{tmp_path}/dash"\n'
        "[watch.config_captures]\n"
        f'repo_path = "{tmp_path}/config-repo"\nstale_after = "7d"\n'
    )
    config_path.chmod(0o600)
    from obi.core.config import load_config

    config = load_config(config_path)
    repo = tmp_path / "config-repo"
    repo.mkdir()
    three_days = time.strftime(
        "%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(NOW_US / 1_000_000 - 3 * 86400)
    )
    (tmp_path / "config-captures.json").write_text(
        json.dumps({"core-gw-1": {"last_success_at": three_days}})
    )
    assert config.dash is not None
    feeds = {f.name: f for f in dash.collect_pipeline_feeds(config, config.dash, now_us=NOW_US)}
    assert feeds["configs-capture"].state == "green"  # 3d old is fresh under a 7d window


def test_135_configs_capture_excludes_maintenance_devices(tmp_path: Path, monkeypatch) -> None:
    from obi.core.config import MaintenanceSettings
    from obi.core.maintenance import Maintenance

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / "config-repo").mkdir()
    now_s = NOW_US / 1_000_000
    stale_iso = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now_s - 3 * 86400))
    fresh_iso = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now_s - 3600))
    (tmp_path / "config-captures.json").write_text(
        json.dumps(
            {
                "wingnut-gw": {"last_success_at": stale_iso},
                "noc-gw": {"last_success_at": stale_iso},
                "chestnut-gw": {"last_success_at": stale_iso},
                "core-gw-1": {"last_success_at": fresh_iso},
            }
        )
    )
    config = _config(tmp_path)
    assert config.dash is not None
    matcher = Maintenance.build(MaintenanceSettings(sites=("wingnut", "noc")))
    feeds = {
        f.name: f
        for f in dash.collect_pipeline_feeds(
            config, config.dash, now_us=NOW_US, maintenance=matcher
        )
    }
    captures = feeds["configs-capture"]
    assert captures.state == "yellow"
    assert captures.detail.startswith("1/2 capture-stale (2 in maintenance)")

    all_off = Maintenance.build(MaintenanceSettings(sites=("wingnut", "noc", "chestnut")))
    feeds = {
        f.name: f
        for f in dash.collect_pipeline_feeds(
            config, config.dash, now_us=NOW_US, maintenance=all_off
        )
    }
    assert feeds["configs-capture"].state == "green"  # the band stops holding amber
