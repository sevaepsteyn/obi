"""Unit (feature 103): the WLED driver — fixtures for reads, MockTransport for writes.

The parse assertions run against payloads captured READ-ONLY from the real portal controller
(tests/fixtures/wled/, WLED 0.15.3); apply() is exercised only against httpx.MockTransport —
no test ever POSTs to the real device (Principle II / plan research D8). Every failure mode
must come back as a typed outcome: the driver never raises.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from obi.core.config import WledSettings
from obi.core.drivers.wled import WledDriver, WledEffects, WledInfo, WledPalettes, WledState

FIXTURES = Path(__file__).parent.parent / "fixtures" / "wled"
BASE = "https://portal.test"


def _fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def _driver(handler: httpx.MockTransport) -> WledDriver:
    return WledDriver(WledSettings(devices={"portal": BASE}, timeout=1.0), transport=handler)


def _fixture_transport() -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/json/info":
            return httpx.Response(200, json=_fixture("info.json"))
        if path == "/json/state" and request.method == "GET":
            return httpx.Response(200, json=_fixture("state.json"))
        if path == "/json/eff":
            return httpx.Response(200, json=_fixture("effects.json"))
        if path == "/json/pal":
            return httpx.Response(200, json=_fixture("palettes.json"))
        raise AssertionError(f"unexpected request: {request.method} {path}")

    return httpx.MockTransport(handle)


def test_info_parses_fixture() -> None:
    result = _driver(_fixture_transport()).info(BASE)
    assert result.ok, result.error
    info = result.value
    assert isinstance(info, WledInfo)
    assert info.name == "Dig-Quad-AR"
    assert info.version == "0.15.3"
    assert info.led_count == 720
    assert info.power_estimate_mw is not None and info.power_estimate_mw > 0
    assert info.wifi_rssi is not None and info.wifi_rssi < 0
    assert info.live is False
    assert info.uptime_s is not None


def test_state_parses_fixture_and_keeps_raw_verbatim() -> None:
    result = _driver(_fixture_transport()).state(BASE)
    assert result.ok, result.error
    state = result.value
    assert isinstance(state, WledState)
    # raw is the VERBATIM body — the snapshot unit a revert POSTs back.
    fixture = _fixture("state.json")
    assert state.raw == fixture
    assert state.on is True
    assert state.brightness == fixture["bri"]
    # Parsed conveniences mirror segment 0 of the capture (the portal is a live, shared
    # device — assert against the fixture's own values, not a remembered look).
    assert state.primary_color == tuple(fixture["seg"][0]["col"][0])
    assert state.effect_id == fixture["seg"][0]["fx"]
    # 107: palette + effect settings parsed from the same segment.
    assert state.palette_id == fixture["seg"][0]["pal"]
    assert state.speed == fixture["seg"][0]["sx"]
    assert state.intensity == fixture["seg"][0]["ix"]


def test_state_parses_six_segment_layout_verbatim() -> None:
    # 128: the re-segmented portal (status/main pairs per leg) parses cleanly and raw stays
    # verbatim — the layout-scoped engine paths depend on every segment surviving in raw.
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_fixture("state_6seg.json"))

    result = _driver(httpx.MockTransport(handle)).state(BASE)
    assert result.ok, result.error
    state = result.value
    assert isinstance(state, WledState)
    fixture = _fixture("state_6seg.json")
    assert state.raw == fixture
    assert [s["id"] for s in state.raw["seg"]] == [0, 1, 2, 3, 4, 5]
    # Parsed conveniences still mirror segment 0 (a status band in this layout).
    assert state.primary_color == tuple(fixture["seg"][0]["col"][0])


def test_effects_parses_fixture_catalog() -> None:
    result = _driver(_fixture_transport()).effects(BASE)
    assert result.ok, result.error
    effects = result.value
    assert isinstance(effects, WledEffects)
    assert len(effects.names) == 187
    assert effects.names[0] == "Solid"
    assert effects.names.index("Fire 2012") > 0  # a name wled_set's matcher will resolve


def test_palettes_parses_fixture_catalog() -> None:
    result = _driver(_fixture_transport()).palettes(BASE)
    assert result.ok, result.error
    palettes = result.value
    assert isinstance(palettes, WledPalettes)
    assert len(palettes.names) == 71
    assert palettes.names[0] == "Default"
    assert "Icefire" in palettes.names  # the radio-call-24 request target


# --- transient-read retry (feature 107, research D3) --------------------------------------------


def test_get_retries_once_on_transient_failure_then_succeeds() -> None:
    calls = {"n": 0}

    def handle(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(502, text="bad gateway")  # the radio-call-24 failure mode
        return httpx.Response(200, json=_fixture("effects.json"))

    result = _driver(httpx.MockTransport(handle)).effects(BASE)
    assert result.ok, result.error
    assert calls["n"] == 2


def test_get_fails_honestly_when_the_transient_failure_persists() -> None:
    calls = {"n": 0}

    def handle(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(502, text="bad gateway")

    result = _driver(httpx.MockTransport(handle)).palettes(BASE)
    assert not result.ok
    assert "HTTP 502" in (result.error or "")
    assert calls["n"] == 2  # exactly one retry, never more


def test_post_is_never_retried() -> None:
    calls = {"n": 0}

    def handle(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ReadTimeout("slow")

    result = _driver(httpx.MockTransport(handle)).apply(BASE, {"on": False})
    assert not result.ok
    assert calls["n"] == 1


def test_4xx_is_a_real_answer_not_retried() -> None:
    calls = {"n": 0}

    def handle(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404, text="nope")

    result = _driver(httpx.MockTransport(handle)).effects(BASE)
    assert not result.ok and "HTTP 404" in (result.error or "")
    assert calls["n"] == 1


def test_apply_posts_partial_state_with_verbose_flag() -> None:
    seen: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/json/state"
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=_fixture("state.json"))

    result = _driver(httpx.MockTransport(handle)).apply(BASE, {"on": True, "bri": 128})
    assert result.ok, result.error
    assert isinstance(result.value, WledState)
    # Exact body: the partial state plus "v": true (echo the resulting full state), nothing else.
    assert seen == [{"on": True, "bri": 128, "v": True}]


def test_apply_live_flag_survives_in_info() -> None:
    live_info = {**_fixture("info.json"), "live": True, "lm": "E1.31"}

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=live_info)

    result = _driver(httpx.MockTransport(handle)).info(BASE)
    assert result.ok
    assert isinstance(result.value, WledInfo)
    assert result.value.live is True
    assert result.value.live_source == "E1.31"


@pytest.mark.parametrize(
    ("exc", "needle"),
    [
        (httpx.ConnectError("boom"), "could not connect"),
        (httpx.ReadTimeout("slow"), "did not respond within"),
    ],
)
def test_transport_failures_become_typed_outcomes(exc: Exception, needle: str) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise exc

    for call in ("info", "state", "effects"):
        result = getattr(_driver(httpx.MockTransport(handle)), call)(BASE)
        assert not result.ok
        assert result.error is not None and needle in result.error
    result = _driver(httpx.MockTransport(handle)).apply(BASE, {"on": False})
    assert not result.ok and needle in (result.error or "")


def test_http_error_and_bad_json_become_typed_outcomes() -> None:
    def handle_500(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="oops")

    result = _driver(httpx.MockTransport(handle_500)).state(BASE)
    assert not result.ok and "HTTP 500" in (result.error or "")

    def handle_garbage(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    result = _driver(httpx.MockTransport(handle_garbage)).state(BASE)
    assert not result.ok and "non-JSON" in (result.error or "")


def test_unexpected_shapes_become_typed_outcomes() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[1, 2, 3])

    assert not _driver(httpx.MockTransport(handle)).info(BASE).ok
    assert not _driver(httpx.MockTransport(handle)).state(BASE).ok

    def handle_dict(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"not": "a list"})

    assert not _driver(httpx.MockTransport(handle_dict)).effects(BASE).ok
