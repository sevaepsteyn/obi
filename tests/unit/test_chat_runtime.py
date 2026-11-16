"""Unit (US3): build_runtime supports any source combination (features 006/010).

Mikrotik-only, UISP-only, NetBox-only, or any mix builds a runtime; no source at all (or no API
key) raises ChatUnavailable. AnthropicLLMClient imports the SDK lazily, so this is fully offline.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from obi.chat.session import build_runtime, build_tool_context
from obi.core.config import ChatSettings, Config, Secret
from obi.core.llm import ChatUnavailable
from obi.core.models import DiscoveryScope
from obi.core.prompt import system_prompt
from tests.fakes import netbox_settings, uisp_settings
from tests.fakes import settings as mikrotik_settings


def _config(
    tmp_path: Path,
    *,
    routeros: bool,
    uisp: bool,
    netbox: bool = False,
    api_key: str | None = "sk-test",
) -> Config:
    return Config(
        scope=DiscoveryScope(seeds=["172.25.110.1"]) if routeros else None,
        routeros=mikrotik_settings() if routeros else None,
        uisp=uisp_settings() if uisp else None,
        netbox=netbox_settings() if netbox else None,
        chat=ChatSettings(api_key=Secret(api_key) if api_key is not None else None),
        audit_path=tmp_path / "audit.jsonl",
        inventory_path=tmp_path / "inventory.json",
    )


def test_uisp_only_builds_runtime(tmp_path: Path) -> None:
    runtime = build_runtime(_config(tmp_path, routeros=False, uisp=True))
    assert runtime.ctx.driver is None
    assert runtime.ctx.uisp_source is not None


def test_mikrotik_only_builds_runtime(tmp_path: Path) -> None:
    runtime = build_runtime(_config(tmp_path, routeros=True, uisp=False))
    assert runtime.ctx.driver is not None
    assert runtime.ctx.uisp_source is None


def test_both_vendors_build_runtime(tmp_path: Path) -> None:
    runtime = build_runtime(_config(tmp_path, routeros=True, uisp=True))
    assert runtime.ctx.driver is not None
    assert runtime.ctx.uisp_source is not None


def test_netbox_only_builds_runtime(tmp_path: Path) -> None:
    runtime = build_runtime(_config(tmp_path, routeros=False, uisp=False, netbox=True))
    assert runtime.ctx.driver is None
    assert runtime.ctx.uisp_source is None
    assert runtime.ctx.netbox_source is not None


def test_netbox_with_mikrotik_builds_runtime(tmp_path: Path) -> None:
    runtime = build_runtime(_config(tmp_path, routeros=True, uisp=False, netbox=True))
    assert runtime.ctx.driver is not None
    assert runtime.ctx.netbox_source is not None


def test_build_tool_context_needs_no_api_key(tmp_path: Path) -> None:
    """Feature 048 seam: the web tool bridge builds the SAME tool wiring with NO API key.

    ``build_runtime`` refuses (ChatUnavailable) without a key; ``build_tool_context`` — extracted
    from it — builds the identical driver/sources/audit ToolContext, minus the reasoning model.
    """

    config = _config(tmp_path, routeros=True, uisp=True, api_key=None)
    with pytest.raises(ChatUnavailable):
        build_runtime(config)
    ctx = build_tool_context(config)
    assert ctx.driver is not None
    assert ctx.uisp_source is not None
    assert ctx.config is config
    assert ctx.audit is not None
    # A directly-constructed ToolContext has no per-turn state bled in.
    assert ctx.question == ""
    assert ctx.live_budget is None


def test_netbox_with_uisp_builds_runtime(tmp_path: Path) -> None:
    runtime = build_runtime(_config(tmp_path, routeros=False, uisp=True, netbox=True))
    assert runtime.ctx.uisp_source is not None
    assert runtime.ctx.netbox_source is not None


def test_all_three_sources_build_runtime(tmp_path: Path) -> None:
    runtime = build_runtime(_config(tmp_path, routeros=True, uisp=True, netbox=True))
    assert runtime.ctx.driver is not None
    assert runtime.ctx.uisp_source is not None
    assert runtime.ctx.netbox_source is not None


def test_no_source_raises(tmp_path: Path) -> None:
    with pytest.raises(ChatUnavailable):
        build_runtime(_config(tmp_path, routeros=False, uisp=False, netbox=False))


def test_missing_api_key_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ChatUnavailable):
        build_runtime(_config(tmp_path, routeros=False, uisp=True, api_key=None))


# --- feature 008: operator network context -----------------------------------------------------


def test_context_composed_into_runtime_system(tmp_path: Path) -> None:
    """A configured context_file is loaded once and appears in runtime.system (US1)."""

    ctx_file = tmp_path / "net.md"
    ctx_file.write_text("SALOON is site 2; gateways are <site>-gw")
    config = _config(tmp_path, routeros=True, uisp=False)
    config.chat.context_file = ctx_file

    runtime = build_runtime(config)

    assert "SALOON is site 2" in runtime.system
    assert runtime.context_loaded is True
    assert runtime.context_warning is None


def test_no_context_runtime_matches_baseline(tmp_path: Path) -> None:
    """No context_file ⇒ runtime.system is exactly today's prompt; nothing flagged (US3/FR-011)."""

    config = _config(tmp_path, routeros=True, uisp=False)
    runtime = build_runtime(config)

    # 137: the prompt renders this config's sections (no [wled] ⇒ no lighting rule).
    assert runtime.system == system_prompt(config=config)
    assert runtime.context_warning is None
    assert runtime.context_loaded is False


def test_bad_context_warns_but_still_builds(tmp_path: Path) -> None:
    """A missing context_file ⇒ runtime still builds, warns, and injects no context (US3)."""

    config = _config(tmp_path, routeros=True, uisp=False)
    config.chat.context_file = tmp_path / "missing.md"

    runtime = build_runtime(config)

    assert runtime.system == system_prompt(config=config)  # no context injected
    assert runtime.context_loaded is False
    assert runtime.context_warning is not None and "not found" in runtime.context_warning


def test_no_obi_secret_leaks_into_system(tmp_path: Path) -> None:
    """SC-005: no obi-managed secret is ever placed into the composed system prompt.

    Build a runtime whose config carries a Mikrotik password, a UISP token, and a chat API key,
    plus a context file, then assert none of those secret values appear in runtime.system.
    """

    ctx_file = tmp_path / "net.md"
    ctx_file.write_text("network notes")
    config = _config(tmp_path, routeros=True, uisp=True, api_key="sk-test-secret")
    config.chat.context_file = ctx_file

    runtime = build_runtime(config)

    assert "fakesecret44028f" not in runtime.system  # Mikrotik password (tests.fakes.settings)
    assert "UISP-TOKEN" not in runtime.system  # UISP token (tests.fakes.uisp_settings)
    assert "sk-test-secret" not in runtime.system  # chat API key
