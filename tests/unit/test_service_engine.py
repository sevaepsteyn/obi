"""The launchd supervision engine (feature 081): specs, plist, launchctl, status, lifecycle.

Pure logic (plist render, guard, status parse, freshness) runs anywhere; the ``launchctl`` and
platform boundaries are monkeypatched so the whole engine is testable without a Mac.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import time
from pathlib import Path

import pytest

from obi.core import service as svc

# --- specs ----------------------------------------------------------------------------------------


def test_specs_have_stable_labels_verbs_and_paths() -> None:
    slack = svc.slack_spec()
    assert slack.label == "com.obi.slack"
    assert slack.verb == ("slack", "run")
    assert slack.plist_path.name == "com.obi.slack.plist"
    assert slack.log_path.name == "slack.log"
    assert slack.heartbeat_path.name == "slack.heartbeat.json"

    web = svc.web_spec()
    assert web.label == "com.obi.web"
    assert web.verb == ("web", "serve")
    assert web.log_path.name == "web-serve.log"
    assert web.kind is svc.ServiceKind.WEB

    radio = svc.radio_spec()
    assert radio.label == "com.obi.radio"
    assert radio.verb == ("radio", "run")
    assert radio.log_path.name == "radio.log"
    assert radio.heartbeat_path.name == "radio.heartbeat.json"
    assert radio.kind is svc.ServiceKind.RADIO
    assert svc.spec_for("radio") == radio
    assert radio in svc.all_specs()


def test_radio_plist_is_standard_process_type(tmp_path: Path) -> None:
    # feature 088: continuous capture + on-device STT must never run darwin-bg (the 088-on-main
    # QoS lesson); ProcessType Standard is asserted for the radio daemon specifically.
    plist = plistlib.loads(
        svc.render_plist(
            svc.radio_spec(),
            obi_exec=tmp_path / "obi",
            run_at_login=True,
            environment={},
            workdir=tmp_path,
        )
    )
    assert plist["ProcessType"] == "Standard"
    assert plist["ProgramArguments"][1:] == ["radio", "run"]


# --- plist rendering ------------------------------------------------------------------------------


def test_render_plist_shape(tmp_path: Path) -> None:
    spec = svc.web_spec()
    exec_path = tmp_path / "obi"
    data = svc.render_plist(
        spec, obi_exec=exec_path, run_at_login=True, environment={"FOO": "bar"}, workdir=tmp_path
    )
    plist = plistlib.loads(data)
    assert plist["Label"] == "com.obi.web"
    assert plist["ProgramArguments"] == [str(exec_path), "web", "serve"]
    assert plist["RunAtLoad"] is True
    assert plist["KeepAlive"] is True
    assert plist["ThrottleInterval"] == svc.THROTTLE_INTERVAL
    assert plist["StandardOutPath"] == plist["StandardErrorPath"] == str(spec.log_path)
    path_env = plist["EnvironmentVariables"]["PATH"]
    assert "/usr/bin" in path_env  # openssl for first-run cert generation
    assert str(exec_path.parent) in path_env  # venv bin
    assert plist["EnvironmentVariables"]["FOO"] == "bar"


def test_render_plist_carries_no_secret(tmp_path: Path) -> None:
    data = svc.render_plist(
        svc.slack_spec(),
        obi_exec=tmp_path / "obi",
        run_at_login=True,
        environment={},
        workdir=tmp_path,
    ).decode()
    assert "token" not in data.lower()
    assert "api_key" not in data.lower()


def test_render_plist_run_at_login_false(tmp_path: Path) -> None:
    plist = plistlib.loads(
        svc.render_plist(
            svc.slack_spec(),
            obi_exec=tmp_path / "obi",
            run_at_login=False,
            environment={},
            workdir=tmp_path,
        )
    )
    assert plist["RunAtLoad"] is False


# --- guard + exec resolution ----------------------------------------------------------------------


def test_worktree_exec_is_refused() -> None:
    with pytest.raises(svc.ServiceError):
        svc.assert_primary_checkout(
            Path("/Users/obi/src/obi-worktrees/081-daemon-mgmt/.venv/bin/obi")
        )


def test_primary_checkout_exec_passes() -> None:
    svc.assert_primary_checkout(Path("/Users/obi/src/obi/.venv/bin/obi"))  # no raise


def test_resolve_obi_exec_is_sibling_of_interpreter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(svc.sys, "executable", "/x/y/.venv/bin/python3")
    assert svc.resolve_obi_exec() == Path("/x/y/.venv/bin/obi")


def test_resolve_obi_exec_does_not_follow_venv_symlink(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The venv's python3 is a symlink to the real (uv-managed) interpreter, whose bin has NO obi
    # shim. resolve_obi_exec() must return the venv's obi (beside the symlink), not follow it —
    # else launchd points at a nonexistent binary and fails to exec (EX_CONFIG 78). Regression.
    real_bin = tmp_path / "uv" / "bin"
    real_bin.mkdir(parents=True)
    (real_bin / "python3").write_text("#!real interpreter")
    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "obi").write_text("#!obi shim")
    (venv_bin / "python3").symlink_to(real_bin / "python3")

    monkeypatch.setattr(svc.sys, "executable", str(venv_bin / "python3"))
    resolved = svc.resolve_obi_exec()
    assert resolved == venv_bin / "obi"
    assert resolved.exists()  # the venv shim exists; the resolved-symlink path would not


# --- launchctl wrappers ---------------------------------------------------------------------


class _FakeRun:
    """Records launchctl argv; `print` returns configured text (empty ⇒ not-loaded, rc 1)."""

    def __init__(self, print_output: str = "") -> None:
        self.calls: list[list[str]] = []
        self.print_output = print_output

    def __call__(self, argv: list[str], capture_output: bool = True, text: bool = True):  # noqa: ANN204
        self.calls.append(argv)
        sub = argv[1] if len(argv) > 1 else ""
        if sub == "print":
            rc = 0 if self.print_output else 1
            return subprocess.CompletedProcess(argv, rc, self.print_output, "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    @property
    def subs(self) -> list[str]:
        return [c[1] for c in self.calls]


def test_launchctl_wrappers_issue_correct_subcommands(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(svc, "is_macos", lambda: True)
    fake = _FakeRun()
    monkeypatch.setattr(svc.subprocess, "run", fake)
    spec = svc.web_spec()
    svc.bootstrap(spec)
    svc.enable(spec)
    svc.kickstart(spec, restart=True)
    svc.bootout(spec)
    assert fake.subs == ["bootstrap", "enable", "kickstart", "bootout"]
    assert "-k" in fake.calls[2]  # restart


def test_launchctl_nonzero_surfaces_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(svc, "is_macos", lambda: True)

    def boom(argv, capture_output=True, text=True):  # noqa: ANN001, ANN202
        return subprocess.CompletedProcess(argv, 5, "", "kaboom")

    monkeypatch.setattr(svc.subprocess, "run", boom)
    with pytest.raises(svc.ServiceError, match="kaboom"):
        svc.bootstrap(svc.web_spec())


def test_bootout_missing_is_benign(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(svc, "is_macos", lambda: True)

    def not_loaded(argv, capture_output=True, text=True):  # noqa: ANN001, ANN202
        return subprocess.CompletedProcess(argv, 3, "", "No such process")

    monkeypatch.setattr(svc.subprocess, "run", not_loaded)
    svc.bootout(svc.web_spec())  # missing_ok default ⇒ no raise
    with pytest.raises(svc.ServiceError):
        svc.bootout(svc.web_spec(), missing_ok=False)


def test_require_macos_off_darwin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(svc, "is_macos", lambda: False)
    with pytest.raises(svc.ServiceError, match="macOS"):
        svc.require_macos()


# --- status parsing -------------------------------------------------------------------------


def test_parse_launchd_state_running() -> None:
    text = "\n".join(
        ["com.obi.web = {", "  state = running", "  pid = 4242", "  last exit code = 0", "}"]
    )
    state = svc.parse_launchd_state(text, installed=True)
    assert state.loaded and state.running and state.pid == 4242 and state.last_exit == 0


def test_parse_launchd_state_crashed() -> None:
    state = svc.parse_launchd_state("state = not running\nlast exit code = 78\n", installed=True)
    assert state.loaded and not state.running and state.pid is None and state.last_exit == 78


def test_service_state_not_loaded_degrades(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(svc, "is_macos", lambda: True)
    monkeypatch.setattr(svc.subprocess, "run", _FakeRun(print_output=""))  # print rc 1
    state = svc.service_state(svc.web_spec())
    assert state.loaded is False and state.pid is None


# --- lifecycle ------------------------------------------------------------------------------


def test_install_writes_plist_then_loads(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(svc, "is_macos", lambda: True)
    monkeypatch.setattr(svc, "LAUNCH_AGENTS_DIR", tmp_path / "LaunchAgents")
    fake = _FakeRun(print_output="state = running\npid = 99\nlast exit code = 0\n")
    monkeypatch.setattr(svc.subprocess, "run", fake)
    spec = svc.web_spec()
    state = svc.install(
        spec, obi_exec=tmp_path / "obi", run_at_login=True, environment={}, workdir=tmp_path
    )
    assert spec.plist_path.exists()
    plist = plistlib.loads(spec.plist_path.read_bytes())
    assert plist["Label"] == "com.obi.web"
    # idempotent (re)load: bootout first, then bootstrap/enable/kickstart, then a status print
    assert fake.subs[:4] == ["bootout", "bootstrap", "enable", "kickstart"]
    assert state.running and state.pid == 99


def test_uninstall_boots_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(svc, "is_macos", lambda: True)
    fake = _FakeRun()
    monkeypatch.setattr(svc.subprocess, "run", fake)
    svc.uninstall(svc.web_spec())
    assert fake.subs == ["bootout"]


def test_restart_not_loaded_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(svc, "is_macos", lambda: True)
    monkeypatch.setattr(svc.subprocess, "run", _FakeRun(print_output=""))  # print ⇒ not loaded
    with pytest.raises(svc.ServiceError, match="not loaded"):
        svc.restart(svc.web_spec())


def test_detect_stray_listener_excludes_self(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_pgrep(argv, capture_output=True, text=True):  # noqa: ANN001, ANN202
        return subprocess.CompletedProcess(argv, 0, f"{os.getpid()}\n4242\n", "")

    monkeypatch.setattr(svc.subprocess, "run", fake_pgrep)
    pids = svc.detect_stray_listener()
    assert 4242 in pids and os.getpid() not in pids


def test_detect_stray_listener_none_running(monkeypatch: pytest.MonkeyPatch) -> None:
    def none(argv, capture_output=True, text=True):  # noqa: ANN001, ANN202
        return subprocess.CompletedProcess(argv, 1, "", "")

    monkeypatch.setattr(svc.subprocess, "run", none)
    assert svc.detect_stray_listener() == []


# --- collector freshness --------------------------------------------------------------------


def test_collector_health_fresh_stale_and_never(tmp_path: Path) -> None:
    now = time.time()
    (tmp_path / "metrics.log").write_text("x")
    os.utime(tmp_path / "metrics.log", (now - 100, now - 100))  # cadence 300 ⇒ fresh
    (tmp_path / "dash.log").write_text("x")
    os.utime(tmp_path / "dash.log", (now - 1000, now - 1000))  # cadence 60 ⇒ stale (095)
    health = {h.name: h for h in svc.collector_health(tmp_path, now=now)}
    assert len(health) == len(svc.COLLECTORS)
    assert health["metrics"].stale is False
    assert health["dash"].stale is True
    # a collector whose log is missing ⇒ never run ⇒ stale
    assert health["watch"].age_s is None and health["watch"].stale is True
