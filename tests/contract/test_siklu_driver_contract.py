"""Contract: SikluDriver is read-only, total, and issues ONLY the show allowlist (feature 020).

See contracts/siklu-driver.md. Uses the injected RecordingSshSession serving the REAL captured
fixtures — no real device (Principle II/IV).
"""

from __future__ import annotations

import pytest

from obi.core.drivers.siklu import SHOW_COMMANDS, SikluAuthError, SikluSession
from obi.core.models import Reachability, SikluRadioStatus
from tests.fakes import failing_siklu_driver, siklu_driver_from_fixture

# A read-only adapter MUST NOT expose any state-changing operation (Principle I).
WRITE_VERBS = ("set", "add", "remove", "update", "write", "exec", "create", "delete", "apply")


def test_fetch_radio_parses_real_fixture() -> None:
    driver, sessions = siklu_driver_from_fixture("tamarack-ptp-basswood-70")
    status = driver.fetch_radio("172.18.89.130", timeout=2.0)

    assert isinstance(status, SikluRadioStatus)
    assert status.reachability is Reachability.REACHABLE
    assert status.device.serial == "2B058C1831BA"
    assert status.device.identity == "tamarack-ptp-basswood-70"
    assert status.rf is not None and status.rf.operational is True
    assert status.rf.rssi_dbm == -44
    assert len(status.eth_ports) == 4
    eth1 = next(p for p in status.eth_ports if p.name == "eth1")
    assert eth1.operational_up is False


def test_driver_issues_only_allowlist_and_closes() -> None:
    driver, sessions = siklu_driver_from_fixture("tamarack-ptp-basswood-70")
    driver.fetch_radio("172.18.89.130", timeout=2.0)

    assert sessions, "expected the driver to open a session"
    session = sessions[0]
    # ONLY the show allowlist, in order, and nothing else.
    assert session.commands_sent == list(SHOW_COMMANDS)
    assert session.closed is True


def test_driver_exposes_no_write_methods() -> None:
    driver, _ = siklu_driver_from_fixture("tamarack-ptp-basswood-70")
    for verb in WRITE_VERBS:
        assert not hasattr(driver, verb), f"siklu exposes write-like method {verb!r}"


@pytest.mark.parametrize(
    "exc, expected",
    [
        (SikluAuthError("bad password"), Reachability.AUTH_FAILED),
        (TimeoutError("login timed out"), Reachability.TIMED_OUT),
        (ConnectionRefusedError("refused"), Reachability.UNREACHABLE),
        (OSError("no route to host"), Reachability.UNREACHABLE),
    ],
)
def test_driver_total_on_connect_failure(exc: Exception, expected: Reachability) -> None:
    driver, attempted = failing_siklu_driver(exc, host="172.25.110.9")
    status = driver.fetch_radio("172.25.110.9", timeout=0.5)

    assert isinstance(status, SikluRadioStatus)
    assert status.reachability is expected
    assert status.notes  # explains why
    assert attempted == ["172.25.110.9"]


def test_driver_unidentified_when_not_siklu() -> None:
    # A host that connects but yields no Siklu markers → UNIDENTIFIED, never a healthy radio.
    driver, _ = siklu_driver_from_fixture(**{"172.25.110.5": {cmd: "" for cmd in SHOW_COMMANDS}})
    status = driver.fetch_radio("172.25.110.5", timeout=1.0)
    assert status.reachability is Reachability.UNIDENTIFIED
    assert status.rf is None
    assert status.eth_ports == []


def test_driver_partial_read_stays_reachable() -> None:
    # show rf missing/empty but identity present → REACHABLE with a note, not a failure (FR-011).
    from tests.fakes import load_siklu_fixture

    bundle = dict(load_siklu_fixture("tamarack-ptp-basswood-70"))
    bundle["show rf"] = ""  # simulate an absent RF read
    driver, _ = siklu_driver_from_fixture(**{"172.18.89.130": bundle})
    status = driver.fetch_radio("172.18.89.130", timeout=1.0)

    assert status.reachability is Reachability.REACHABLE
    assert status.rf is None
    assert any("rf link unavailable" in note for note in status.notes)


def test_timeout_value_is_passed_to_session() -> None:
    captured: dict[str, float] = {}

    def factory(host: str, timeout: float) -> SikluSession:
        captured["timeout"] = timeout
        raise TimeoutError("t")

    from obi.core.drivers.siklu import SikluDriver
    from tests.fakes import siklu_settings

    driver = SikluDriver(siklu_settings(), session=factory)
    driver.fetch_radio("172.25.110.1", timeout=0.25)
    assert captured["timeout"] == 0.25
