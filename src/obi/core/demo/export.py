"""``obi demo export``: a 134 dataset → a self-contained static demo bundle (feature 136).

The bundle is obi's real web workspace and XR viewer plus one demo bootstrap. Its data is
pre-rendered at build time by the LIVE web handlers (``web/data_api.py``), run in-process
against the dataset with the clock frozen at the dataset's window end. A static host such as
GitHub Pages or a VM's web server then serves the bundle with no obi process behind it.

Build states (data-model.md):

    init → rendered → sliced → recordings → assets → budget_ok → gate_passed → digested
         → published → audited

As in 134's snapshot, the bundle is assembled in a private staging directory, gated as a whole
by 134's independent leak gate, and only then renamed into place.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import shutil
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from obi.core.demo import DemoError, ProfileError
from obi.core.demo.routerview import router_view
from obi.core.demo.routes import METRIC_WINDOWS, capture, data_path

DEFAULT_BASE_PATH = "/obi/"
MAX_FILE_BYTES = 95 * 1024 * 1024  # under GitHub Pages' 100 MB hard per-file limit


def _state_root() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(Path.home(), ".local", "state")
    return Path(base) / "obi"


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


@dataclass(frozen=True)
class ExportProfile:
    dataset: Path
    bundle_dir: Path
    base_path: str = DEFAULT_BASE_PATH
    logs_slice_rows: int = 20_000
    audit_slice_rows: int = 5_000
    tool_result_clip_bytes: int = 2_048
    max_bundle_mb: int = 500
    pages_remote: str | None = None
    pages_branch: str = "gh-pages"
    vm_target: str | None = None

    @property
    def state_dir(self) -> Path:
        return _state_root()


def load_export_profile(
    raw: dict[str, Any], *, config_path: Path, overrides: dict[str, Any] | None = None
) -> ExportProfile:
    """``[demo.export]`` + ``[demo.publish]`` (all optional), validated fail-closed."""

    demo = raw.get("demo", {}) or {}
    table: dict[str, Any] = dict(demo.get("export", {}) or {})
    publish: dict[str, Any] = dict(demo.get("publish", {}) or {})
    table.update({k: v for k, v in (overrides or {}).items() if v is not None})

    dataset = Path(str(table.get("dataset") or demo.get("output_dir") or "~/obi-demo/dataset"))
    dataset = dataset.expanduser()
    bundle_dir = Path(str(table.get("bundle_dir", "~/obi-demo/bundle"))).expanduser()
    config_dir = config_path.expanduser().resolve().parent
    for name, root in (("state", _state_root()), ("config", config_dir), ("dataset", dataset)):
        if _inside(bundle_dir, root):
            raise ProfileError(f"[demo.export] bundle_dir must not be inside the {name} directory")

    base_path = str(table.get("base_path", DEFAULT_BASE_PATH))
    if not (base_path.startswith("/") and base_path.endswith("/")) or "//" in base_path:
        raise ProfileError("[demo.export] base_path must look like /name/ (leading + trailing /)")

    ints = {
        key: int(table.get(key, default))
        for key, default in (
            ("logs_slice_rows", 20_000),
            ("audit_slice_rows", 5_000),
            ("tool_result_clip_bytes", 2_048),
            ("max_bundle_mb", 500),
        )
    }
    if any(v <= 0 for v in ints.values()):
        raise ProfileError("[demo.export] slice sizes, clip and budget must be positive")

    return ExportProfile(
        dataset=dataset,
        bundle_dir=bundle_dir,
        base_path=base_path,
        pages_remote=publish.get("pages_remote") or None,
        pages_branch=str(publish.get("pages_branch", "gh-pages")),
        vm_target=publish.get("vm_target") or None,
        **ints,
    )


# --- the dataset as obi's web handlers see it ---------------------------------------------------

DATASET_FORMAT = 1
_METRICS_JS = Path(__file__).resolve().parents[2] / "web" / "static" / "widgets" / "met.js"


@dataclass(frozen=True)
class DatasetView:
    dataset: Path
    config: Any  # obi.core.config.Config, loaded with XDG_STATE_HOME = <dataset>/state
    manifest: dict[str, Any]
    now: datetime  # the dataset window's end: "now" for every rendered route


@contextmanager
def dataset_env(dataset: Path) -> Iterator[None]:
    """Point obi's XDG state resolution at the dataset (every store default resolves inside)."""

    previous = os.environ.get("XDG_STATE_HOME")
    os.environ["XDG_STATE_HOME"] = str(dataset / "state")
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("XDG_STATE_HOME", None)
        else:
            os.environ["XDG_STATE_HOME"] = previous


def load_dataset(dataset: Path) -> DatasetView:
    from obi.core.config import ConfigError, load_config

    manifest_path = dataset / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProfileError(f"no readable 134 dataset at {dataset}: {exc}") from exc
    if manifest.get("format_version") != DATASET_FORMAT:
        raise ProfileError(f"unsupported dataset format {manifest.get('format_version')!r}")
    with dataset_env(dataset):
        try:
            config = load_config(dataset / "config.toml")
        except ConfigError as exc:
            raise ProfileError(f"the dataset's config.toml does not load: {exc}") from exc
    now = datetime.fromisoformat(str(manifest["window"]["end"]))
    return DatasetView(dataset=dataset, config=config, manifest=manifest, now=now)


def metric_names() -> list[str]:
    """The metrics the MET picker offers (widgets/met.js ``METRICS``): each is pre-rendered."""

    text = _METRICS_JS.read_text(encoding="utf-8")
    block = text[text.index("const METRICS = [") : text.index("];", text.index("const METRICS"))]
    return re.findall(r"\['(\w+)',", block)


def _scrub_paths(obj: Any, roots: tuple[str, ...]) -> Any:
    """Local filesystem paths (``db_path``, ``repo_path``…) never reach a public bundle."""

    if isinstance(obj, str):
        for root in roots:
            if root and root in obj:
                obj = obj.replace(root, "")
        return obj
    if isinstance(obj, dict):
        return {k: _scrub_paths(v, roots) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub_paths(v, roots) for v in obj]
    return obj


class _Writer:
    def __init__(self, out_root: Path, roots: tuple[str, ...]) -> None:
        self.out_root = out_root
        self.roots = roots
        self.counts: dict[str, int] = {}

    def put(self, route: str, params: dict[str, str], payload: Any) -> None:
        path = self.out_root / data_path(route, params)
        path.parent.mkdir(parents=True, exist_ok=True)
        clean = _scrub_paths(payload, self.roots)
        path.write_text(json.dumps(clean, ensure_ascii=False), encoding="utf-8")
        self.counts[route] = self.counts.get(route, 0) + 1


def render_routes(view: DatasetView, out_root: Path, scratch: Path) -> dict[str, int]:
    """Pre-render every ``rendered``/``snapshot`` route with the LIVE handlers (research R2)."""

    from obi.web import data_api as api

    cfg = view.config
    # Read-only rendering: the configs route writes intent audit records — send them nowhere.
    quiet = dataclasses.replace(
        cfg,
        audit_path=scratch / "audit.jsonl",
        audit=dataclasses.replace(cfg.audit, enabled=False),
    )
    roots = (str(view.dataset.resolve()) + "/", str(view.dataset) + "/", str(Path.home()))
    w = _Writer(out_root, roots)

    def q(**params: str) -> str:
        return "?" + urlencode({k: v for k, v in params.items() if v})

    with dataset_env(view.dataset), api.frozen_now(view.now):
        w.put("dash", {}, capture(api.handle_dash_request, "/api/dash.json", cfg.dash)[1])
        w.put(
            "inventory",
            {},
            capture(api.handle_inventory_request, "/api/inventory.json", cfg.inventory_path)[1],
        )

        _s, fleet = capture(api.handle_health_request, "/api/health.json", quiet)
        w.put("health", {}, fleet)
        for row in fleet.get("devices", []):
            device = str(row.get("device", ""))
            if device:
                w.put(
                    "health",
                    {"device": device},
                    capture(
                        api.handle_health_request, "/api/health.json" + q(device=device), quiet
                    )[1],
                )

        history = cfg.snmp.history if cfg.snmp is not None else None
        _s, coverage = capture(api.handle_metrics_request, "/api/metrics.json", history)
        w.put("metrics", {}, coverage)
        for row in coverage.get("devices", []):
            target = str(row.get("device", ""))
            for metric in metric_names():
                for window in METRIC_WINDOWS:
                    params = {"target": target, "metric": metric, "window": window}
                    w.put(
                        "metrics",
                        params,
                        capture(
                            api.handle_metrics_request, "/api/metrics.json" + q(**params), history
                        )[1],
                    )

        _s, status = capture(api.handle_configs_request, "/api/configs.json", quiet)
        w.put("configs", {}, status)
        for row in status.get("rows", []):
            device = str(row.get("identity", ""))
            if not device or not row.get("has_backup"):
                continue
            _s, hist = capture(
                api.handle_configs_request, "/api/configs.json" + q(device=device), quiet
            )
            w.put("configs", {"device": device}, hist)
            for entry in hist.get("entries", []):
                commit = str(entry.get("commit", ""))
                for view_name, key in (("diff", "to"), ("show", "at")):
                    params = {"device": device, "view": view_name, key: commit}
                    w.put(
                        "configs",
                        params,
                        capture(
                            api.handle_configs_request, "/api/configs.json" + q(**params), quiet
                        )[1],
                    )

        for record in _status_devices(view.dataset):
            address = str(record.get("address", ""))
            if not address:
                continue
            export = _config_export(view.dataset, str(record.get("identity", "")))
            w.put("router", {"device": address}, router_view(record, export, read_at=view.now))
    return w.counts


def _status_devices(dataset: Path) -> list[dict[str, Any]]:
    path = dataset / "state" / "obi" / "ar" / "snapshots" / "status.json"
    try:
        devices = json.loads(path.read_text(encoding="utf-8"))["data"]["devices"]
    except (OSError, ValueError, KeyError, TypeError):
        return []
    return [d for d in devices if isinstance(d, dict)]


def _config_export(dataset: Path, identity: str) -> str | None:
    from obi.core.configstore import path_for_identity

    if not identity:
        return None
    path = dataset / "state" / "obi" / "config-repo" / path_for_identity(identity)
    return path.read_text(encoding="utf-8") if path.exists() else None


# --- slices the browser filters (research R3) ---------------------------------------------------

AUDIT_STATS_WINDOWS: tuple[str, ...] = ("1h", "24h", "7d", "30d")  # widgets/audit.js options


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def render_slices(
    view: DatasetView, out_root: Path, profile: ExportProfile
) -> dict[str, dict[str, int]]:
    """``data/logs-slice.json`` + ``data/audit-slice.json`` + the empty watch/radio answers."""

    from obi.core import auditstore, logstore
    from obi.core.serialize import log_event_to_dict
    from obi.web import data_api as api

    cfg = view.config
    roots = (str(view.dataset.resolve()) + "/", str(view.dataset) + "/", str(Path.home()))
    out: dict[str, dict[str, int]] = {}
    with dataset_env(view.dataset), api.frozen_now(view.now):
        logs: dict[str, Any] = {"entries": [], "cursor": None, "of": 0}
        if cfg.logs is not None and cfg.logs.db_path.exists():
            conn = logstore.connect_readonly(cfg.logs.db_path)
            try:
                events = logstore.query_events(conn, limit=profile.logs_slice_rows)
                logs["entries"] = [log_event_to_dict(e) for e in reversed(events)]
                logs["cursor"] = logstore.latest_cursor(conn)
                logs["of"] = int(conn.execute("SELECT count(*) FROM logs").fetchone()[0])
            finally:
                conn.close()
        _write(out_root / "data" / "logs-slice.json", _scrub_paths(logs, roots))
        out["logs"] = {"rows": len(logs["entries"]), "of": logs["of"]}

        audit: dict[str, Any] = {
            "events": [], "total": 0, "details": {}, "traces": {}, "stats": {},
            "store": {"present": False, "enabled": False},
        }  # fmt: skip
        db_path = cfg.audit.db_path
        if db_path.exists():
            conn = auditstore.connect_readonly(db_path)
            try:
                rows, total = auditstore.query_events(conn, limit=profile.audit_slice_rows)
                audit["events"], audit["total"] = rows, total
                for row in rows:
                    detail = auditstore.get_event(conn, int(row["id"]))
                    if detail is not None:
                        audit["details"][str(row["id"])] = detail
                for session in sorted({r["session_id"] for r in rows if r.get("session_id")}):
                    audit["traces"][session] = auditstore.session_trace(conn, session)
            finally:
                conn.close()
            audit["store"] = {"present": True, "enabled": bool(cfg.audit.enabled)}
            for since in AUDIT_STATS_WINDOWS:
                _s, stats = capture(
                    api.handle_audit_request,
                    f"/api/audit.json?view=stats&since={since}",
                    cfg.audit,
                )
                audit["stats"][since] = stats
        _write(out_root / "data" / "audit-slice.json", _scrub_paths(audit, roots))
        out["audit"] = {"rows": len(audit["events"]), "of": int(audit["total"])}

        for route, payload in (
            ("watch", capture(api.handle_watch_request, "/api/watch.json",
                              cfg.watch.watch_dir, cfg.logs)[1]),
            ("radio", capture(api.handle_radio_request, "/api/radio.json", cfg.radio)[1]),
        ):  # fmt: skip
            _write(out_root / data_path(route, {}), _scrub_paths(payload, roots))
    return out


# --- the web assets ------------------------------------------------------------------------------

#: The live index.html boot block, and its demo replacement: boot.js sets up the demo, then
#: imports shell.js and calls its boot() itself.
LIVE_BOOT = "import { boot } from './shell.js';\nawait boot();"
DEMO_BOOT = "import './demo/boot.js';"
#: Export artifacts the viewer/map widgets fetch relative to the page (served at the root live).
AR_ARTIFACTS = ("links.json", "terrain.json", "terrain-shade.png", "terrain-shade.meta.json")


def copy_assets(view: DatasetView, out_root: Path) -> int:
    """The live static assets (byte-identical) + the demo boot swap + dataset artifacts."""

    from obi.web.server import STATIC_DIR

    shutil.copytree(
        STATIC_DIR, out_root, dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__", ".DS_Store", "*.map"),
    )  # fmt: skip
    index = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    if index.count(LIVE_BOOT) != 1:
        raise DemoError("web/static/index.html's boot block changed — update the demo export")
    (out_root / "index.html").write_text(index.replace(LIVE_BOOT, DEMO_BOOT), encoding="utf-8")

    ar = view.dataset / "state" / "obi" / "ar"
    copied = 0
    for name in AR_ARTIFACTS:
        if (ar / name).exists():
            shutil.copyfile(ar / name, out_root / name)
            copied += 1
    snapshots = ar / "snapshots"
    if snapshots.is_dir():
        (out_root / "data" / "snapshots").mkdir(parents=True, exist_ok=True)
        for path in sorted(snapshots.glob("*.json")):
            shutil.copyfile(path, out_root / "data" / "snapshots" / path.name)
            copied += 1
    (out_root / ".nojekyll").write_text("", encoding="utf-8")  # Pages: serve every path as-is
    return copied


# --- the build ----------------------------------------------------------------------------------

BUNDLE_FORMAT = 1
_MB = 1024 * 1024


def bundle_digest(root: Path) -> str:
    """sha256 over every file's relative path and bytes, in sorted order (publish integrity)."""

    import hashlib

    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(path.relative_to(root).as_posix().encode() + b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def digest_path() -> Path:
    return _state_root() / "demo" / "bundle-digest.json"


def _record_digest(bundle_dir: Path, digest: str) -> None:
    path = digest_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path.with_suffix(".tmp"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({"bundle_dir": str(bundle_dir.resolve()), "digest": digest}, handle)
    os.replace(path.with_suffix(".tmp"), path)


def static_copies(out_root: Path) -> frozenset[str]:
    """Bundle paths that are byte-identical copies of obi's web source (application code)."""

    import filecmp

    from obi.web.server import STATIC_DIR

    same: set[str] = set()
    for src in STATIC_DIR.rglob("*"):
        if not src.is_file() or "__pycache__" in src.parts:
            continue
        rel = src.relative_to(STATIC_DIR).as_posix()
        dest = out_root / rel
        if dest.is_file() and filecmp.cmp(src, dest, shallow=False):
            same.add(rel)
    return frozenset(same)


@dataclass
class ExportResult:
    state: str
    bundle_dir: Path | None
    counts: dict[str, int] = dataclasses.field(default_factory=dict)
    slices: dict[str, dict[str, int]] = dataclasses.field(default_factory=dict)
    recordings: int = 0
    total_bytes: int = 0
    gate: Any = None
    left_out: list[str] = dataclasses.field(default_factory=list)


def _sizes(root: Path, profile: ExportProfile, allow_oversize: bool) -> int:
    from obi.core.demo import BudgetError

    total = 0
    for path in root.rglob("*"):
        if path.is_file():
            size = path.stat().st_size
            if size > MAX_FILE_BYTES:
                raise BudgetError(
                    f"{path.relative_to(root)} is {size // _MB} MB — over the 95 MB per-file "
                    "limit (GitHub Pages refuses files over 100 MB); lower the slice sizes"
                )
            total += size
    if total > profile.max_bundle_mb * _MB and not allow_oversize:
        raise BudgetError(
            f"bundle is {total // _MB} MB, over the {profile.max_bundle_mb} MB budget; "
            "lower the slice sizes or pass --allow-oversize"
        )
    return total


def _manifest(view: DatasetView, profile: ExportProfile, slices: dict[str, dict[str, int]],
              recordings: int) -> dict[str, Any]:  # fmt: skip
    from obi.core.demo.manifest import _obi_version
    from obi.core.demo.routes import ROUTES

    return {
        "format_version": BUNDLE_FORMAT,
        "generated_by": f"obi {_obi_version()}",
        "dataset": {"format_version": view.manifest.get("format_version"),
                    "window": view.manifest.get("window")},  # fmt: skip
        "demo_now": view.now.isoformat(),
        "base_path_hint": profile.base_path,
        "routes": {name: spec.strategy for name, spec in sorted(ROUTES.items())},
        "slices": slices,
        "recordings": recordings,
        # The snapshots this bundle ships: the shim answers any other /snapshots/* with a 404
        # itself (e.g. netbox-drift, which 134 excludes), without a network request.
        "snapshots": sorted(
            p.name for p in (view.dataset / "state" / "obi" / "ar" / "snapshots").glob("*.json")
        ),
    }


def run_export(
    raw_config: dict[str, Any],
    config_path: Path,
    profile: ExportProfile,
    *,
    allow_oversize: bool = False,
    audit: Any = None,
    recordings: Callable[[DatasetView, Path], tuple[int, list[str]]] | None = None,
    post_build_hook: Callable[[Path], None] | None = None,
) -> ExportResult:
    """Build, gate and publish (rename) one bundle — data-model build states."""

    from obi.core.demo import DemoError, LeakGateError
    from obi.core.demo.gate import LeakGate, format_report
    from obi.core.demo.profile import load_profile
    from obi.core.demo.snapshot import prepare, public_values_of

    out = profile.bundle_dir
    if out.exists():
        raise ProfileError(f"bundle {out} already exists; remove it first (never overwritten)")
    view = load_dataset(profile.dataset)
    for stale in out.parent.glob(f"{out.name}.staging-*"):
        shutil.rmtree(stale, ignore_errors=True)
    staging = out.parent / f"{out.name}.staging-{os.getpid()}"
    state = "init"
    result = ExportResult(state=state, bundle_dir=None)
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        staging.mkdir(mode=0o700)
        scratch = staging / ".scratch"
        scratch.mkdir()
        result.counts = render_routes(view, staging, scratch)
        state = "rendered"
        result.slices = render_slices(view, staging, profile)
        state = "sliced"
        if recordings is not None:
            result.recordings, result.left_out = recordings(view, staging)
        if not (staging / "data" / "chat" / "index.json").exists():
            _write(staging / "data" / "chat" / "index.json", {"recordings": []})
        state = "recordings"
        copy_assets(view, staging)
        shutil.rmtree(scratch)
        _write(staging / "data" / "manifest.json",
               _manifest(view, profile, result.slices, result.recordings))  # fmt: skip
        state = "assets"
        if post_build_hook is not None:
            post_build_hook(staging)
        result.total_bytes = _sizes(staging, profile, allow_oversize)
        state = "budget_ok"

        demo_profile = load_profile(
            raw_config, config_path=config_path, overrides={"output_dir": str(profile.dataset)}
        )
        _window, harvest, _mapping, _shift, _reloc = prepare(raw_config, demo_profile)
        exempt = public_values_of(profile.dataset) | _recording_tool_names(staging)
        gate = LeakGate(harvest, exempt_values=frozenset(exempt))
        skip = static_copies(staging) | {"data/manifest.json"}
        result.gate = gate.scan(staging, skip=frozenset(skip))
        if result.gate.fatal:
            raise LeakGateError(format_report(result.gate))
        state = "gate_passed"
        digest = bundle_digest(staging)
        os.rename(staging, out)
        state = "published"
        _record_digest(out, digest)
        _audit_export(audit, "ok", state, result)
        result.state, result.bundle_dir = "audited", out
        return result
    except DemoError as exc:
        shutil.rmtree(staging, ignore_errors=True)
        _audit_export(audit, "error", f"failed:{state}", result, type(exc).__name__)
        raise
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        _audit_export(audit, "error", f"failed:{state}", result, "unexpected error")
        raise


def _recording_tool_names(staging: Path) -> frozenset[str]:
    """obi's own tool names in the shipped recordings: code vocabulary (``inventory_list``), exempt
    as whole values only — tool arguments and results are gated like everything else."""

    from obi.core.demo.chats import _TOOL_NAME

    names: set[str] = set()
    for path in (staging / "data" / "chat").glob("rec-*.json"):
        recording = json.loads(path.read_text(encoding="utf-8"))
        for turn in recording.get("turns", []):
            names |= {str(s.get("tool", "")) for s in turn.get("steps", [])}
            names |= {str(t.get("name", "")) for t in (turn.get("report") or {}).get("tools", [])}
    return frozenset(n for n in names if _TOOL_NAME.match(n))


def _audit_export(
    audit: Any, outcome: str, state: str, result: ExportResult, detail: str | None = None
) -> None:
    if audit is None:
        return
    from datetime import UTC

    from obi.core.models import AuditOutcome, AuditRecord

    audit.record(
        AuditRecord(
            timestamp=datetime.now(UTC),
            action="demo_export",
            target="demo-bundle",
            reason="operator-initiated static demo export (feature 136)",
            outcome=AuditOutcome.OK if outcome == "ok" else AuditOutcome.ERROR,
            params={
                "state": state,
                "routes": result.counts,
                "slices": result.slices,
                "recordings": result.recordings,
                "total_bytes": result.total_bytes,
            },
            detail=detail,
        )
    )
