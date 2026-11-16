"""T012: UnifiInternalClient (feature 016) against httpx.MockTransport — offline, read-only."""

from __future__ import annotations

import json

import httpx
import pytest

from obi.core.drivers.unifi_common import UnifiError
from obi.core.drivers.unifi_internal import UnifiInternalClient
from obi.core.models import SourceOutcome
from tests.unifi_fakes import sample_events, unifi_internal_settings


class _Handler:
    """Records every request and serves canned login / sites / system-log responses."""

    def __init__(self, *, pages: list[list[dict]] | None = None, sys_status: int = 200) -> None:
        self.requests: list[tuple[str, str]] = []
        self.bodies: list[dict] = []
        self._pages = pages if pages is not None else [sample_events()]
        self._sys_status = sys_status

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append((request.method, path))
        if path == "/api/auth/login":
            return httpx.Response(
                200,
                headers={"x-csrf-token": "csrf123", "set-cookie": "TOKEN=eyJ.fake.jwt"},
                json={"unique_id": "u1"},
            )
        if path == "/proxy/network/api/self/sites":
            return httpx.Response(200, json={"data": [{"name": "qgjyvj7p", "desc": "Alder"}]})
        if path.endswith("/system-log/all"):
            body = json.loads(request.content)
            self.bodies.append(body)
            if self._sys_status != 200:
                return httpx.Response(self._sys_status, json={"meta": {"rc": "error"}})
            page_no = int(body.get("pageNumber", 0))
            rows = self._pages[page_no] if page_no < len(self._pages) else []
            return httpx.Response(200, json={"data": rows})
        return httpx.Response(404, json={"meta": {"rc": "error", "msg": "api.err.NotFound"}})


def _client(handler: _Handler, **over: object) -> UnifiInternalClient:
    settings = unifi_internal_settings(**over)

    def factory() -> httpx.Client:
        return httpx.Client(
            base_url=settings.base_url,
            transport=httpx.MockTransport(handler),
            headers={"Accept": "application/json"},
        )

    return UnifiInternalClient(settings, client_factory=factory)


def test_login_extracts_csrf_and_lists_sites() -> None:
    handler = _Handler()
    with _client(handler) as client:
        sites = client.list_sites()
    assert sites == {"Alder": "qgjyvj7p"}  # display -> internal
    assert client._csrf == "csrf123"  # noqa: SLF001 - asserting auth state


def test_fetch_omits_client_devices_and_pages_until_short() -> None:
    # page 0: full page of 2; page 1: short page of 1 -> stop after page 1.
    pages = [sample_events(), sample_events()[:1]]
    handler = _Handler(pages=pages)
    with _client(handler) as client:
        events = list(client.iter_events("qgjyvj7p", 0, 9_999_999_999_999, page_size=2))
    assert len(events) == 3  # 2 + 1
    # CLIENT_DEVICES is never requested (FR-005).
    for body in handler.bodies:
        assert "CLIENT_DEVICES" not in body["categories"]
        assert set(body["severities"]) == {"LOW", "MEDIUM", "HIGH", "VERY_HIGH"}


def test_only_read_endpoints_are_called(tmp_path: object) -> None:
    # FR-010: a full ingest issues only login + self/sites + system-log/all — never a write path.
    handler = _Handler()
    with _client(handler) as client:
        client.selected_sites()
        list(client.iter_events("qgjyvj7p", 0, 9_999_999_999_999))
    allowed = {"/api/auth/login", "/proxy/network/api/self/sites"}
    for method, path in handler.requests:
        is_read = path in allowed or path.endswith("/system-log/all")
        assert is_read, f"unexpected call {method} {path}"
        # No mutation verbs/paths.
        assert "/cmd/" not in path and "/set/" not in path and "/rest/" not in path


def test_auth_failure_classifies_to_unifi_error() -> None:
    handler = _Handler(sys_status=401)
    with _client(handler) as client:  # noqa: SIM117
        with pytest.raises(UnifiError) as exc:
            list(client.iter_events("qgjyvj7p", 0, 9_999_999_999_999))
    assert exc.value.outcome is SourceOutcome.AUTH_FAILED
