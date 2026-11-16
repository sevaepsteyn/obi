"""launchd-backed supervision of obi's always-on daemons (feature 081).

obi has two always-on processes — the Slack listener (``obi slack run``) and the web server
(``obi web serve``). This module is the supervision engine behind the per-daemon lifecycle verbs
(``start`` / ``stop`` / ``restart`` / ``status`` / ``logs``): it renders a per-user launchd
LaunchAgent plist, drives ``launchctl`` in the ``gui/<uid>`` domain, parses ``launchctl print`` for
status, probes real liveness (a ``/healthz`` GET for the web server, a heartbeat file for the
listener), and reads the cron collectors' freshness. All logic lives here; the CLI verbs are thin
adapters (Constitution Principle III).

macOS-only at the ``launchctl`` boundary; the pure helpers (plist render, status parse, heartbeat
interpretation, collector freshness, the worktree guard) are platform-independent and unit-tested
without a Mac.
"""

from __future__ import annotations

import contextlib
import json
import os
import plistlib
import ssl
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from enum import Enum
from http.client import HTTPSConnection
from pathlib import Path

from obi.core.config import default_service_dir

LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"

THROTTLE_INTERVAL = 10  # seconds; bounds launchd restart frequency so a bad config can't crash-loop
HEARTBEAT_INTERVAL = 15.0  # seconds between listener heartbeats while connected
HEARTBEAT_STALE_AFTER = HEARTBEAT_INTERVAL * 3  # 45 s: alive-but-no-beat ⇒ "possibly wedged"

# The launchd PATH for supervised daemons: the venv bin (so `obi` resolves its own siblings) plus
# the system dirs `obi web serve` needs to shell out to `openssl` for first-run cert generation.
_SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"

# The project develops features in worktrees under ~/src/obi-worktrees (docs/development/worktrees).
# A supervised daemon must never point at one (deleted after the feature merges) — see FR-007.
WORKTREE_MARKER = "/obi-worktrees/"


class ServiceError(Exception):
    """A daemon-management operation failed (wrong platform, launchctl error, guard tripped)."""


class ServiceKind(Enum):
    SLACK = "slack"
    WEB = "web"
    RADIO = "radio"


def _state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(Path.home(), ".local", "state")
    return Path(base) / "obi"


@dataclass(frozen=True)
class ServiceSpec:
    """The static description of one supervised daemon (see specs/081 data-model)."""

    name: str  # "slack" / "web" — the CLI namespace
    label: str  # "com.obi.slack" / "com.obi.web" — launchd label + plist basename
    verb: tuple[str, ...]  # foreground args appended to the obi exec, e.g. ("slack", "run")
    log_path: Path  # StandardOut/ErrPath — the existing daemon log
    kind: ServiceKind

    @property
    def plist_path(self) -> Path:
        return LAUNCH_AGENTS_DIR / f"{self.label}.plist"

    @property
    def heartbeat_path(self) -> Path:
        return default_service_dir() / f"{self.name}.heartbeat.json"


def slack_spec() -> ServiceSpec:
    return ServiceSpec(
        name="slack",
        label="com.obi.slack",
        verb=("slack", "run"),
        log_path=_state_dir() / "slack.log",
        kind=ServiceKind.SLACK,
    )


def web_spec() -> ServiceSpec:
    return ServiceSpec(
        name="web",
        label="com.obi.web",
        verb=("web", "serve"),
        log_path=_state_dir() / "web-serve.log",
        kind=ServiceKind.WEB,
    )


def radio_spec() -> ServiceSpec:
    # feature 088: the walkie-talkie station daemon. Continuous audio capture + on-device
    # transcription — the ProcessType Standard rule below is load-bearing for it (darwin-bg
    # would starve the capture pipeline exactly like the 13s-vs-1.4s git reads).
    return ServiceSpec(
        name="radio",
        label="com.obi.radio",
        verb=("radio", "run"),
        log_path=_state_dir() / "radio.log",
        kind=ServiceKind.RADIO,
    )


def spec_for(name: str) -> ServiceSpec:
    if name == "slack":
        return slack_spec()
    if name == "web":
        return web_spec()
    if name == "radio":
        return radio_spec()
    raise ServiceError(f"unknown service {name!r}")  # pragma: no cover — CLI passes a known name


def all_specs() -> list[ServiceSpec]:
    return [slack_spec(), web_spec(), radio_spec()]


# --------------------------------------------------------------------------------------------------
# Platform + primary-checkout guard
# --------------------------------------------------------------------------------------------------


def is_macos() -> bool:
    return sys.platform == "darwin"


def require_macos() -> None:
    if not is_macos():
        raise ServiceError(
            "daemon supervision uses macOS launchd and is only available on macOS "
            "(`run` and the collector panel still work here)."
        )


def resolve_obi_exec() -> Path:
    """The obi executable to supervise — the sibling ``obi`` of the running interpreter.

    launchd needs an absolute program path; ``sys.executable`` is ``…/.venv/bin/python3``, whose
    sibling ``obi`` is the console-script shim installed by the same venv. We deliberately do NOT
    ``.resolve()`` it: the venv's ``python3`` is a symlink to the real (uv-managed) interpreter,
    and resolving would point at ``…/uv/python/.../bin`` — which has no ``obi`` shim, so launchd
    would fail to exec it (``EX_CONFIG``). The venv ``bin`` directory is the correct, stable home.
    """

    return Path(sys.executable).parent / "obi"


def assert_primary_checkout(exec_path: Path) -> None:
    """Refuse an obi exec inside a feature worktree — daemons run from the primary checkout."""

    if WORKTREE_MARKER in str(exec_path.resolve()):
        raise ServiceError(
            f"{exec_path} is inside a feature worktree — a supervised daemon must run from the "
            "primary checkout (~/src/obi). Run this from the primary checkout, or pass "
            "--exec <path-to-primary-obi> if you really mean it."
        )


def primary_workdir(exec_path: Path) -> Path:
    """A stable ``WorkingDirectory`` for the plist: the repo root above the venv (else parent)."""

    resolved = exec_path.resolve()
    for parent in resolved.parents:
        if (parent / "pyproject.toml").exists():
            return parent
    return resolved.parent


# --------------------------------------------------------------------------------------------------
# Plist rendering
# --------------------------------------------------------------------------------------------------


def render_plist(
    spec: ServiceSpec,
    *,
    obi_exec: Path,
    run_at_login: bool,
    environment: dict[str, str],
    workdir: Path,
) -> bytes:
    """The LaunchAgent plist for one daemon. Returns XML bytes.

    Carries NO secret — the daemon reads ``~/.config/obi/config.toml`` at runtime. PATH includes the
    venv bin (so ``obi`` resolves its own siblings) and ``/usr/bin`` (so ``obi web serve`` can shell
    out to ``openssl``). ``KeepAlive`` + ``ThrottleInterval`` give crash-restart without a
    crash-loop; ``RunAtLoad`` is the start-at-login policy.
    """

    env: dict[str, str] = {"PATH": f"{obi_exec.resolve().parent}:{_SYSTEM_PATH}"}
    env.update(environment)  # operator escape hatch wins (e.g. an env-based API key)
    plist: dict[str, object] = {
        "Label": spec.label,
        "ProgramArguments": [str(obi_exec), *spec.verb],
        "RunAtLoad": run_at_login,
        "KeepAlive": True,
        "ThrottleInterval": THROTTLE_INTERVAL,
        "StandardOutPath": str(spec.log_path),
        "StandardErrorPath": str(spec.log_path),
        "WorkingDirectory": str(workdir),
        # Standard, NOT "Background": Background is darwin-bg — E-core scheduling + throttled
        # disk I/O for the service AND every subprocess it spawns. Live 2026-08-19: the CFG
        # board's 33 config-repo git reads took 13 s under the throttle vs 1.4 s at Standard
        # (the operator felt it as "the conf widget takes many seconds"); voice/TTS and every
        # other server-side read paid the same tax. These are interactive services.
        "ProcessType": "Standard",
        "EnvironmentVariables": env,
    }
    return plistlib.dumps(plist)


# --------------------------------------------------------------------------------------------------
# launchctl wrappers (gui/<uid> domain)
# --------------------------------------------------------------------------------------------------


def _domain_target() -> str:
    return f"gui/{os.getuid()}"


def _service_target(spec: ServiceSpec) -> str:
    return f"gui/{os.getuid()}/{spec.label}"


def _launchctl(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(["launchctl", *args], capture_output=True, text=True)
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise ServiceError(f"launchctl {' '.join(args)} failed ({proc.returncode}): {detail}")
    return proc


def bootstrap(spec: ServiceSpec) -> None:
    require_macos()
    _launchctl("bootstrap", _domain_target(), str(spec.plist_path))


def enable(spec: ServiceSpec) -> None:
    require_macos()
    _launchctl("enable", _service_target(spec))


def kickstart(spec: ServiceSpec, *, restart: bool = False) -> None:
    require_macos()
    args = ["kickstart"]
    if restart:
        args.append("-k")
    args.append(_service_target(spec))
    _launchctl(*args)


def bootout(spec: ServiceSpec, *, missing_ok: bool = True) -> None:
    """Unload the job (this is how `stop` defeats KeepAlive). A not-loaded job is a benign no-op."""

    require_macos()
    proc = _launchctl("bootout", _service_target(spec), check=False)
    if proc.returncode != 0 and not missing_ok:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise ServiceError(f"launchctl bootout failed ({proc.returncode}): {detail}")


def print_service(spec: ServiceSpec) -> str | None:
    """Raw ``launchctl print`` text, or ``None`` when the job is not loaded (not an error)."""

    require_macos()
    proc = _launchctl("print", _service_target(spec), check=False)
    if proc.returncode != 0:
        return None
    return proc.stdout


# --------------------------------------------------------------------------------------------------
# Status
# --------------------------------------------------------------------------------------------------


@dataclass
class LaunchdState:
    installed: bool  # the plist file exists on disk
    loaded: bool  # launchd knows the job
    pid: int | None  # running PID, if any
    last_exit: int | None  # last exit code (non-zero after a crash)

    @property
    def running(self) -> bool:
        return self.pid is not None


def _int_or_none(text: str) -> int | None:
    try:
        return int(text.strip())
    except ValueError:
        return None


def parse_launchd_state(text: str, *, installed: bool) -> LaunchdState:
    """Scrape ``launchctl print`` for pid / last-exit. Lenient: missing keys ⇒ ``None``."""

    pid: int | None = None
    last_exit: int | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("pid = "):
            pid = _int_or_none(line.split("=", 1)[1])
        elif line.startswith("last exit code = "):
            last_exit = _int_or_none(line.split("=", 1)[1])
    return LaunchdState(installed=installed, loaded=True, pid=pid, last_exit=last_exit)


def service_state(spec: ServiceSpec) -> LaunchdState:
    installed = spec.plist_path.exists()
    if not is_macos():
        return LaunchdState(installed=installed, loaded=False, pid=None, last_exit=None)
    text = print_service(spec)
    if text is None:
        return LaunchdState(installed=installed, loaded=False, pid=None, last_exit=None)
    return parse_launchd_state(text, installed=installed)


# --------------------------------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------------------------------


def install(
    spec: ServiceSpec,
    *,
    obi_exec: Path,
    run_at_login: bool,
    environment: dict[str, str],
    workdir: Path,
) -> LaunchdState:
    """Write the plist and (re)load it: bootout any stale copy, bootstrap, enable, kickstart."""

    require_macos()
    spec.plist_path.parent.mkdir(parents=True, exist_ok=True)
    spec.log_path.parent.mkdir(parents=True, exist_ok=True)
    spec.plist_path.write_bytes(
        render_plist(
            spec,
            obi_exec=obi_exec,
            run_at_login=run_at_login,
            environment=environment,
            workdir=workdir,
        )
    )
    # Idempotent (re)load: drop any prior instance so the freshly-written plist takes effect, then
    # bootstrap + enable + kickstart (kickstart starts it now even when RunAtLoad is off).
    bootout(spec, missing_ok=True)
    bootstrap(spec)
    enable(spec)
    kickstart(spec)
    return service_state(spec)


def uninstall(spec: ServiceSpec) -> None:
    """Stop the daemon and keep it stopped (bootout defeats KeepAlive). Plist left in place."""

    require_macos()
    bootout(spec, missing_ok=True)


def restart(spec: ServiceSpec) -> LaunchdState:
    require_macos()
    if print_service(spec) is None:
        raise ServiceError(f"{spec.label} is not loaded — run `obi {spec.name} start` first.")
    kickstart(spec, restart=True)
    return service_state(spec)


def detect_stray_listener() -> list[int]:
    """Best-effort PIDs of ``obi slack`` processes (excluding this one), via ``pgrep``.

    Used to warn before ``slack start`` (run before install, so any match is a hand-started
    listener that would double every answer). Any error yields ``[]`` — the warning is advisory.
    """

    try:
        proc = subprocess.run(["pgrep", "-f", "obi slack"], capture_output=True, text=True)
    except OSError:
        return []
    if proc.returncode != 0:
        return []
    self_pid = os.getpid()
    pids: list[int] = []
    for token in proc.stdout.split():
        candidate = _int_or_none(token)
        if candidate is not None and candidate != self_pid:
            pids.append(candidate)
    return pids


# --------------------------------------------------------------------------------------------------
# Liveness — web /healthz probe
# --------------------------------------------------------------------------------------------------


@dataclass
class WebLiveness:
    reachable: bool
    status: int | None

    @property
    def healthy(self) -> bool:
        return self.reachable and self.status == 200


def probe_web(bind: str, port: int, *, timeout: float = 2.0) -> WebLiveness:
    """``GET /healthz`` over HTTPS to the local server; self-signed cert ⇒ verification disabled."""

    host = "127.0.0.1" if bind in ("", "0.0.0.0") else bind
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    conn = HTTPSConnection(host, port, timeout=timeout, context=ctx)
    try:
        conn.request("GET", "/healthz")
        resp = conn.getresponse()
        resp.read()
        return WebLiveness(reachable=True, status=resp.status)
    except (OSError, ssl.SSLError, TimeoutError):
        return WebLiveness(reachable=False, status=None)
    finally:
        conn.close()


# --------------------------------------------------------------------------------------------------
# Liveness — slack heartbeat
# --------------------------------------------------------------------------------------------------


@dataclass
class HeartbeatRecord:
    name: str
    pid: int
    connected: bool
    ts: float


class Heartbeat:
    """The listener's liveness beacon: an atomically-written JSON file refreshed while connected.

    ``obi slack status`` reads it to tell "actually connected" from "process alive but wedged",
    independent of DM traffic. Optional in the listener (absent ⇒ no file; tests unaffected).
    """

    def __init__(self, path: Path, *, name: str, interval: float = HEARTBEAT_INTERVAL) -> None:
        self._path = path
        self._name = name
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _write(self, *, connected: bool) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "name": self._name,
            "pid": os.getpid(),
            "connected": connected,
            "ts": time.time(),
        }
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(json.dumps(record), encoding="utf-8")
        os.replace(tmp, self._path)  # atomic — status never reads a torn file

    def start(self) -> None:
        self._write(connected=True)
        self._thread = threading.Thread(target=self._loop, name="obi-heartbeat", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            # a transient filesystem error must never kill the listener
            with contextlib.suppress(OSError):
                self._write(connected=True)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        with contextlib.suppress(OSError):
            self._write(connected=False)


def read_heartbeat(path: Path) -> HeartbeatRecord | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    try:
        return HeartbeatRecord(
            name=str(data["name"]),
            pid=int(data["pid"]),
            connected=bool(data["connected"]),
            ts=float(data["ts"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


@dataclass
class SlackLiveness:
    state: str  # "connected" | "wedged" | "disconnected" | "unknown"
    age_s: float | None


def interpret_heartbeat(
    record: HeartbeatRecord | None,
    *,
    now: float,
    stale_after: float = HEARTBEAT_STALE_AFTER,
) -> SlackLiveness:
    if record is None:
        return SlackLiveness(state="unknown", age_s=None)
    age = max(0.0, now - record.ts)
    if not record.connected:
        return SlackLiveness(state="disconnected", age_s=age)
    if age > stale_after:
        return SlackLiveness(state="wedged", age_s=age)
    return SlackLiveness(state="connected", age_s=age)


# --------------------------------------------------------------------------------------------------
# Collector freshness (read-only view over the cron jobs)
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class CollectorSpec:
    name: str
    rel_path: str  # relative to the obi state dir
    cadence_s: int


# The eleven documented cron collectors (README / crontab). Freshness = age of the log's last write.
COLLECTORS: tuple[CollectorSpec, ...] = (
    CollectorSpec("logs-sync", "logs-sync.log", 300),
    CollectorSpec("metrics", "metrics.log", 300),
    CollectorSpec("configs", "configs.log", 300),
    CollectorSpec("unifi-logs", "unifi-logs.log", 300),
    CollectorSpec("uisp-logs", "uisp-logs.log", 300),
    # 095: the dash minute cycle ABSORBS the icmp cron (same sweep in-process, same store);
    # the old `1-59/2 obi icmp run` crontab line is retired at enablement (quickstart §3).
    CollectorSpec("dash", "dash.log", 60),
    CollectorSpec("watch", "watch/cron.log", 3600),
    CollectorSpec("web-export", "web-export.log", 300),
    CollectorSpec("web-snapshot", "web-snapshot.log", 300),
    # 110: the hourly BRC weather snapshot (`17 * * * * obi weather run`); an internet fetch
    # on a flaky uplink goes stale routinely — this tile is where that shows (no watch pass).
    CollectorSpec("weather", "weather.log", 3600),
    # 112: the 5-minute BRC gate-traffic poll (`*/5 * * * * obi traffic run`); pre-event the
    # feed answers 403 — that is a recorded outcome, so the tile watches the CRON's liveness,
    # not the dashboard's willingness to publish (no watch pass).
    CollectorSpec("traffic", "traffic.log", 300),
)


@dataclass
class CollectorHealth:
    name: str
    age_s: float | None  # None ⇒ never run (log missing)
    cadence_s: int
    stale: bool


def collector_health(
    state_dir: Path | None = None, *, now: float | None = None
) -> list[CollectorHealth]:
    """Read-only freshness of the cron collectors from their log mtimes. Never runs a collector."""

    base = state_dir if state_dir is not None else _state_dir()
    clock = now if now is not None else time.time()
    out: list[CollectorHealth] = []
    for c in COLLECTORS:
        try:
            mtime = (base / c.rel_path).stat().st_mtime
        except OSError:
            out.append(CollectorHealth(c.name, None, c.cadence_s, stale=True))
            continue
        age = max(0.0, clock - mtime)
        # Generous: tolerate one skipped tick (3× cadence) plus a minute of slack.
        stale = age > c.cadence_s * 3 + 60
        out.append(CollectorHealth(c.name, age, c.cadence_s, stale=stale))
    return out
