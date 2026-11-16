"""Publish a gated demo bundle to GitHub Pages and/or a VM (feature 136, research R10).

Nothing leaves the laptop unless the bundle's tree digest equals the one the last GATED export
recorded privately (FR-014). Any edit after the gate, even a single byte, refuses with exit 3.
Pages is a single-commit orphan branch force-pushed through ``configstore`` (the git
quarantine); the VM is a fixed-argv ``rsync -a --delete``.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from obi.core.demo import LeakGateError, ProfileError
from obi.core.demo.export import ExportProfile, bundle_digest, digest_path

_RSYNC_TIMEOUT = 1800.0

Runner = Callable[..., Any]
Pusher = Callable[[Path, str, str, str, datetime], str]


@dataclass
class PublishResult:
    files: int
    total_bytes: int
    destinations: list[str]
    dry_run: bool
    pushed: dict[str, str] = field(default_factory=dict)  # destination → sha / "ok"


def _default_pusher(src: Path, remote: str, branch: str, message: str, when: datetime) -> str:
    from obi.core.configstore import push_orphan_branch

    return push_orphan_branch(src, remote, branch, message, when)


def verify_digest(bundle: Path) -> str:
    """The bundle's digest, or LeakGateError unless it equals the last gated build's."""

    try:
        recorded = json.loads(digest_path().read_text(encoding="utf-8")).get("digest")
    except (OSError, ValueError) as exc:
        raise LeakGateError(
            "no gated build is recorded — run `obi demo export` before publishing"
        ) from exc
    actual = bundle_digest(bundle)
    if actual != recorded:
        raise LeakGateError(
            "the bundle differs from the last gated build (edited, or a different bundle) — "
            "re-run `obi demo export`; nothing was sent"
        )
    return actual


def publish(
    bundle: Path,
    profile: ExportProfile,
    *,
    pages: bool,
    vm: bool,
    dry_run: bool = False,
    runner: Runner = subprocess.run,
    pusher: Pusher = _default_pusher,
    audit: Any = None,
) -> PublishResult:
    if not (pages or vm):
        raise ProfileError("choose a target: --pages, --vm, or both")
    if pages and not profile.pages_remote:
        raise ProfileError(
            "--pages needs [demo.publish] pages_remote, the public repository's git URL (e.g. "
            "git@github.com:<you>/obi.git). GitHub Pages then serves its "
            f"'{profile.pages_branch}' branch."
        )
    if vm and not profile.vm_target:
        raise ProfileError("--vm needs [demo.publish] vm_target (user@host:/path)")
    if not bundle.is_dir():
        raise ProfileError(f"no bundle at {bundle} — run `obi demo export` first")

    digest = verify_digest(bundle)
    files = [p for p in bundle.rglob("*") if p.is_file()]
    destinations = []
    if pages:
        destinations.append(f"pages:{profile.pages_remote}#{profile.pages_branch}")
    if vm:
        destinations.append(f"vm:{profile.vm_target}")
    result = PublishResult(len(files), sum(p.stat().st_size for p in files), destinations,
                           dry_run)  # fmt: skip
    if dry_run:
        return result

    when = datetime.now(UTC)
    if pages and profile.pages_remote:
        target = destinations[0]
        try:
            sha = pusher(bundle, profile.pages_remote, profile.pages_branch,
                         f"obi demo {digest[:12]}", when)  # fmt: skip
        except (subprocess.SubprocessError, OSError) as exc:
            _audit(audit, target, "error", result, digest)
            raise ProfileError(f"pages push failed: {_why(exc)}") from exc
        result.pushed[target] = sha
        _audit(audit, target, "ok", result, digest)
    if vm and profile.vm_target:
        target = destinations[-1]
        try:
            runner(["rsync", "-a", "--delete", f"{bundle}/", profile.vm_target],
                   check=True, capture_output=True, text=True, timeout=_RSYNC_TIMEOUT)  # fmt: skip
        except (subprocess.SubprocessError, OSError) as exc:
            _audit(audit, target, "error", result, digest)
            raise ProfileError(f"vm rsync failed: {_why(exc)}") from exc
        result.pushed[target] = "ok"
        _audit(audit, target, "ok", result, digest)
    return result


def _why(exc: BaseException) -> str:
    stderr = getattr(exc, "stderr", None)
    return (stderr or str(exc)).strip().splitlines()[-1] if (stderr or str(exc)).strip() else "?"


def _audit(audit: Any, target: str, outcome: str, result: PublishResult, digest: str) -> None:
    if audit is None:
        return
    from obi.core.models import AuditOutcome, AuditRecord

    audit.record(
        AuditRecord(
            timestamp=datetime.now(UTC),
            action="demo_publish",
            target=target,
            reason="operator-initiated static demo publish (feature 136)",
            outcome=AuditOutcome.OK if outcome == "ok" else AuditOutcome.ERROR,
            params={
                "files": result.files,
                "total_bytes": result.total_bytes,
                "digest": digest[:16],
            },  # fmt: skip
        )
    )
