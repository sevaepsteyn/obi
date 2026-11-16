"""Local git-backed store for RouterOS device configs (feature 026).

obi's config-in-git backup. A scheduled ``obi configs ingest`` captures each device's **redacted**
``/export`` and this module records it as one file per device (``<identity>.rsc``) in a git working
tree under XDG state; git history is the per-device change history. Deliberately git rather than
SQLite (unlike ``logstore``/``metricstore``): configs are versioned text and git natively gives
history, diff, and as-of retrieval — reimplementing those over SQL would be *more* code (R2).

This is the ONLY module permitted to shell out to ``git`` (Constitution Principle IV — git is
quarantined here, exactly as ``sqlite3`` is quarantined in the stores). Every call is a fixed-argv
``subprocess.run`` — no shell, no interpolation — as ``ar/server.py`` and ``voice/tts.py`` invoke
``openssl``/``say``. Content is opaque text keyed by device identity, so a later
Siklu/UniFi capture can reuse the store unchanged.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from obi.core.models import ConfigVersionRef

# The volatile first line of a RouterOS ``/export`` — e.g. ``# 2026-07-05 12:00:00 by RouterOS X``.
# It changes every run, so storing it verbatim would commit every device every run (FR-004). We drop
# it before storing; provenance (capture time, version) rides in the commit metadata instead.
_HEADER_RE = re.compile(r"^#\s*\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\s+by RouterOS\b.*$")
_VERSION_RE = re.compile(r"^#\s*\d{4}-\d{2}-\d{2}.*by RouterOS\s+(\S+)")
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")
_RELATIVE_RE = re.compile(r"^(\d+)\s*([smhdw])$")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")
_GIT_TIMEOUT = 30.0
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}

# git identity + settings applied to every commit so the store never depends on (or pollutes) the
# operator's global git identity, and a global ``commit.gpgsign=true`` can't make a commit fail.
_GIT_COMMIT_CONFIG = (
    "-c",
    "user.name=obi",
    "-c",
    "user.email=obi@localhost",
    "-c",
    "commit.gpgsign=false",
)


@dataclass
class StoredDevice:
    """One device's presence in the store — the raw git facts, before the roster join (status)."""

    identity: str
    versions: int
    last_commit: str | None
    last_changed_at: datetime | None


# --- pure helpers (independently testable) -----------------------------------------------------


#: Transient device-STATE comment ``/export`` emits (e.g. a netwatch startup-delay countdown after
#: a reboot: ``# Warning: probe waiting startup-delay=5m; 2m11s remaining``). Not configuration —
#: it appears and vanishes with device state, so it must not register as a config change (099).
#: Stable device-generated comments (``# model = ...``, ``# ... not a bridge port``) don't match.
_WARNING_RE = re.compile(r"^\s*# Warning:")


def normalize_export(text: str) -> str:
    """Canonicalize a raw ``/export`` for storage: drop volatile lines, one trailing NL.

    The result is what change-detection compares, so an unchanged device must produce identical
    bytes. Dropped as volatile: the dated header, and transient ``# Warning: ...`` state comments
    (feature 099). A warning splits its entry into three lines (bare menu path, warning, bare
    command) where an unwarned entry is a terse one-liner — so dropping the warning also rejoins
    that split, making warning presence byte-invisible. Idempotent:
    ``normalize_export(normalize_export(x)) == normalize_export(x)``. Nothing else is reordered or
    altered — RouterOS rule order is semantic and the stored file stays a faithful capture.
    """

    kept: list[str] = []
    join_pending = False  # dropped warning(s) sat right after a bare menu-path line
    for line in text.splitlines():
        if _HEADER_RE.match(line):
            continue
        if _WARNING_RE.match(line):
            # only a warning in the observed split shape re-joins; elsewhere it is just dropped
            join_pending = bool(kept) and kept[-1].startswith("/") and "=" not in kept[-1]
            continue
        if join_pending and not line.startswith(("/", "#")):
            kept[-1] = f"{kept[-1]} {line}"
        else:
            kept.append(line)
        join_pending = False
    body = "\n".join(kept).strip("\n")
    return body + "\n" if body else ""


def is_line_permutation(a: str, b: str) -> bool:
    """True iff ``a`` and ``b`` hold exactly the same lines (as a multiset) in a different order.

    Feature 099: classifies a reorder-only export flip — RouterOS 7 re-emitting an entry at a
    different position with zero content change — so the watch config-change pass can suppress
    the alert while the store keeps the commit. Multiset, not set: a change in a duplicated
    line's count is a real change. Identical bytes are not a permutation.
    """

    return a != b and Counter(a.splitlines()) == Counter(b.splitlines())


#: RouterOS ``/export`` (even non-``show-sensitive``) hides user passwords, wireless keys, and IPsec
#: secrets, but NOT the **SNMP v2c community** — it appears as ``name=`` in ``/snmp community`` AND
#: as ``trap-community=`` in ``/snmp set`` (both verified cleartext live). We scrub both here so the
#: redaction guarantee holds (FR-002/SC-005), plus SNMPv3 auth/priv passwords defensively.
_SNMP_COMMUNITY_PREFIX = "/snmp community"
_REDACTED_VALUE = "<redacted-by-obi>"
_MIN_SECRET_LEN = 4  # never redact a trivially short literal (would shred unrelated config)
_NAME_VALUE_RE = re.compile(r'(\bname=)("[^"]*"|\S+)')
_SENSITIVE_FIELD_RE = re.compile(
    r'\b((?:authentication-password|encryption-password|trap-community)=)("[^"]*"|\S+)'
)


def redact_export(text: str, *, extra_secrets: tuple[str, ...] = ()) -> str:
    """Scrub secrets ``/export`` leaves cleartext, structurally + by known literal (FR-002/SC-005).

    Two layers: (1) **structural** — redact the SNMP community wherever it appears by field
    (``name=`` inside ``/snmp community``; ``trap-community=`` in ``/snmp set``) plus SNMPv3
    auth/priv passwords, so a community obi doesn't even know is still scrubbed; (2) **value
    backstop** — any known secret from obi's config (``extra_secrets``: the SNMP community, keys,
    passwords) is redacted wherever it literally appears, catching locations a field list misses
    (this layer caught a ``trap-community`` leak the field list alone did not). Redaction is to a
    **stable** placeholder, so a rotation shows no diff (accepted: redacted backup, not a restore
    image). Handles both ``terse`` and section-header ``/export`` layouts.
    """

    out: list[str] = []
    in_snmp_community = False
    for line in text.splitlines():
        if line.startswith("/"):
            in_snmp_community = line.startswith(_SNMP_COMMUNITY_PREFIX)
        if in_snmp_community and "name=" in line:
            line = _NAME_VALUE_RE.sub(rf"\g<1>{_REDACTED_VALUE}", line)
        line = _SENSITIVE_FIELD_RE.sub(rf"\g<1>{_REDACTED_VALUE}", line)
        out.append(line)
    result = "\n".join(out)
    for secret in extra_secrets:
        if secret and len(secret) >= _MIN_SECRET_LEN:
            result = result.replace(secret, _REDACTED_VALUE)
    return result + "\n" if text.endswith("\n") and result else result


# feature 028: field-aware redaction for RouterOS **API** rows (dicts), analogous to redact_export
# (which is line-based, for /export script TEXT). The RouterOS API has no read/secret split, so a
# general read could otherwise surface a PPP secret, wireless key, or SNMP community to the model.
_SENSITIVE_KEY_RE = re.compile(
    r"(pass(word|wd)?|secret|passphrase|(^|[-_])key$|(^|[-_])psk$|pre-shared-key|wpa\d?|eap|"
    r"private-key|auth.*password|enc.*password)",
    re.IGNORECASE,
)
# Field names that are secrets specifically under the /snmp menu (the community string) — the same
# value redact_export scrubs from /export.
_SNMP_SECRET_KEYS = frozenset({"name", "community", "trap-community"})


def redact_scalar(value: str, extra_secrets: tuple[str, ...] = ()) -> str:
    """Value-backstop: redact any known config secret that appears literally in ``value``."""

    result = value
    for secret in extra_secrets:
        if secret and len(secret) >= _MIN_SECRET_LEN:
            result = result.replace(secret, _REDACTED_VALUE)
    return result


def _is_sensitive_key(key: str, *, snmp: bool) -> bool:
    if _SENSITIVE_KEY_RE.search(key):
        return True
    return snmp and key.lower() in _SNMP_SECRET_KEYS


def redact_records(
    records: list[dict[str, Any]], *, path: str = "", extra_secrets: tuple[str, ...] = ()
) -> list[dict[str, Any]]:
    """Redact RouterOS API rows, mirroring :func:`redact_export`'s guarantee for structured data.

    Two layers, same stable ``<redacted-by-obi>`` placeholder: (1) **field-name** — redact the value
    of any key that looks like a credential (password/secret/key/psk/pre-shared-key/…), plus
    ``name``/``community`` under the ``/snmp`` menu; (2) **value backstop** — redact any known
    secret from obi's config wherever it appears as a value. Returns fresh dicts; inputs untouched.
    """

    snmp = path.lstrip("/").startswith("snmp")
    out: list[dict[str, Any]] = []
    for record in records:
        red: dict[str, Any] = {}
        for key, value in record.items():
            if _is_sensitive_key(str(key), snmp=snmp):
                red[key] = _REDACTED_VALUE
            elif isinstance(value, str):
                red[key] = redact_scalar(value, extra_secrets)
            else:
                red[key] = value
        out.append(red)
    return out


def parse_router_version(text: str) -> str | None:
    """Extract the RouterOS version from a raw ``/export`` header (provenance), else ``None``."""

    for line in text.splitlines():
        match = _VERSION_RE.match(line)
        if match:
            return match.group(1)
    return None


def path_for_identity(identity: str) -> str:
    """Map a device identity to its store filename, sanitized to a safe, traversal-free name."""

    safe = _UNSAFE_NAME.sub("_", identity.strip()) or "device"
    return f"{safe}.rsc"


def identity_for_path(name: str) -> str:
    """Inverse of :func:`path_for_identity` for display (lossy only for non-safe identities)."""

    return name[:-4] if name.endswith(".rsc") else name


# --- git plumbing ------------------------------------------------------------------------------


def _git(
    repo_path: Path,
    *args: str,
    env: dict[str, str] | None = None,
    timeout: float = _GIT_TIMEOUT,
) -> str:
    """Run one fixed-argv git command in ``repo_path`` and return stdout (raises on non-zero)."""

    result = subprocess.run(  # noqa: S603 - fixed argv, no shell, no interpolation
        ["git", "-C", str(repo_path), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    return result.stdout


def _git_ok(repo_path: Path, *args: str) -> bool:
    """Run a git command for its exit status only (0 ⇒ True). Used with ``--quiet`` predicates."""

    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", "-C", str(repo_path), *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT,
    )
    return result.returncode == 0


def ensure_repo(repo_path: Path) -> None:
    """Create the git repo if absent (idempotent). Call ONCE before any concurrent capture.

    git is not safe to init/commit against concurrently, so the engine fans out the SSH reads but
    records versions sequentially; this must run before that fan-out.
    """

    if (repo_path / ".git").exists():
        return
    repo_path.mkdir(parents=True, exist_ok=True)
    _git(repo_path, "init", "-q", "-b", "main")


def record_version(
    repo_path: Path,
    identity: str,
    content: str,
    *,
    address: str,
    router_version: str | None,
    when: datetime,
    extra_secrets: tuple[str, ...] = (),
) -> str | None:
    """Store a device's config; commit + return the sha iff it changed, else ``None``.

    Normalizes (drops the volatile header) AND redacts (scrubs the SNMP community + any SNMPv3
    keys ``/export`` leaves cleartext), writes ``<identity>.rsc``, stages it, and commits only when
    the stored content differs from the last version (git itself is the change detector). One commit
    per device; capture time + version + source address ride in the commit message and author date.
    Not concurrency-safe (see ``ensure_repo``).
    """

    ensure_repo(repo_path)
    rel = path_for_identity(identity)
    (repo_path / rel).write_text(
        redact_export(normalize_export(content), extra_secrets=extra_secrets), encoding="utf-8"
    )
    _git(repo_path, "add", "--", rel)
    # `diff --cached --quiet` exits 0 when nothing is staged (unchanged), 1 when there is a change.
    if _git_ok(repo_path, "diff", "--cached", "--quiet", "--", rel):
        return None
    stamp = when.isoformat()
    ver = f" RouterOS {router_version}" if router_version else ""
    message = (
        f"{identity}: config change\n\ndevice: {identity} ({address}){ver}\ncaptured-at: {stamp}"
    )
    env = {**os.environ, "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
    _git(repo_path, *_GIT_COMMIT_CONFIG, "commit", "-q", "-m", message, env=env)
    return _git(repo_path, "rev-parse", "HEAD").strip() or None


# --- feature 136: publish a directory as a single-commit orphan branch ------------------------

_PUSH_TIMEOUT = 600.0


def push_orphan_branch(
    src_dir: Path, remote: str, branch: str, message: str, when: datetime
) -> str:
    """Force-push ``src_dir``'s files to ``remote`` as ``branch``: ONE commit, no history.

    The commit is built in a throwaway git dir whose work tree is ``src_dir`` (nothing is copied,
    no ``.git`` is left behind in it), under obi's fixed identity, so it never carries the
    operator's identity or this repository's history. Only ``refs/heads/<branch>`` is pushed;
    every other ref on the remote is untouched. Returns the pushed commit sha.
    """

    import tempfile

    with tempfile.TemporaryDirectory(prefix="obi-orphan-") as tmp:
        git_dir = Path(tmp) / "repo.git"
        stamp = when.isoformat()
        env = {
            **os.environ,
            "GIT_DIR": str(git_dir),
            "GIT_WORK_TREE": str(src_dir),
            "GIT_AUTHOR_DATE": stamp,
            "GIT_COMMITTER_DATE": stamp,
        }
        _git(src_dir, "init", "-q", "-b", branch, env=env)
        _git(src_dir, "add", "-A", ".", env=env)
        _git(src_dir, *_GIT_COMMIT_CONFIG, "commit", "-q", "-m", message, env=env)
        sha = _git(src_dir, "rev-parse", "HEAD", env=env).strip()
        _git(src_dir, "push", "-q", "--force", remote, f"HEAD:refs/heads/{branch}",
             env=env, timeout=_PUSH_TIMEOUT)  # fmt: skip
    return sha


# --- reads (never raise on "not found") --------------------------------------------------------


def current_content(repo_path: Path, identity: str) -> str | None:
    """The device's latest stored config (the working-tree file), or ``None`` if never captured."""

    file_path = repo_path / path_for_identity(identity)
    if not file_path.exists():
        return None
    return file_path.read_text(encoding="utf-8")


def content_at(repo_path: Path, identity: str, ref: str) -> str | None:
    """The device's config as of a resolved commit ``ref``, or ``None`` if absent there."""

    rel = path_for_identity(identity)
    try:
        return _git(repo_path, "show", f"{ref}:{rel}")
    except subprocess.CalledProcessError:
        return None


def resolve_ref(
    repo_path: Path, identity: str, spec: str, *, now: datetime | None = None
) -> str | None:
    """Resolve ``current``/a sha/an ISO date/a relative window (``1w``) to a commit for this device.

    For a time spec, returns the last commit that touched the device's file at or before that time
    (``git log --before``). Returns ``None`` when nothing matches (e.g. a window before the first
    capture, or an unknown device).
    """

    rel = path_for_identity(identity)
    text = (spec or "current").strip()
    if text.lower() in ("current", "", "head", "latest"):
        return "HEAD" if current_content(repo_path, identity) is not None else None

    before = _spec_to_before(text, now or datetime.now(UTC))
    if before is not None:
        try:
            out = _git(repo_path, "log", "-n1", "--format=%H", f"--before={before}", "--", rel)
        except subprocess.CalledProcessError:
            return None
        return out.strip() or None

    try:  # a raw sha / ref
        out = _git(repo_path, "rev-parse", "--verify", "--quiet", f"{text}^{{commit}}")
    except subprocess.CalledProcessError:
        return None
    return out.strip() or None


def _spec_to_before(spec: str, now: datetime) -> str | None:
    """A relative window (``30d``) → an ISO timestamp; an ISO date → itself; else ``None``."""

    match = _RELATIVE_RE.match(spec.lower())
    if match:
        seconds = int(match.group(1)) * _UNIT_SECONDS[match.group(2)]
        return (now - timedelta(seconds=seconds)).isoformat()
    if _ISO_DATE_RE.match(spec):
        return spec
    return None


def history(repo_path: Path, identity: str, limit: int) -> tuple[list[ConfigVersionRef], bool]:
    """A device's change points (newest first) + a truncated flag. Empty for an unknown device."""

    rel = path_for_identity(identity)
    try:
        out = _git(
            repo_path,
            "log",
            f"-n{limit + 1}",
            "--date=iso-strict",
            "--format=__C__%x1f%H%x1f%cI%x1f%s",
            "--numstat",
            "--",
            rel,
        )
    except subprocess.CalledProcessError:
        return [], False

    entries: list[ConfigVersionRef] = []
    for block in out.split("__C__"):
        block = block.strip("\n")
        if not block:
            continue
        head, _, rest = block.partition("\n")
        parts = head.lstrip("\x1f").split("\x1f")
        if len(parts) < 3:
            continue
        sha, iso, summary = parts[0], parts[1], parts[2]
        insertions = deletions = 0
        for line in rest.splitlines():
            cols = line.split("\t")
            if len(cols) >= 2 and (cols[0].isdigit() or cols[0] == "-"):
                insertions = int(cols[0]) if cols[0].isdigit() else 0
                deletions = int(cols[1]) if cols[1].isdigit() else 0
                break
        entries.append(
            ConfigVersionRef(
                commit=sha,
                captured_at=_parse_iso(iso),
                summary=summary,
                insertions=insertions,
                deletions=deletions,
            )
        )
    truncated = len(entries) > limit
    return entries[:limit], truncated


# git's canonical empty tree (SHA-1 repos; the 026 store is created with plain `git init`) —
# diffing a commit against it renders a file's full content as an all-additions unified diff.
_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def initial_diff(repo_path: Path, identity: str, ref: str) -> str:
    """The device's file at ``ref`` as an all-additions unified diff (baseline = empty tree)."""

    rel = path_for_identity(identity)
    try:
        return _git(repo_path, "diff", _EMPTY_TREE, ref, "--", rel)
    except subprocess.CalledProcessError:
        return ""


def previous_commit(repo_path: Path, identity: str, commit: str) -> str | None:
    """The version of this device's file immediately before ``commit``; ``None`` at the first."""

    rel = path_for_identity(identity)
    try:
        out = _git(repo_path, "log", "-n2", "--format=%H", commit, "--", rel)
    except subprocess.CalledProcessError:
        return None
    shas = out.split()
    return shas[1] if len(shas) >= 2 else None


def diff(repo_path: Path, identity: str, from_ref: str, to_ref: str) -> str:
    """Unified diff of the device's file between two resolved commits (empty if identical)."""

    rel = path_for_identity(identity)
    try:
        return _git(repo_path, "diff", from_ref, to_ref, "--", rel)
    except subprocess.CalledProcessError:
        return ""


def coverage(repo_path: Path) -> list[StoredDevice]:
    """Per-device store facts (identity, version count, last commit + time); [] for a new repo."""

    if not (repo_path / ".git").exists():
        return []
    devices: list[StoredDevice] = []
    for file_path in sorted(repo_path.glob("*.rsc")):
        rel = file_path.name
        try:
            last = _git(repo_path, "log", "-n1", "--format=%H%x1f%cI", "--", rel).strip()
            count_raw = _git(repo_path, "rev-list", "--count", "HEAD", "--", rel).strip()
        except subprocess.CalledProcessError:
            continue
        sha: str | None = None
        changed_at: datetime | None = None
        if last:
            sha, _, iso = last.partition("\x1f")
            changed_at = _parse_iso(iso) if iso else None
        devices.append(
            StoredDevice(
                identity=identity_for_path(rel),
                versions=int(count_raw or 0),
                last_commit=sha or None,
                last_changed_at=changed_at,
            )
        )
    return devices


def _parse_iso(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.strip())
    except (ValueError, TypeError):
        return None


# --- capture state (feature 085) -----------------------------------------------------------------
#
# The git history only records CHANGES — an unchanged capture leaves no trace, so "how fresh is
# the backup?" was unanswerable and `configs status` mislabeled long-stable devices as stale.
# Ingest now records every attempt in a sidecar JSON kept NEXT TO the repo (state, not config —
# never committed, never touching the working tree). Missing/malformed file ⇒ {} (first-run
# semantics, like the watch baselines): pre-085 stores show "no capture record yet" for one cron
# tick, then fill in.

CAPTURE_STATE_FILENAME = "config-captures.json"


def capture_state_path(repo_path: Path) -> Path:
    return repo_path.expanduser().parent / CAPTURE_STATE_FILENAME


def read_capture_state(repo_path: Path) -> dict[str, dict[str, Any]]:
    """The per-identity capture record; ``{}`` on any problem (first-run semantics)."""

    try:
        data = json.loads(capture_state_path(repo_path).read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): v for k, v in data.items() if isinstance(v, dict)}


def record_capture_state(repo_path: Path, updates: dict[str, dict[str, Any]]) -> None:
    """Merge one ingest run's per-device outcomes into the sidecar (atomic rewrite).

    An update WITHOUT ``last_success_at`` (a failed attempt) preserves the stored success
    timestamp — a dead device keeps its "last known good" while accumulating attempt info.
    """

    state = read_capture_state(repo_path)
    for identity, update in updates.items():
        entry = dict(state.get(identity, {}))
        if "last_success_at" not in update and "last_success_at" in entry:
            update = {**update, "last_success_at": entry["last_success_at"]}
        entry.update(update)
        state[identity] = entry
    path = capture_state_path(repo_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True) + "\n")
    os.replace(tmp, path)


# --- demo dataset support (feature 134) --------------------------------------------------------
# The demo sanitizer replays a window of this store's history into a FRESH repo (rewritten content,
# shifted dates, a fixed author) and its leak gate reads every object of the result. git stays
# quarantined here, so both go through these fixed-argv helpers.


def commits_between(repo_path: Path, since: datetime, until: datetime) -> list[tuple[str, str]]:
    """``(sha, author-date ISO)`` of commits in ``[since, until]``, oldest first ([] when none)."""

    try:
        out = _git(
            repo_path,
            "log",
            "--reverse",
            "--format=%H %aI",
            f"--since={since.isoformat()}",
            f"--until={until.isoformat()}",
        )
    except subprocess.CalledProcessError:
        return []
    pairs = [line.split(" ", 1) for line in out.splitlines() if line.strip()]
    return [(sha, when) for sha, when in pairs]


def last_commit_before(repo_path: Path, when: datetime) -> str | None:
    try:
        sha = _git(repo_path, "log", "-1", "--format=%H", f"--before={when.isoformat()}").strip()
    except subprocess.CalledProcessError:
        return None
    return sha or None


def commit_message(repo_path: Path, sha: str) -> str:
    return _git(repo_path, "log", "-1", "--format=%B", sha)


def tree_files(repo_path: Path, ref: str) -> dict[str, str]:
    """Every file of ``ref``'s tree with its content."""

    names = _git(repo_path, "ls-tree", "-r", "--name-only", ref).splitlines()
    return {name: _git(repo_path, "show", f"{ref}:{name}") for name in names if name}


def changed_files(repo_path: Path, sha: str) -> dict[str, str | None]:
    """The files ``sha`` touched → content at ``sha`` (``None`` = deleted there)."""

    out = _git(repo_path, "diff-tree", "--root", "--no-commit-id", "--name-status", "-r", sha)
    changed: dict[str, str | None] = {}
    for line in out.splitlines():
        status, _, name = line.partition("\t")
        if not name:
            continue
        changed[name] = None if status.startswith("D") else _git(repo_path, "show", f"{sha}:{name}")
    return changed


def commit_snapshot(
    repo_path: Path,
    files: dict[str, str | None],
    *,
    message: str,
    when: datetime,
    author_name: str,
    author_email: str,
) -> None:
    """Write/delete ``files`` and commit them with a fixed identity and both dates = ``when``."""

    ensure_repo(repo_path)
    for name, content in files.items():
        target = repo_path / name
        if content is None:
            target.unlink(missing_ok=True)
        else:
            target.write_text(content, encoding="utf-8")
    _git(repo_path, "add", "-A")
    if _git_ok(repo_path, "diff", "--cached", "--quiet"):
        return
    stamp = when.isoformat()
    env = {
        **os.environ,
        "GIT_AUTHOR_DATE": stamp,
        "GIT_COMMITTER_DATE": stamp,
        "GIT_AUTHOR_NAME": author_name,
        "GIT_AUTHOR_EMAIL": author_email,
        "GIT_COMMITTER_NAME": author_name,
        "GIT_COMMITTER_EMAIL": author_email,
    }
    _git(repo_path, *_GIT_COMMIT_CONFIG, "commit", "-q", "-m", message, env=env)


def dump_objects(repo_path: Path) -> bytes:
    """Every object in the repo — reachable or not — as ``git cat-file --batch`` output."""

    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", "-C", str(repo_path), "cat-file", "--batch-all-objects", "--batch"],
        check=True,
        capture_output=True,
        timeout=300,
    )
    return result.stdout
