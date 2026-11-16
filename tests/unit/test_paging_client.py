"""Unit (feature 102): the PlayaPage uAPI client — success rule, TLS relax, spacing.

Everything runs against an injected fake opener; no test ever reaches the network.
"""

from __future__ import annotations

import json
import ssl
import urllib.request
from pathlib import Path

from obi.core.config import PagingSettings, Secret
from obi.paging.client import TIMEOUT_S, PagingClient, host_is_ipv4

FIXTURES = Path(__file__).parent.parent / "fixtures" / "paging"


def _settings(**overrides: object) -> PagingSettings:
    defaults: dict = {
        "username": "mira",
        "password": Secret("pw"),
        "url": "https://pager.example.net/api.php",
        "send_spacing_s": 2.0,
    }
    defaults.update(overrides)
    return PagingSettings(**defaults)


class FakeOpener:
    """Records every request; replies from a scripted (status, body) queue."""

    def __init__(self, *replies: tuple[int, str]) -> None:
        self.replies = list(replies)
        self.requests: list[urllib.request.Request] = []
        self.contexts: list[ssl.SSLContext | None] = []

    def __call__(
        self, req: urllib.request.Request, ctx: ssl.SSLContext | None, timeout: float
    ) -> tuple[int, str]:
        assert timeout == TIMEOUT_S
        self.requests.append(req)
        self.contexts.append(ctx)
        return self.replies.pop(0)

    def payload(self, index: int = -1) -> dict:
        data = self.requests[index].data
        assert isinstance(data, bytes)
        return json.loads(data)


def _client(opener: FakeOpener, settings: PagingSettings | None = None, **kw) -> PagingClient:
    return PagingClient(settings or _settings(), opener=opener, **kw)


# --- success rule ------------------------------------------------------------------------------


def test_send_success_requires_200_and_no_error_field() -> None:
    opener = FakeOpener((200, (FIXTURES / "send_ok.json").read_text()))
    result = _client(opener).send_page(people=["101"], message="hi")
    assert result.ok and result.status == 200 and result.error is None


def test_200_with_app_error_is_failure_carrying_the_text() -> None:
    opener = FakeOpener((200, (FIXTURES / "send_error.json").read_text()))
    result = _client(opener).send_page(people=["101"], message="hi")
    assert not result.ok
    assert result.status == 200
    assert "Invalid username or password" in (result.error or "")


def test_non_200_is_failure() -> None:
    opener = FakeOpener((500, '{"status": "boom"}'))
    result = _client(opener).send_page(people=["101"], message="hi")
    assert not result.ok and result.error == "HTTP 500"


def test_non_json_body_is_unverifiable_failure_with_excerpt() -> None:
    opener = FakeOpener((200, "<html>login</html>"))
    result = _client(opener).send_page(people=["101"], message="hi")
    assert not result.ok
    assert "non-JSON" in (result.error or "") and "login" in (result.error or "")


def test_connection_failure_never_raises() -> None:
    def boom(req, ctx, timeout):  # noqa: ANN001, ANN202
        raise OSError("no route to host")

    result = PagingClient(_settings(), opener=boom).send_page(people=["101"], message="hi")
    assert not result.ok and "no route to host" in (result.error or "")


# --- payload shape -----------------------------------------------------------------------------


def test_send_payload_shape_and_credentials() -> None:
    opener = FakeOpener((200, "{}"))
    _client(opener).send_page(people=["101", "102"], groups=["201"], message="text here")
    payload = opener.payload()
    assert payload["username"] == "mira" and payload["password"] == "pw"
    assert payload["sendpage"] == {
        "recipients": {"people": ["101", "102"], "groups": ["201"]},
        "message": "text here",
    }


def test_empty_recipient_arrays_are_omitted() -> None:
    opener = FakeOpener((200, "{}"))
    _client(opener).send_page(people=["101"], message="x")
    assert "groups" not in opener.payload()["sendpage"]["recipients"]


def test_send_refuses_locally_without_recipients_or_message() -> None:
    opener = FakeOpener()
    client = _client(opener)
    assert not client.send_page(people=[], message="x").ok
    assert not client.send_page(people=["101"], message="").ok
    assert opener.requests == []  # nothing transmitted


def test_list_people_and_groups_parse_fixture_shapes() -> None:
    opener = FakeOpener(
        (200, (FIXTURES / "listpeople.json").read_text()),
        (200, (FIXTURES / "listgroups.json").read_text()),
    )
    client = _client(opener)
    people = client.list_people()
    groups = client.list_groups()
    assert people.ok and people.body["people"][0]["person_id"] == "101"
    assert groups.ok and groups.body["groups"][0]["name"] == "Weather Watch"
    assert opener.payload(0)["listpeople"] == ""
    assert opener.payload(1)["listgroups"] == ""


def test_check_credentials_uses_listgroups_and_never_sends() -> None:
    opener = FakeOpener((200, (FIXTURES / "listgroups.json").read_text()))
    result = _client(opener).check_credentials()
    assert result.ok
    payload = opener.payload()
    assert "listgroups" in payload and "sendpage" not in payload


# --- courtesy spacing --------------------------------------------------------------------------


def test_consecutive_sends_are_spaced() -> None:
    clock = {"now": 100.0}
    naps: list[float] = []

    def monotonic() -> float:
        return clock["now"]

    def sleep(s: float) -> None:
        naps.append(s)
        clock["now"] += s

    opener = FakeOpener((200, "{}"), (200, "{}"), (200, "{}"))
    client = PagingClient(_settings(), opener=opener, monotonic=monotonic, sleep=sleep)
    client.send_page(people=["101"], message="a")  # first send never waits
    clock["now"] += 0.5
    client.send_page(people=["101"], message="b")  # 1.5 s early ⇒ naps 1.5
    clock["now"] += 5.0
    client.send_page(people=["101"], message="c")  # already spaced ⇒ no nap
    assert naps == [1.5]


# --- TLS posture -------------------------------------------------------------------------------


def test_hostname_endpoint_verifies_fully() -> None:
    opener = FakeOpener((200, "{}"))
    _client(opener).send_page(people=["101"], message="x")
    assert opener.contexts == [None]  # default context ⇒ full verification


def test_raw_ip_endpoint_relaxes_hostname_verification() -> None:
    assert host_is_ipv4("https://203.0.113.21/api.php")
    assert not host_is_ipv4("https://pager.example.net/api.php")
    opener = FakeOpener((200, "{}"))
    settings = _settings(url="https://203.0.113.21/api.php")
    _client(opener, settings).send_page(people=["101"], message="x")
    ctx = opener.contexts[0]
    assert ctx is not None and ctx.check_hostname is False and ctx.verify_mode == ssl.CERT_NONE
