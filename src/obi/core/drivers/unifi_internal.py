"""UniFi **internal/controller** event adapter — strictly read-only (Constitution I, IV).

Pulls controller events from the reverse-engineered v2 ``system-log/all`` endpoint (feature 016),
authenticated by a console-**local** admin account (TOKEN cookie + CSRF). This is an UNDOCUMENTED
API and may change on controller upgrades; obi isolates it here and issues ONLY the three reads
below — login, ``self/sites``, and ``system-log/all`` — never a state-changing call, even though the
credential is capable of one (FR-010). The classic ``stat/event`` route was removed in Network
10.x; this uses the v2 endpoint the controller UI's System Log page calls. ``httpx`` is quarantined
to this module and ``unifi_common`` for the UniFi vendor.

See specs/016-unifi-logs/contracts/unifi-internal-api.md.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from typing import Any

import httpx

from obi.core.audit import record_device_command
from obi.core.config import UnifiInternalSettings
from obi.core.drivers.unifi_common import (
    _csrf_from_token,
    _http_error,
    _unwrap,
    site_selected,
)

# Every severity is requested; the operator narrows by category instead (CLIENT_DEVICES excluded).
_SEVERITIES = ["LOW", "MEDIUM", "HIGH", "VERY_HIGH"]
_PAGE_SIZE = 100
_MAX_PAGES = 1000  # defensive bound on a single-window page walk

ClientFactory = Callable[[], httpx.Client]


class UnifiInternalClient:
    """Read-only client for the reverse-engineered UniFi controller event API (feature 016).

    Use as a context manager so the underlying ``httpx.Client`` (and its session cookie) is closed.
    ``client_factory`` is injectable so tests drive it with an ``httpx.MockTransport`` — no network.
    """

    def __init__(
        self,
        settings: UnifiInternalSettings,
        *,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self._s = settings
        self._client_factory = client_factory or self._default_client_factory
        self._client: httpx.Client | None = None
        self._csrf: str | None = None

    def _default_client_factory(self) -> httpx.Client:
        return httpx.Client(
            base_url=self._s.base_url,
            verify=self._s.verify_tls,
            timeout=self._s.timeout,
            headers={"Accept": "application/json"},
        )

    def __enter__(self) -> UnifiInternalClient:
        self._client = self._client_factory()
        return self

    def __exit__(self, *exc: object) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    @property
    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = self._client_factory()
        return self._client

    # --- auth -----------------------------------------------------------------------------------

    def login(self) -> None:
        """POST ``/api/auth/login`` → TOKEN cookie (stored by httpx) + CSRF. Raises on failure."""

        try:
            resp = self._http.post(
                "/api/auth/login",
                json={
                    "username": self._s.username.reveal(),
                    "password": self._s.password.reveal(),
                },
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise _http_error(exc, self._s.timeout, "controller login") from exc
        csrf = resp.headers.get("x-csrf-token")
        if not csrf:
            token = self._http.cookies.get("TOKEN")
            csrf = _csrf_from_token(token) if token else None
        self._csrf = csrf

    def _auth_headers(self) -> dict[str, str]:
        return {"X-CSRF-Token": self._csrf} if self._csrf else {}

    def _request(
        self, method: str, path: str, *, json: dict[str, Any] | None = None
    ) -> httpx.Response:
        """Issue a read with lazy login and a single re-login-on-401 retry (FR-009)."""

        # feature 042: one audited device_command per controller request (ambient scope).
        from urllib.parse import urlparse

        target = urlparse(self._s.base_url).netloc or self._s.base_url
        start = time.monotonic()
        try:
            if self._csrf is None:
                self.login()
            resp = self._http.request(method, path, json=json, headers=self._auth_headers())
            if resp.status_code == 401:
                self.login()  # session expired — re-authenticate once and retry
                resp = self._http.request(method, path, json=json, headers=self._auth_headers())
            resp.raise_for_status()
        except Exception as exc:
            record_device_command(
                target,
                "http",
                f"{method} {path}",
                duration_ms=(time.monotonic() - start) * 1000.0,
                outcome="error",
                error=str(exc),
            )
            raise
        record_device_command(
            target,
            "http",
            f"{method} {path}",
            response_bytes=len(resp.content),
            duration_ms=(time.monotonic() - start) * 1000.0,
        )
        return resp

    # --- the three reads ------------------------------------------------------------------------

    def list_sites(self) -> dict[str, str]:
        """Return ``{display_name: internal_id}`` for the console's sites (read-only)."""

        try:
            resp = self._request("GET", "/proxy/network/api/self/sites")
        except httpx.HTTPError as exc:
            raise _http_error(exc, self._s.timeout, "controller sites") from exc
        sites: dict[str, str] = {}
        for site in _unwrap(resp.json(), "data"):
            internal = site.get("name")
            if internal:
                display = site.get("desc") or internal
                sites[str(display)] = str(internal)
        return sites

    def selected_sites(self) -> dict[str, str]:
        """``list_sites`` narrowed to the operator's configured ``sites`` (all when empty)."""

        return {
            display: internal
            for display, internal in self.list_sites().items()
            if site_selected([display, internal], self._s.sites)
        }

    def fetch_events(
        self,
        internal_site: str,
        frm_ms: int,
        to_ms: int,
        *,
        page: int = 0,
        page_size: int = _PAGE_SIZE,
    ) -> list[dict[str, Any]]:
        """One page of ``system-log/all`` events for a site (``CLIENT_DEVICES`` omitted, FR-005)."""

        payload: dict[str, Any] = {
            "severities": list(_SEVERITIES),
            "categories": list(self._s.categories),
            "timestampFrom": frm_ms,
            "timestampTo": to_ms,
            "pageNumber": page,
            "pageSize": page_size,
        }
        path = f"/proxy/network/v2/api/site/{internal_site}/system-log/all"
        try:
            resp = self._request("POST", path, json=payload)
        except httpx.HTTPError as exc:
            raise _http_error(exc, self._s.timeout, "controller system-log") from exc
        return _unwrap(resp.json(), "data")

    def iter_events(
        self,
        internal_site: str,
        frm_ms: int,
        to_ms: int,
        *,
        page_size: int = _PAGE_SIZE,
    ) -> Iterator[dict[str, Any]]:
        """Yield every event in the window, paging until a short/empty page (bounded)."""

        for page in range(_MAX_PAGES):
            rows = self.fetch_events(internal_site, frm_ms, to_ms, page=page, page_size=page_size)
            yield from rows
            if len(rows) < page_size:
                return
