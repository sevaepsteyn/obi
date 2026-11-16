"""Site Manager adapter: /v1/devices flattening + field mapping (feature 011, account surface).

The Site Manager surface is a single account-wide read (inventory + reachability across all sites);
it exposes no per-device cpu/memory/uptime, so those stay None without "unavailable" noise.
"""

from __future__ import annotations

from obi.core.drivers.unifi_sm import UnifiSiteManagerController, _flatten_sitemanager
from obi.core.models import Reachability, SourceOutcome
from tests.fakes import load_unifi_sitemanager, unifi_sm_settings


def _sm_controller() -> UnifiSiteManagerController:
    flat = _flatten_sitemanager(load_unifi_sitemanager())
    return UnifiSiteManagerController(unifi_sm_settings(), transport=lambda t: flat)


def test_flatten_groups_devices_by_host() -> None:
    flat = _flatten_sitemanager(load_unifi_sitemanager())
    assert len(flat) == 3
    assert {d["name"] for d in flat} == {"sm-gw-01", "sm-ap-01", "sm-sw-01"}


def test_maps_site_manager_device_fields() -> None:
    report = _sm_controller().fetch(name_filter=None, timeout=5.0)
    assert report.outcome is SourceOutcome.REACHABLE
    assert len(report.devices) == 3
    gw = next(s for s in report.devices if s.device.identity == "sm-gw-01")
    assert gw.device.address == "10.50.0.1"  # `ip`
    assert gw.device.model == "UXG Max"
    assert gw.device.version == "4.1.13"  # `version`
    assert gw.reachability is Reachability.REACHABLE  # `status` == "online"


def test_offline_status_maps_to_unreachable() -> None:
    devices = _sm_controller().fetch(name_filter=None, timeout=5.0).devices
    sw = next(s for s in devices if s.device.identity == "sm-sw-01")
    assert sw.reachability is Reachability.UNREACHABLE  # `status` == "offline"
    assert sw.device.version is None  # absent in the fixture → None, not invented


def test_no_health_gap_noise() -> None:
    # The Site Manager API exposes no per-device cpu/memory/uptime; we don't annotate every device.
    devices = _sm_controller().fetch(name_filter=None, timeout=5.0).devices
    assert all(s.notes == [] for s in devices)


def test_filter_applies() -> None:
    report = _sm_controller().fetch(name_filter="gw", timeout=5.0)
    assert [s.device.identity for s in report.devices] == ["sm-gw-01"]


def test_fetch_is_total_on_error() -> None:
    from obi.core.drivers.unifi_common import UnifiError

    def boom(timeout: float) -> list:
        raise UnifiError(SourceOutcome.AUTH_FAILED, "Site Manager API rejected the credential")

    report = UnifiSiteManagerController(unifi_sm_settings(), transport=boom).fetch(
        name_filter=None, timeout=5.0
    )
    assert report.outcome is SourceOutcome.AUTH_FAILED
    assert report.devices == []


def test_default_transport_wiring(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import httpx

    from obi.core.config import Secret, UnifiSiteManagerSettings

    captured: dict = {}

    class _Resp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"data": []}

    class _Client:
        def __init__(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
            captured.update(kwargs)

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

        def get(self, url, headers=None, params=None):  # type: ignore[no-untyped-def]
            captured["url"] = url
            captured["headers"] = headers
            captured["params"] = params
            return _Resp()

    monkeypatch.setattr(httpx, "Client", _Client)
    settings = UnifiSiteManagerSettings(api_key=Secret("sm-key"))
    UnifiSiteManagerController(settings)._default_transport(6.0)

    assert captured["verify"] is True and captured["timeout"] == 6.0
    assert captured["url"] == "https://api.ui.com/v1/devices"
    assert captured["headers"]["X-API-KEY"] == "sm-key"
    assert captured["params"].get("pageSize") == 200
