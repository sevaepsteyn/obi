"""obi demo publish — digest gate, targets, dry run, audit (feature 136, T040)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from obi.core.demo import LeakGateError, ProfileError
from obi.core.demo.export import ExportProfile, _record_digest, bundle_digest
from obi.core.demo.publish import publish


class Calls:
    def __init__(self) -> None:
        self.runs: list[list[str]] = []
        self.pushes: list[tuple[Any, ...]] = []
        self.audits: list[Any] = []

    def runner(self, argv: list[str], **_kw: Any) -> None:
        self.runs.append(argv)

    def pusher(self, src: Path, remote: str, branch: str, msg: str, when: datetime) -> str:
        self.pushes.append((src, remote, branch, msg))
        return "abc123"

    def record(self, rec: Any) -> None:
        self.audits.append(rec)


@pytest.fixture
def bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    root = tmp_path / "bundle"
    (root / "data").mkdir(parents=True)
    (root / "index.html").write_text("<html></html>")
    (root / "data" / "manifest.json").write_text(json.dumps({"format_version": 1}))
    _record_digest(root, bundle_digest(root))
    return root


def _profile(bundle: Path, **kw: Any) -> ExportProfile:
    return ExportProfile(dataset=bundle.parent / "ds", bundle_dir=bundle, **kw)


FULL = {"pages_remote": "git@example.invalid:me/obi.git", "vm_target": "me@vm:/srv/obi"}


def _go(bundle: Path, calls: Calls, **kw: Any) -> Any:
    profile = _profile(bundle, **kw.pop("profile", FULL))
    return publish(bundle, profile, runner=calls.runner, pusher=calls.pusher, audit=calls, **kw)


def test_any_edit_after_the_gate_refuses_before_any_subprocess(bundle: Path) -> None:
    (bundle / "index.html").write_text("<html>edited</html>")
    calls = Calls()
    with pytest.raises(LeakGateError) as err:
        _go(bundle, calls, pages=True, vm=True)
    assert err.value.exit_code == 3
    assert calls.runs == [] and calls.pushes == [] and calls.audits == []


def test_no_recorded_digest_refuses(bundle: Path, tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    (other / "x").write_text("x")
    with pytest.raises(LeakGateError):
        _go(other, Calls(), pages=False, vm=True)


def test_pages_without_remote_is_guidance_exit_2(bundle: Path) -> None:
    with pytest.raises(ProfileError) as err:
        _go(bundle, Calls(), pages=True, vm=False, profile={})
    assert err.value.exit_code == 2 and "pages_remote" in str(err.value)


def test_vm_is_fixed_argv_rsync(bundle: Path) -> None:
    calls = Calls()
    _go(bundle, calls, pages=False, vm=True)
    assert calls.runs == [["rsync", "-a", "--delete", f"{bundle}/", "me@vm:/srv/obi"]]
    assert calls.pushes == []


def test_pages_pushes_orphan_branch(bundle: Path) -> None:
    calls = Calls()
    result = _go(bundle, calls, pages=True, vm=False)
    (src, remote, branch, _msg), = calls.pushes
    assert (src, remote, branch) == (bundle, FULL["pages_remote"], "gh-pages")
    assert result.pushed == {f"pages:{FULL['pages_remote']}#gh-pages": "abc123"}


def test_dry_run_sends_nothing_and_reports(bundle: Path) -> None:
    calls = Calls()
    result = _go(bundle, calls, pages=True, vm=True, dry_run=True)
    assert calls.runs == [] and calls.pushes == [] and calls.audits == []
    assert result.files == 2 and result.total_bytes > 0 and len(result.destinations) == 2


def test_one_audit_record_per_target(bundle: Path) -> None:
    calls = Calls()
    _go(bundle, calls, pages=True, vm=True)
    assert [a.target for a in calls.audits] == [
        f"pages:{FULL['pages_remote']}#gh-pages", "vm:me@vm:/srv/obi"
    ]  # fmt: skip
    assert all(a.action == "demo_publish" for a in calls.audits)
