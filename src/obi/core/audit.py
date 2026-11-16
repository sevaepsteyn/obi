"""obi's audit trail (Constitution Principle V).

Two complementary halves live here:

- The original **append-only JSONL intent log** (:class:`AuditTrail`): one structured JSON
  object per action — what was queried, against which device, why, and the outcome. Durable
  across restarts (append mode + flush); credentials MUST NOT appear in any field.
- The feature-042 **audit event recorder** (:class:`AuditRecorder` + the ambient-scope
  helpers): full-payload capture of every LLM call, model tool call, device/vendor command,
  human turn, and report delivery into the queryable SQLite store
  (:mod:`obi.core.auditstore`), grouped by session.

Capture is *ambient*: an entrypoint opens :func:`audit_scope` (source + session id) and every
instrumented seam below it records through :func:`record_event` — which is a silent no-op
when no scope is active. That one property gives fail-open capture (no recorder → nothing to
fail), keeps instrumented code paths unconditional, and exempts the read-only inspection
surfaces (``obi audit``, the web board) from ever auditing themselves. Recording never raises
and never blocks the operation it observes: a store failure increments a counter, logs one
warning per failure streak, and drops the event.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from obi.core.models import AuditRecord

if TYPE_CHECKING:
    import sqlite3

    from obi.core.config import AuditSettings, Config

logger = logging.getLogger("obi")

_REDACT_KEYS = frozenset({"password", "secret", "token", "credential", "credentials", "passwd"})
_REDACTED = "***"


def _redact(value: Any) -> Any:
    """Recursively replace values under credential-like keys with ``***``."""

    if isinstance(value, Mapping):
        return {
            key: (_REDACTED if str(key).lower() in _REDACT_KEYS else _redact(val))
            for key, val in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    return value


class AuditTrail:
    """Durable, append-only JSONL audit writer."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # feature 009: serialize concurrent appends so the Slack listener's worker pool cannot
        # interleave partial lines. A no-op for the single-threaded CLI.
        self._lock = threading.Lock()

    @staticmethod
    def _serialize(record: AuditRecord) -> dict[str, Any]:
        data: dict[str, Any] = {
            "timestamp": record.timestamp.isoformat(),
            "action": record.action,
            "target": record.target,
            "reason": record.reason,
            "outcome": record.outcome.value,
            "params": _redact(dict(record.params)),
            "detail": record.detail,
        }
        # feature 009: attribute the action to a Slack operator when present. Omitted for every
        # CLI/core record so their serialized shape is byte-for-byte unchanged.
        if record.actor is not None:
            data["actor"] = record.actor
        return data

    def record(self, record: AuditRecord) -> None:
        """Append one record as a single JSON line, flushing to disk immediately.

        Feature 042: each record is additionally teed into the audit event store as an
        ``action`` event when an ambient :func:`audit_scope` is active — so every existing
        engine/tool intent record lands in the queryable trail with zero per-site changes.
        """

        data = self._serialize(record)
        line = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
        with self._lock, self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
        record_event(
            "action",
            summary=f"{record.action} → {record.target}",
            payload={
                "action": record.action,
                "reason": record.reason,
                "params": data["params"],  # post-_redact
                "detail": record.detail,
                "actor": record.actor,
            },
            target=record.target,
            outcome="error" if record.outcome.value == "error" else "ok",
            error=record.detail if record.outcome.value == "error" else None,
        )


# --- feature 042: the audit event recorder + ambient capture scope -----------------------------

# The placeholder the config-backup store uses (feature 026) — one convention everywhere.
_REDACTED_VALUE = "<redacted-by-obi>"
# Secrets shorter than this are not value-scrubbed (a 1-2 char "secret" would shred payloads).
_MIN_SECRET_LEN = 4


class AuditRecorder:
    """Write API for the audit event store. Never raises; never blocks the audited operation.

    Holds one lazily opened WAL connection (shared across the Slack worker threads behind
    ``_lock``), applies the known-secret value backstop and the per-field payload bound to
    every event, and prunes the store to the retention age once per process.
    """

    def __init__(
        self,
        settings: AuditSettings,
        known_secrets: Sequence[str] = (),
    ) -> None:
        self.settings = settings
        self._secrets = tuple(
            s for s in known_secrets if isinstance(s, str) and len(s) >= _MIN_SECRET_LEN
        )
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._pruned = False
        self._failures = 0
        self._warned = False

    @property
    def failures(self) -> int:
        """Events dropped by this process because the store could not be written."""

        return self._failures

    # -- internals --------------------------------------------------------------------------

    def _ensure_conn(self) -> sqlite3.Connection:
        from obi.core import auditstore

        if self._conn is None:
            self._conn = auditstore.connect(self.settings.db_path, check_same_thread=False)
        if not self._pruned:
            self._pruned = True
            cutoff = datetime.now(UTC) - timedelta(days=self.settings.retention_days)
            deleted = auditstore.prune(self._conn, older_than=cutoff)
            if deleted:
                logger.info("audit store pruned", extra={"deleted": deleted})
        return self._conn

    def _scrub(self, value: Any) -> Any:
        """Value backstop: replace every known secret literal anywhere in the payload."""

        if isinstance(value, str):
            for secret in self._secrets:
                if secret in value:
                    value = value.replace(secret, _REDACTED_VALUE)
            return value
        if isinstance(value, Mapping):
            return {str(k): self._scrub(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._scrub(item) for item in value]
        return value

    def _bound(self, payload: dict[str, Any]) -> tuple[dict[str, Any], bool, int | None]:
        """Bound each payload field to ``max_payload_bytes`` with an explicit marker."""

        limit = self.settings.max_payload_bytes
        original = len(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"))
        truncated = False
        bounded: dict[str, Any] = {}
        for key, value in payload.items():
            text = value if isinstance(value, str) else None
            if text is None:
                rendered = json.dumps(value, ensure_ascii=False, default=str)
                if len(rendered.encode("utf-8")) <= limit:
                    bounded[key] = value
                    continue
                text = rendered
            raw = text.encode("utf-8")
            if len(raw) <= limit:
                bounded[key] = text if isinstance(value, str) else value
                continue
            truncated = True
            clipped = raw[:limit].decode("utf-8", errors="ignore")
            bounded[key] = (
                f"{clipped}… [truncated by obi audit: {limit} of {len(raw)} bytes]"
            )
        return bounded, truncated, (original if truncated else None)

    # -- the one write entrypoint -------------------------------------------------------------

    def record(
        self,
        kind: str,
        *,
        source: str,
        session_id: str | None,
        summary: str,
        payload: dict[str, Any],
        target: str | None = None,
        duration_ms: float | None = None,
        outcome: str = "ok",
        error: str | None = None,
    ) -> None:
        """Insert one event; on any failure, count + log once per streak and drop it (FR-007)."""

        from obi.core import auditstore

        try:
            scrubbed = self._scrub(dict(payload))
            bounded, truncated, original = self._bound(scrubbed)
            clean_error = self._scrub(error) if error is not None else None
            with self._lock:
                conn = self._ensure_conn()
                auditstore.insert_event(
                    conn,
                    ts=datetime.now(UTC),
                    kind=kind,
                    source=source,
                    session_id=session_id,
                    target=target,
                    summary=str(self._scrub(summary)),
                    payload=bounded,
                    duration_ms=duration_ms,
                    outcome=outcome,
                    error=clean_error,
                    truncated=truncated,
                    original_bytes=original,
                )
            self._warned = False
        except Exception as exc:  # noqa: BLE001 - recording must never break the audited op
            self._failures += 1
            if not self._warned:
                self._warned = True
                logger.warning(
                    "audit event dropped (store unavailable)",
                    extra={"error": str(exc), "failures": self._failures},
                )

    def close(self) -> None:
        """Close the store connection (best-effort; safe to call more than once)."""

        with self._lock:
            if self._conn is not None:
                with contextlib.suppress(Exception):
                    self._conn.close()
                self._conn = None


class _Scope:
    """The ambient capture context: which recorder, subsystem, and session events belong to."""

    __slots__ = ("recorder", "source", "session_id")

    def __init__(self, recorder: AuditRecorder, source: str, session_id: str | None) -> None:
        self.recorder = recorder
        self.source = source
        self.session_id = session_id


_scope_var: contextvars.ContextVar[_Scope | None] = contextvars.ContextVar(
    "obi_audit_scope", default=None
)


@contextmanager
def audit_scope(
    recorder: AuditRecorder | None, source: str, session_id: str | None
) -> Iterator[None]:
    """Make ``recorder`` ambient for the calling context (thread/task) until exit.

    A ``None`` recorder (capture disabled) opens nothing, so call sites stay unconditional.
    Contextvars are per-thread: the Slack handler opens its scope inside the worker thread.
    """

    if recorder is None:
        yield
        return
    token = _scope_var.set(_Scope(recorder, source, session_id))
    try:
        yield
    finally:
        _scope_var.reset(token)


def current_scope() -> tuple[AuditRecorder, str, str | None] | None:
    """The ambient (recorder, source, session_id), or ``None`` outside any scope."""

    scope = _scope_var.get()
    if scope is None:
        return None
    return scope.recorder, scope.source, scope.session_id


def current_session_id() -> str | None:
    """The ambient session id, or ``None`` outside any scope (for cross-referencing surfaces)."""

    scope = _scope_var.get()
    return scope.session_id if scope is not None else None


def record_event(
    kind: str,
    *,
    summary: str,
    payload: dict[str, Any],
    target: str | None = None,
    duration_ms: float | None = None,
    outcome: str = "ok",
    error: str | None = None,
) -> None:
    """Record one event on the ambient recorder; a silent no-op when no scope is active."""

    scope = _scope_var.get()
    if scope is None:
        return
    scope.recorder.record(
        kind,
        source=scope.source,
        session_id=scope.session_id,
        summary=summary,
        payload=payload,
        target=target,
        duration_ms=duration_ms,
        outcome=outcome,
        error=error,
    )


def record_device_command(
    target: str,
    transport: str,
    operation: str,
    *,
    response_excerpt: str | None = None,
    response_bytes: int | None = None,
    response_items: int | None = None,
    duration_ms: float | None = None,
    outcome: str = "ok",
    error: str | None = None,
) -> None:
    """Record one device/vendor command (FR-003); a no-op outside any capture scope.

    Called from the driver transport seams — one line per adapter, inside each vendor's
    quarantine (Constitution IV). ``operation`` is the exact command string / API path /
    ``METHOD path`` / SNMP op; the response is carried as a bounded excerpt or as sizes.
    """

    payload: dict[str, Any] = {"transport": transport, "operation": operation}
    if response_excerpt is not None:
        payload["response_excerpt"] = response_excerpt
    if response_bytes is not None:
        payload["response_bytes"] = response_bytes
    if response_items is not None:
        payload["response_items"] = response_items
    record_event(
        "device_command",
        summary=f"[{transport}] {target}: {operation}",
        payload=payload,
        target=target,
        duration_ms=duration_ms,
        outcome=outcome,
        error=error,
    )


@contextmanager
def timed_device_command(target: str, transport: str, operation: str) -> Iterator[None]:
    """Record one device command around a transport call, success or raise (then re-raise)."""

    start = time.monotonic()
    try:
        yield
    except Exception as exc:
        record_device_command(
            target,
            transport,
            operation,
            duration_ms=(time.monotonic() - start) * 1000.0,
            outcome="error",
            error=str(exc),
        )
        raise
    record_device_command(
        target, transport, operation, duration_ms=(time.monotonic() - start) * 1000.0
    )


def recorder_from_config(config: Config) -> AuditRecorder | None:
    """Build the process's recorder from ``[audit]``; ``None`` when capture is disabled."""

    if not config.audit.enabled:
        return None
    return AuditRecorder(config.audit, known_secrets=_all_known_secrets(config))


def _all_known_secrets(config: Config) -> tuple[str, ...]:
    """Every secret obi's config knows, for the audit store's value backstop (FR-008).

    Broader than the config-export set (``engine._known_secrets``): audit payloads carry LLM
    prompts, tool results, and vendor responses, so *any* configured credential — API keys and
    controller tokens included — must never survive into the store.
    """

    secrets: list[str] = []
    if config.routeros is not None:
        secrets.append(config.routeros.password.reveal())
    if config.uisp is not None:
        secrets.append(config.uisp.token.reveal())
    if config.netbox is not None:
        secrets.append(config.netbox.token.reveal())
    if config.unifi_network is not None:
        for maybe in (config.unifi_network.api_key, config.unifi_network.token):
            if maybe is not None:
                secrets.append(maybe.reveal())
    if config.unifi_sm is not None:
        secrets.append(config.unifi_sm.api_key.reveal())
    if config.unifi_internal is not None:
        secrets.append(config.unifi_internal.username.reveal())
        secrets.append(config.unifi_internal.password.reveal())
    if config.siklu is not None:
        secrets.append(config.siklu.password.reveal())
    if config.snmp is not None:
        for maybe in (config.snmp.community, config.snmp.v3_auth_key, config.snmp.v3_priv_key):
            if maybe is not None:
                secrets.append(maybe.reveal())
        for override in getattr(config.snmp, "overrides", {}).values():
            community = getattr(override, "community", None)
            if community is not None:
                secrets.append(community.reveal())
    if config.slack is not None:
        secrets.append(config.slack.bot_token.reveal())
        secrets.append(config.slack.app_token.reveal())
    if config.chat.api_key is not None:
        secrets.append(config.chat.api_key.reveal())
    if config.configs is not None:
        secrets.append(config.configs.password.reveal())
    # 134 (analyze review): the PlayaPage credential was the one configured secret missing here.
    if config.paging is not None:
        secrets.append(config.paging.password.reveal())
    seen: set[str] = set()
    deduped: list[str] = []
    for secret in secrets:
        if secret and secret not in seen:
            seen.add(secret)
            deduped.append(secret)
    return tuple(deduped)
