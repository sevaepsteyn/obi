"""Integration: `obi uisp links` (feature 034 — T008, contracts/surfaces.md).

Fixture-backed, offline: the table shows every wireless link with joined RF detail; down and
quality-degraded rows are distinguishable; `--json` emits exactly the serializer output; a
controller failure degrades exactly like `obi uisp status` (classified note, exit 0, no partial
table); the token never appears in any output.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from obi.cli.main import app
from obi.core.drivers.uisp import UispError
from obi.core.models import SourceOutcome
from tests.fakes import load_uisp_links_fixture
from tests.integration._uisp import patch_geo_transport, write_uisp_config

runner = CliRunner()


def test_links_table_lists_wireless_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_geo_transport(monkeypatch)
    config = write_uisp_config(tmp_path)

    result = runner.invoke(app, ["uisp", "links", "--scope", str(config)])

    assert result.exit_code == 0, result.output
    out = result.output
    assert "wireless link(s)" in out
    assert "active=" in out and "down=" in out
    # the capture's chestnut backhaul appears with its RF detail
    assert "BM-Chestnut-airMAX" in out
    assert "5550" in out  # frequency MHz
    assert "disconnected" in out  # off-season down links render distinguishably
    assert "UISP-TOKEN" not in out


def test_links_json_matches_serializer_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_geo_transport(monkeypatch)
    config = write_uisp_config(tmp_path)

    result = runner.invoke(app, ["uisp", "links", "--scope", str(config), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["outcome"] == "reachable"
    assert payload["counts"]["total"] == 40  # all wireless links, no ethernet
    assert payload["counts"]["active"] + payload["counts"]["down"] == 40
    link = next(x for x in payload["links"] if x["state"] == "active")
    assert link["ends"][0]["radio"] is not None  # the join carried radio quality
    assert "UISP-TOKEN" not in result.output


def test_links_filter_narrows_by_either_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_geo_transport(monkeypatch)
    config = write_uisp_config(tmp_path)

    result = runner.invoke(
        app, ["uisp", "links", "--scope", str(config), "--json", "--filter", "chestnut"]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["name_filter"] == "chestnut"
    assert 0 < payload["counts"]["total"] < 40
    assert all("chestnut" in x["label"].lower() for x in payload["links"])


def test_links_ignore_names_excludes_matching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """feature 035: [uisp] ignore_names drops matching links from the table + --json."""
    patch_geo_transport(monkeypatch)
    config = write_uisp_config(tmp_path, extra='ignore_names = ["chestnut"]')

    result = runner.invoke(app, ["uisp", "links", "--scope", str(config), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert 0 < payload["counts"]["total"] < 40, "chestnut links excluded, others remain"
    # the label carries both endpoint names, so a clean label proves neither endpoint matched
    assert all("chestnut" not in x["label"].lower() for x in payload["links"])


def test_links_ethernet_records_are_excluded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = load_uisp_links_fixture("data-links")
    assert any(str(r.get("type", "")).lower() == "ethernet" for r in raw)
    patch_geo_transport(monkeypatch)
    config = write_uisp_config(tmp_path)

    result = runner.invoke(app, ["uisp", "links", "--scope", str(config), "--json"])

    payload = json.loads(result.output)
    wireless_count = sum(1 for r in raw if str(r.get("type", "")).lower() == "wireless")
    assert payload["counts"]["total"] == wireless_count < len(raw)


def test_links_controller_failure_degrades_like_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_geo_transport(
        monkeypatch,
        error=UispError(SourceOutcome.TIMED_OUT, "controller did not respond within 5s"),
    )
    config = write_uisp_config(tmp_path)

    table = runner.invoke(app, ["uisp", "links", "--scope", str(config)])
    assert table.exit_code == 0, table.output
    assert "timed-out" in table.output or "timed_out" in table.output

    as_json = runner.invoke(app, ["uisp", "links", "--scope", str(config), "--json"])
    assert as_json.exit_code == 0, as_json.output
    # CliRunner mixes the stderr note into output; the JSON document starts at the first brace.
    payload = json.loads(as_json.output[as_json.output.index("{") :])
    assert payload["outcome"] == SourceOutcome.TIMED_OUT.value
    assert payload["links"] == []  # total report, never a partial view


def test_links_requires_uisp_config(tmp_path: Path) -> None:
    # another source is configured (so load_config passes), but [uisp] is absent
    config = tmp_path / "config.toml"
    config.write_text(
        "[logs]\n"
        f'db_path = "{tmp_path / "logs.db"}"\n'
        "[audit]\n"
        f'path = "{tmp_path / "audit.jsonl"}"\n'
    )
    config.chmod(0o600)

    result = runner.invoke(app, ["uisp", "links", "--scope", str(config)])

    assert result.exit_code == 2
    assert "uisp not configured" in result.output


def test_links_writes_one_audit_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_geo_transport(monkeypatch)
    config = write_uisp_config(tmp_path)

    result = runner.invoke(app, ["uisp", "links", "--scope", str(config)])
    assert result.exit_code == 0, result.output

    records = [
        json.loads(line)
        for line in (tmp_path / "audit.jsonl").read_text().splitlines()
        if line.strip()
    ]
    links_records = [r for r in records if r["action"] == "uisp_links"]
    assert len(links_records) == 1
    assert "UISP-TOKEN" not in json.dumps(records)
