"""Unit (feature 127): the playa_locate tool — registration shape and path wiring.

The engine is pinned by test_playa.py; here what matters is the ToolDefinition's
read-only registration, the no-data answer on a bare host, and that the handler
reads the [meshtastic] art_path override and the default playa camp path (every
path resolves under the test's XDG tmp — nothing real is touched, nothing fetched).
"""

from __future__ import annotations

import json
from pathlib import Path

from obi.core.audit import AuditTrail
from obi.core.config import MeshtasticSettings, default_playa_state_path
from obi.core.toolset import ToolContext, _playa_locate, build_tools
from tests.fakes import chat_config


def _ctx(tmp_path: Path, *, mesh: MeshtasticSettings | None = None) -> ToolContext:
    config = chat_config(tmp_path, mesh=mesh)
    return ToolContext(
        config=config,
        driver=None,
        audit=AuditTrail(config.audit_path),
        question="where is snuggles?",
        actor="U123",
    )


def test_registered_read_only_with_the_contract_schema() -> None:
    tool = {t.name: t for t in build_tools()}["playa_locate"]
    assert tool.mutating is False
    assert tool.risk_class == "read_only"
    assert tool.input_schema["required"] == ["name"]
    assert tool.input_schema["additionalProperties"] is False
    assert set(tool.input_schema["properties"]) == {"name"}


def test_missing_name_is_an_error(tmp_path: Path) -> None:
    assert _playa_locate(_ctx(tmp_path), {}) == "error: 'name' is required."


def test_bare_host_answers_the_no_data_state(tmp_path: Path) -> None:
    answer = _playa_locate(_ctx(tmp_path), {"name": "snuggles"})
    assert "No playa data downloaded yet" in answer


def test_camps_answer_from_the_default_playa_dir(tmp_path: Path) -> None:
    camp_path = default_playa_state_path() / "camp.json"
    camp_path.parent.mkdir(parents=True, exist_ok=True)
    camp_path.write_text(json.dumps([{"name": "Snuggles", "location_string": "G & 9:15"}]))
    answer = _playa_locate(_ctx(tmp_path), {"name": "snuggles"})
    assert answer == "Snuggles is at 9:15 & Golf."


def test_art_honors_the_meshtastic_art_path_override(tmp_path: Path) -> None:
    art_path = tmp_path / "custom-art.json"
    art_path.write_text(
        json.dumps(
            [
                {
                    "name": "Painted Cat",
                    "location": {
                        "gps_latitude": 40.7788198,
                        "gps_longitude": -119.2155828,
                    },
                }
            ]
        )
    )
    ctx = _ctx(tmp_path, mesh=MeshtasticSettings(art_path=art_path))
    answer = _playa_locate(ctx, {"name": "painted cat"})
    assert answer.startswith("Painted Cat is at ")
