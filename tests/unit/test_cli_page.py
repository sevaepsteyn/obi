"""Unit (feature 102): the `obi page` CLI — send/recipients/whoami/refresh.

Transport is intercepted at the client's opener seam; nothing reaches the network. Audit
recording is asserted through a monkeypatched `record_page` (the CLI's late import binds at
call time, so patching the source module is enough).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import obi.paging.client as paging_client
from obi.cli.main import app
from obi.paging.roster import RosterEntry, load_roster, save_roster

runner = CliRunner()

FIXTURES = Path(__file__).parent.parent / "fixtures" / "paging"

ROSTER = [
    RosterEntry(type="group", id="201", name="Weather Watch"),
    RosterEntry(type="person", id="101", name="Sandpiper", fullname="Sandpiper (Alex Doe)"),
    RosterEntry(type="person", id="102", name="Dusty", fullname="Dusty (Sam Roe)"),
    RosterEntry(type="person", id="104", name="Dust Devil", fullname="Dust Devil (Kit Fog)"),
]


def _write_config(tmp_path: Path, *, paging: bool = True) -> Path:
    config = tmp_path / "config.toml"
    body = '[inventory]\npath = "inv.json"\n[logs]\ndb_path = "l.db"\n'
    if paging:
        body += f"""
[paging]
url = "https://pager.example.net/api.php"
username = "mira"
password = "pw"
roster_path = "{tmp_path / "roster.csv"}"
"""
    config.write_text(body)
    config.chmod(0o600)
    return config


class FakeOpener:
    def __init__(self, *replies: tuple[int, str]) -> None:
        self.replies = list(replies)
        self.payloads: list[dict] = []

    def __call__(self, req, ctx, timeout):  # noqa: ANN001, ANN202
        self.payloads.append(json.loads(req.data))
        return self.replies.pop(0)


@pytest.fixture
def audit_pages(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    calls: list[dict] = []

    def fake_record_page(**kw) -> None:  # noqa: ANN003
        calls.append(kw)

    monkeypatch.setattr(paging_client, "record_page", fake_record_page)
    return calls


def _wire(monkeypatch: pytest.MonkeyPatch, *replies: tuple[int, str]) -> FakeOpener:
    opener = FakeOpener(*replies)
    monkeypatch.setattr(paging_client, "_default_opener", opener)
    return opener


# --- send --------------------------------------------------------------------------------------


def test_send_resolves_prints_and_sends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, audit_pages: list[dict]
) -> None:
    config = _write_config(tmp_path)
    save_roster(tmp_path / "roster.csv", ROSTER)
    opener = _wire(monkeypatch, (200, (FIXTURES / "send_ok.json").read_text()))
    result = runner.invoke(
        app,
        ["page", "send", "--to", "sandpiper,201", "-m", "obi test", "--scope", str(config)],
    )
    assert result.exit_code == 0, result.output
    assert "Sandpiper" in result.output  # resolved recipients printed before the send
    assert "page sent" in result.output
    assert opener.payloads[0]["sendpage"]["recipients"] == {"people": ["101", "201"]}
    assert audit_pages == [
        {
            "path": "cli",
            "people": ["101", "201"],
            "groups": [],
            "body": "obi test",
            "outcome": "ok",
            "error": None,
        }
    ]


def test_send_groups_option_forces_group_bucket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, audit_pages: list[dict]
) -> None:
    config = _write_config(tmp_path)
    save_roster(tmp_path / "roster.csv", ROSTER)
    opener = _wire(monkeypatch, (200, (FIXTURES / "send_ok.json").read_text()))
    result = runner.invoke(
        app,
        [
            "page",
            "send",
            "--people",
            "101",
            "--groups",
            "weather",
            "-m",
            "x",
            "--scope",
            str(config),
        ],
    )
    assert result.exit_code == 0, result.output
    assert opener.payloads[0]["sendpage"]["recipients"] == {
        "people": ["101"],
        "groups": ["201"],
    }


def test_send_dry_run_prints_masked_payload_and_transmits_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, audit_pages: list[dict]
) -> None:
    config = _write_config(tmp_path)
    save_roster(tmp_path / "roster.csv", ROSTER)
    opener = _wire(monkeypatch)
    result = runner.invoke(
        app,
        ["page", "send", "--to", "101", "-m", "hello", "--dry-run", "--scope", str(config)],
    )
    assert result.exit_code == 0, result.output
    assert '"password": "***"' in result.output and "pw" not in result.output
    assert opener.payloads == []  # nothing transmitted
    assert audit_pages[0]["outcome"] == "dry_run"


def test_send_ambiguous_name_refuses_with_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, audit_pages: list[dict]
) -> None:
    config = _write_config(tmp_path)
    save_roster(tmp_path / "roster.csv", ROSTER)
    opener = _wire(monkeypatch)
    result = runner.invoke(app, ["page", "send", "--to", "dust", "-m", "x", "--scope", str(config)])
    assert result.exit_code == 2
    assert "ambiguous" in result.output
    assert "Dusty" in result.output and "Dust Devil" in result.output
    assert opener.payloads == [] and audit_pages == []


def test_send_over_length_message_refuses_stating_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, audit_pages: list[dict]
) -> None:
    config = _write_config(tmp_path)
    opener = _wire(monkeypatch)
    result = runner.invoke(
        app, ["page", "send", "--to", "101", "-m", "x" * 201, "--scope", str(config)]
    )
    assert result.exit_code == 2
    assert "200" in result.output  # the limit is stated
    assert opener.payloads == [] and audit_pages == []


def test_send_no_recipients_and_empty_message_refuse(tmp_path: Path) -> None:
    config = _write_config(tmp_path)
    assert runner.invoke(app, ["page", "send", "-m", "x", "--scope", str(config)]).exit_code == 2
    result = runner.invoke(
        app, ["page", "send", "--to", "101", "-m", "", "--scope", str(config)], input=""
    )
    assert result.exit_code == 2


def test_send_message_from_stdin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, audit_pages: list[dict]
) -> None:
    config = _write_config(tmp_path)
    opener = _wire(monkeypatch, (200, (FIXTURES / "send_ok.json").read_text()))
    result = runner.invoke(
        app,
        ["page", "send", "--to", "101", "-m", "-", "--scope", str(config)],
        input="from stdin\n",
    )
    assert result.exit_code == 0, result.output
    assert opener.payloads[0]["sendpage"]["message"] == "from stdin"


def test_send_app_error_exits_1_and_audits_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, audit_pages: list[dict]
) -> None:
    config = _write_config(tmp_path)
    _wire(monkeypatch, (200, (FIXTURES / "send_error.json").read_text()))
    result = runner.invoke(app, ["page", "send", "--to", "101", "-m", "x", "--scope", str(config)])
    assert result.exit_code == 1
    assert "Invalid username or password" in result.output
    assert audit_pages[0]["outcome"] == "error"
    assert "Invalid username" in audit_pages[0]["error"]


def test_send_without_paging_section_exits_2_with_guidance(tmp_path: Path) -> None:
    config = _write_config(tmp_path, paging=False)
    result = runner.invoke(app, ["page", "send", "--to", "101", "-m", "x", "--scope", str(config)])
    assert result.exit_code == 2
    assert "paging not configured" in result.output


# --- recipients (offline) ----------------------------------------------------------------------


def test_recipients_searches_offline(tmp_path: Path) -> None:
    config = _write_config(tmp_path)
    save_roster(tmp_path / "roster.csv", ROSTER)
    result = runner.invoke(app, ["page", "recipients", "dust", "--scope", str(config)])
    assert result.exit_code == 0
    assert "Dusty" in result.output and "Dust Devil" in result.output
    assert "2 match(es)" in result.output


def test_recipients_filters_and_missing_roster_guidance(tmp_path: Path) -> None:
    config = _write_config(tmp_path)
    result = runner.invoke(app, ["page", "recipients", "--scope", str(config)])
    assert result.exit_code == 1
    assert "obi page refresh" in result.output
    save_roster(tmp_path / "roster.csv", ROSTER)
    result = runner.invoke(app, ["page", "recipients", "--groups-only", "--scope", str(config)])
    assert result.exit_code == 0
    assert "Weather Watch" in result.output and "Sandpiper" not in result.output


def test_recipients_works_without_paging_section(tmp_path: Path, monkeypatch) -> None:
    # No [paging] at all: the default XDG roster path serves the search (no creds needed).
    import os

    from obi.core.config import default_paging_roster_path

    config = _write_config(tmp_path, paging=False)
    assert os.environ["XDG_STATE_HOME"].startswith(str(tmp_path.parent))  # suite isolation
    save_roster(default_paging_roster_path(), ROSTER)
    result = runner.invoke(app, ["page", "recipients", "sandpiper", "--scope", str(config)])
    assert result.exit_code == 0
    assert "Sandpiper" in result.output


# --- whoami ------------------------------------------------------------------------------------


def test_whoami_uses_listgroups_and_never_sends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _write_config(tmp_path)
    opener = _wire(monkeypatch, (200, (FIXTURES / "listgroups.json").read_text()))
    result = runner.invoke(app, ["page", "whoami", "--scope", str(config)])
    assert result.exit_code == 0, result.output
    assert "credentials OK" in result.output
    assert len(opener.payloads) == 1
    assert "listgroups" in opener.payloads[0] and "sendpage" not in opener.payloads[0]


def test_whoami_failure_exits_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _write_config(tmp_path)
    _wire(monkeypatch, (200, (FIXTURES / "send_error.json").read_text()))
    result = runner.invoke(app, ["page", "whoami", "--scope", str(config)])
    assert result.exit_code == 1
    assert "Invalid username or password" in result.output


# --- refresh -----------------------------------------------------------------------------------


def test_refresh_rewrites_snapshot_with_bak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _write_config(tmp_path)
    save_roster(tmp_path / "roster.csv", ROSTER[:1])
    _wire(
        monkeypatch,
        (200, (FIXTURES / "listpeople.json").read_text()),
        (200, (FIXTURES / "listgroups.json").read_text()),
    )
    result = runner.invoke(app, ["page", "refresh", "--scope", str(config)])
    assert result.exit_code == 0, result.output
    assert "4 people, 2 groups" in result.output
    refreshed = load_roster(tmp_path / "roster.csv")
    assert refreshed is not None and len(refreshed) == 6
    assert load_roster(tmp_path / "roster.csv.bak") == ROSTER[:1]


def test_refresh_failure_leaves_snapshot_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _write_config(tmp_path)
    save_roster(tmp_path / "roster.csv", ROSTER)
    _wire(monkeypatch, (500, "oops"), (500, "oops"))
    result = runner.invoke(app, ["page", "refresh", "--scope", str(config)])
    assert result.exit_code == 1
    assert "untouched" in result.output
    assert load_roster(tmp_path / "roster.csv") == ROSTER
