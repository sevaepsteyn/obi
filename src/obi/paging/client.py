"""PlayaPage uAPI transport (feature 102) — the ONE code path that can send a page.

A thin, dependency-free client over the uAPI's single-POST protocol (contracts/uapi.md):
credentials on every request, exactly one action key, and the strict success rule
``HTTP 200 AND no truthy "error" in the JSON body`` — a 200 carrying ``{"error": …}`` is an
application rejection (bad credentials, unknown recipient) and MUST read as failure.

Containment mirrors :class:`obi.slack.notifier.SlackNotifier`: no method raises — every
outcome (including a non-JSON body, which we cannot verify as delivered) comes back in a
:class:`PageResult`, so a paging problem can never break a caller's cycle. The client also
enforces the courtesy send spacing observed in the field-proven reference scripts, and
relaxes TLS hostname verification ONLY for raw-IPv4 endpoints (the on-playa DNS-less path,
where a matching certificate is impossible); hostname endpoints always verify fully.
"""

from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from obi.core.audit import record_event

if TYPE_CHECKING:
    from obi.core.config import Config, PagingSettings

TIMEOUT_S = 30.0
_EXCERPT_CHARS = 200

#: Injectable HTTP seam: (request, ssl_context, timeout) → (status, decoded body).
OpenerFn = Callable[[urllib.request.Request, "ssl.SSLContext | None", float], "tuple[int, str]"]


@dataclass(frozen=True)
class PageResult:
    """Outcome of one uAPI call. ``ok`` follows the strict success rule; never an exception."""

    ok: bool
    status: int | None = None
    error: str | None = None
    body: Any = None  # parsed JSON when the response parsed (list* callers read it)


def _default_opener(
    req: urllib.request.Request, ctx: ssl.SSLContext | None, timeout: float
) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:  # noqa: S310
            return resp.status, resp.read().decode(errors="replace")
    except urllib.error.HTTPError as exc:  # non-2xx still carries a body
        return exc.code, exc.read().decode(errors="replace")


def host_is_ipv4(url: str) -> bool:
    """True when the URL's host is a raw IPv4 literal (the on-playa DNS-less endpoint)."""

    host = url.split("//", 1)[-1].split("/", 1)[0].split(":", 1)[0]
    parts = host.split(".")
    return len(parts) == 4 and all(p.isdigit() for p in parts)


def record_page(
    *,
    path: str,
    people: Sequence[str],
    groups: Sequence[str],
    body: str,
    outcome: str,
    error: str | None = None,
) -> None:
    """Audit one page attempt (042, ambient scope) — sent, failed, suppressed, or dry-run.

    The summary names the outcome and the payload names the ORIGINATING PATH (``cli`` /
    ``dash`` / ``slack:<sender id>``, feature 130) — the 095 lesson: audit labels must say
    which path sent. Credentials never enter the payload; ``body`` is operator-, pager-line,
    or requester-verbatim text (130: behind its attribution prefix), never model-composed.
    """

    summary = {
        "ok": "page sent",
        "error": "page failed",
        "dry_run": "page dry-run",
        "suppressed": "page suppressed (hourly cap)",
        # feature 130: a member request the deterministic guards turned away (allowlist,
        # roster refusal, empty/over-length message) — refused before any transport call.
        "refused": "page refused",
    }.get(outcome, f"page {outcome}")
    record_event(
        "report",
        summary=summary,
        payload={
            "channel": "paging",
            "path": path,
            "people": list(people),
            "groups": list(groups),
            "body": body,
        },
        outcome=outcome,
        error=error,
    )


class PagingClient:
    """uAPI calls for one configured paging account. No method raises."""

    def __init__(
        self,
        settings: PagingSettings,
        *,
        opener: OpenerFn | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._settings = settings
        self._opener = opener or _default_opener
        self._monotonic = monotonic
        self._sleep = sleep
        self._last_send: float | None = None

    @classmethod
    def from_config(cls, config: Config) -> PagingClient:
        """Build from a loaded config; the caller has already checked ``config.paging``."""

        if config.paging is None:  # pragma: no cover - callers gate on the section
            raise ValueError("[paging] section is not configured")
        return cls(config.paging)

    # -- transport --------------------------------------------------------------------------

    def _call(self, action: dict[str, Any]) -> PageResult:
        payload = {
            "username": self._settings.username,
            "password": self._settings.password.reveal(),
            **action,
        }
        req = urllib.request.Request(
            self._settings.url,
            data=json.dumps(payload).encode(),
            method="POST",
            headers={"Content-Type": "application/json", "User-Agent": "obi-paging"},
        )
        ctx: ssl.SSLContext | None = None
        if host_is_ipv4(self._settings.url):
            # Raw-IP endpoint (on-playa, no DNS): a hostname-matching cert is impossible, so
            # verification is relaxed HERE ONLY — hostname endpoints always verify fully.
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        try:
            status, text = self._opener(req, ctx, TIMEOUT_S)
        except Exception as exc:  # URLError, timeout, DNS failure — never raise
            return PageResult(ok=False, status=None, error=f"connection failed: {exc}")
        try:
            parsed: Any = json.loads(text)
        except ValueError:
            # Unverifiable ⇒ failure (a page we cannot confirm accepted is not "sent").
            excerpt = text.strip()[:_EXCERPT_CHARS]
            return PageResult(
                ok=False, status=status, error=f"non-JSON response (HTTP {status}): {excerpt!r}"
            )
        app_error = parsed.get("error") if isinstance(parsed, dict) else None
        if status == 200 and not app_error:
            return PageResult(ok=True, status=status, body=parsed)
        error = str(app_error) if app_error else f"HTTP {status}"
        return PageResult(ok=False, status=status, error=error, body=parsed)

    # -- actions ----------------------------------------------------------------------------

    def send_page(
        self, *, people: Sequence[str], groups: Sequence[str] = (), message: str
    ) -> PageResult:
        """One ``sendpage`` (recipient arrays ride a single call). Courtesy-spaced ≥2 s."""

        if not people and not groups:
            return PageResult(ok=False, error="no recipients")
        if not message:
            return PageResult(ok=False, error="empty message")
        recipients: dict[str, list[str]] = {}
        if people:
            recipients["people"] = [str(p) for p in people]
        if groups:
            recipients["groups"] = [str(g) for g in groups]
        if self._last_send is not None and self._settings.send_spacing_s > 0:
            wait = self._settings.send_spacing_s - (self._monotonic() - self._last_send)
            if wait > 0:
                self._sleep(wait)
        result = self._call({"sendpage": {"recipients": recipients, "message": message}})
        self._last_send = self._monotonic()
        return result

    def list_people(self) -> PageResult:
        return self._call({"listpeople": ""})

    def list_groups(self) -> PageResult:
        return self._call({"listgroups": ""})

    def check_credentials(self) -> PageResult:
        """Verify the account via ``listgroups`` — a credential check can NEVER send a page."""

        return self._call({"listgroups": ""})
