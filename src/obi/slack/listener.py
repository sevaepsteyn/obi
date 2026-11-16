"""The long-lived Socket Mode listener for the Slack interface (feature 009).

Connects **outbound** to Slack via Socket Mode (an app-token-authenticated WebSocket — no inbound
endpoint, works behind NAT/field firewalls), acks each event, and dispatches it to a bounded
worker pool running the :class:`~obi.slack.handler.SlackHandler`. ``slack_sdk`` is imported lazily
here so importing obi's core/CLI never requires it and the offline core keeps working if it is
absent. Ctrl-C / SIGTERM stop the listener cleanly (the kill switch, FR-013).
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

from obi.chat.session import ChatRuntime
from obi.core.audit import recorder_from_config
from obi.core.config import Config
from obi.slack.handler import SlackHandler

if TYPE_CHECKING:
    from obi.core.service import Heartbeat

logger = logging.getLogger("obi")

# Bound concurrent answers so one slow live-read/model call can't block others, while capping
# concurrent device load and model spend (research §7).
MAX_WORKERS = 4


def _extract_event(payload: Any) -> dict[str, Any] | None:
    """The user event inside a Socket Mode ``events_api`` payload, or ``None``.

    Only live events delivered while connected are ever dispatched — the listener keeps no event
    buffer, so a reconnect replays nothing (FR-017). Control frames (``hello``/``disconnect``)
    are not ``events_api`` and never reach here.
    """

    if not isinstance(payload, dict):
        return None
    event = payload.get("event")
    return event if isinstance(event, dict) else None


class _WebClientPoster:
    """Adapts ``slack_sdk``'s ``WebClient`` to the handler's :class:`SlackPoster` seam."""

    def __init__(self, web: Any) -> None:
        self._web = web

    def post_message(self, channel: str, text: str, thread_ts: str | None = None) -> str | None:
        response = self._web.chat_postMessage(channel=channel, text=text, thread_ts=thread_ts)
        ts = response.get("ts")  # SlackResponse behaves like a dict; ts identifies the message
        return ts if isinstance(ts, str) else None

    def delete_message(self, channel: str, ts: str) -> None:
        self._web.chat_delete(channel=channel, ts=ts)


# One page is 200 messages; 5 pages (1000) is far beyond any real DM/report thread and caps the
# work a pathological thread can cause (feature 045).
HISTORY_PAGE_LIMIT = 200
HISTORY_MAX_PAGES = 5

# Membership pages mirror the history bounds: 200 ids/page, 5 pages (1000 members) is far beyond
# any real ops channel (feature 104). The TTL is the spec's ≤5-minute roster-staleness bound
# (FR-002): joins/leaves take effect within it, and it caps roster reads at one per channel per
# window rather than one per message.
MEMBERS_PAGE_LIMIT = 200
MEMBERS_MAX_PAGES = 5
MEMBERSHIP_TTL = 300.0


class _WebClientMembership:
    """Adapts ``conversations.members`` to the handler's ``ChannelMembership`` seam (feature 104).

    A per-channel TTL cache guarded by a lock (worker-pool threads share one instance); the clock
    is injected for tests. Fetch errors propagate — the handler fails closed — and an expired
    entry is never served as a fallback, so a revoked member is out within one TTL no matter how
    Slack misbehaves.
    """

    def __init__(
        self,
        web: Any,
        *,
        ttl: float = MEMBERSHIP_TTL,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._web = web
        self._ttl = ttl
        self._now = now
        self._cache: dict[str, tuple[float, set[str]]] = {}
        self._lock = threading.Lock()

    def members(self, channel: str) -> set[str]:
        # The lock spans the fetch on purpose: concurrent askers for a cold channel wait for one
        # roster read instead of racing duplicate ones.
        with self._lock:
            entry = self._cache.get(channel)
            if entry is not None and self._now() - entry[0] < self._ttl:
                return set(entry[1])
            members = self._fetch(channel)
            self._cache[channel] = (self._now(), members)
            return set(members)

    def _fetch(self, channel: str) -> set[str]:
        members: set[str] = set()
        cursor: str | None = None
        for _ in range(MEMBERS_MAX_PAGES):
            kwargs: dict[str, Any] = {"channel": channel, "limit": MEMBERS_PAGE_LIMIT}
            if cursor:
                kwargs["cursor"] = cursor
            response = self._web.conversations_members(**kwargs)
            page = response.get("members")
            if isinstance(page, list):
                members.update(str(user) for user in page)
            metadata = response.get("response_metadata") or {}
            cursor = metadata.get("next_cursor") or None
            if not cursor:
                break
        return members


def _is_missing_scope(exc: Exception) -> bool:
    """Whether a ``slack_sdk`` API error is a ``missing_scope`` refusal (duck-typed, no import)."""

    response = getattr(exc, "response", None)
    try:
        return response is not None and response.get("error") == "missing_scope"
    except Exception:  # noqa: BLE001 — anything unshaped is not the refusal we're looking for
        return False


class _WebClientConversations:
    """Adapts ``conversations.info`` to the handler's ``ConversationTypes`` seam (group-DM fix).

    A conversation's type is immutable, so verdicts cache for the listener's lifetime (bounded by
    the handful of conversations obi is ever mentioned in). A ``missing_scope`` refusal IS a
    verdict: the token reads every channel type a mention can arrive from (``channels:read`` /
    ``groups:read`` are granted), so metadata it cannot read means an im/mpim — and
    ``app_mention`` never fires in a 1:1 im. Granting ``mpim:read`` later simply makes the call
    succeed with ``is_mpim: true``; any other error propagates and the handler fails closed.
    """

    def __init__(self, web: Any) -> None:
        self._web = web
        self._cache: dict[str, bool] = {}
        self._lock = threading.Lock()

    def is_mpim(self, channel: str) -> bool:
        # The lock spans the fetch on purpose (same trade as membership): concurrent askers for
        # an unseen conversation wait for one info read instead of racing duplicates.
        with self._lock:
            verdict = self._cache.get(channel)
            if verdict is None:
                verdict = self._fetch(channel)
                self._cache[channel] = verdict
            return verdict

    def _fetch(self, channel: str) -> bool:
        try:
            response = self._web.conversations_info(channel=channel)
        except Exception as exc:
            if _is_missing_scope(exc):
                logger.info(
                    "conversations.info refused for lack of mpim:read — group DM inferred",
                    extra={"channel": channel},
                )
                return True
            raise
        info = response.get("channel")
        return bool(info.get("is_mpim")) if isinstance(info, dict) else False


class _WebClientUserNames:
    """Adapts ``users.info`` to the handler's ``UserNames`` seam (feature 130, attribution).

    Display names change rarely but DO change, so entries expire on the same TTL as the
    membership roster; failures propagate and the handler falls back to the raw id. Prefers
    the profile display name, then the real name — the same identity a teammate sees in
    Slack, which is what a paged responder should read.
    """

    def __init__(self, web: Any, *, ttl: float = MEMBERSHIP_TTL) -> None:
        self._web = web
        self._ttl = ttl
        self._cache: dict[str, tuple[float, str]] = {}
        self._lock = threading.Lock()

    def display(self, user: str) -> str:
        with self._lock:
            entry = self._cache.get(user)
            if entry is not None and time.monotonic() - entry[0] < self._ttl:
                return entry[1]
            name = self._fetch(user)
            self._cache[user] = (time.monotonic(), name)
            return name

    def _fetch(self, user: str) -> str:
        response = self._web.users_info(user=user)
        info = response.get("user")
        if not isinstance(info, dict):
            return user
        profile = info.get("profile")
        display = profile.get("display_name") if isinstance(profile, dict) else None
        real = profile.get("real_name") if isinstance(profile, dict) else None
        return str(display or real or info.get("real_name") or info.get("name") or user)


class _WebClientHistory:
    """Adapts ``conversations.replies`` to the handler's ``ThreadHistoryFetcher`` seam (045).

    Returns the thread's messages in API order (parent first, oldest→newest), following cursor
    pagination up to :data:`HISTORY_MAX_PAGES` pages. Errors propagate — the handler degrades.
    """

    def __init__(self, web: Any) -> None:
        self._web = web

    def fetch_thread(self, channel: str, thread_ts: str) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(HISTORY_MAX_PAGES):
            kwargs: dict[str, Any] = {
                "channel": channel,
                "ts": thread_ts,
                "limit": HISTORY_PAGE_LIMIT,
            }
            if cursor:
                kwargs["cursor"] = cursor
            response = self._web.conversations_replies(**kwargs)
            page = response.get("messages")
            if isinstance(page, list):
                messages.extend(page)
            metadata = response.get("response_metadata") or {}
            cursor = metadata.get("next_cursor") or None
            if not cursor:
                break
        return messages


class SlackListener:
    """Owns the Socket Mode connection and the worker pool for one ``obi slack`` run."""

    def __init__(
        self,
        config: Config,
        runtime: ChatRuntime,
        heartbeat: Heartbeat | None = None,
    ) -> None:
        if config.slack is None:  # pragma: no cover — the CLI guards this first
            raise ValueError("SlackListener requires a [slack] config section")
        self._slack = config.slack
        self._config = config
        self._runtime = runtime
        # feature 081: optional liveness beacon written while connected; None ⇒ no heartbeat file
        # (the pre-081 behavior, kept for tests and non-supervised foreground runs).
        self._heartbeat = heartbeat

    def run(self) -> int:
        """Connect and serve until interrupted. Returns the process exit code."""

        try:
            from slack_sdk.socket_mode import SocketModeClient
            from slack_sdk.socket_mode.response import SocketModeResponse
            from slack_sdk.web import WebClient
        except ModuleNotFoundError:  # pragma: no cover — slack_sdk is a declared dependency
            logger.error("slack_sdk is not installed; run: uv add slack_sdk")
            return 3

        web = WebClient(token=self._slack.bot_token.reveal())
        # Typed as Any: slack_sdk's SocketModeClient has some unannotated methods (e.g. close()),
        # which mypy --strict would reject as untyped calls.
        client: Any = SocketModeClient(app_token=self._slack.app_token.reveal(), web_client=web)
        # feature 104: only a configured authorized channel activates the roster seam. obi's own
        # user ID (to strip its mention token from questions) is needed unconditionally now that
        # group-DM mentions are answered too. auth_test uses the same bot token as everything
        # else, so a failure here is a credential problem: degrade exactly like a failed connect
        # (FR shape of 009).
        # feature 130: the paging gate reads channel rosters too, so [paging.slack] channels
        # also activate the membership seam even on an allowlist-only chat config.
        paging = self._config.paging
        paging_channels = (
            paging.slack.channels if paging is not None and paging.slack is not None else []
        )
        membership: _WebClientMembership | None = None
        if self._slack.authorized_channels or paging_channels:
            membership = _WebClientMembership(web)
        bot_user_id: str | None = None
        try:
            auth: Any = web.auth_test()
            user_id = auth.get("user_id")
            bot_user_id = user_id if isinstance(user_id, str) else None
        except Exception as exc:  # noqa: BLE001 — bad credential ⇒ degrade clearly
            logger.error("could not identify obi's Slack user", extra={"detail": str(exc)})
            return 3
        handler = SlackHandler(
            runtime=self._runtime,
            allowlist=self._slack.allowlist,
            poster=_WebClientPoster(web),
            # feature 042: one recorder for the listener's lifetime; each worker-thread request
            # opens its own capture scope over it (contextvars are per-thread).
            recorder=recorder_from_config(self._config),
            # feature 045: replies into unknown threads (watch reports, pre-restart chats)
            # recover their visible thread history as session context.
            history=_WebClientHistory(web),
            membership=membership,
            authorized_channels=self._slack.authorized_channels,
            bot_user_id=bot_user_id,
            # group-DM fix: mentions in a multi-person DM answer like an authorized channel.
            conversations=_WebClientConversations(web),
            # feature 130: member paging — the handler arms only when [paging.slack] is set;
            # attribution names are wanted whenever it is (operators-only configs included).
            paging=paging,
            user_names=(
                _WebClientUserNames(web)
                if paging is not None and paging.slack is not None
                else None
            ),
        )
        executor = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="obi-slack")
        stop = threading.Event()

        def _on_request(sm_client: Any, req: Any) -> None:
            # Only events_api envelopes carry questions; ack first (Slack retries un-acked ones),
            # then run the event off the recv loop. Control frames are ignored ⇒ no replay (FR-017).
            if req.type != "events_api":
                return
            sm_client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
            event = _extract_event(req.payload)
            if event is not None:
                executor.submit(handler.handle_event, event)

        client.socket_mode_request_listeners.append(_on_request)

        def _on_signal(_signum: int, _frame: Any) -> None:
            stop.set()

        try:  # signal handlers can only be installed on the main thread
            signal.signal(signal.SIGINT, _on_signal)
            signal.signal(signal.SIGTERM, _on_signal)
        except ValueError:  # pragma: no cover — not the main thread
            pass

        try:
            client.connect()
        except Exception as exc:  # noqa: BLE001 — connect failure ⇒ degrade clearly (FR-010)
            logger.error("could not connect to Slack", extra={"detail": str(exc)})
            executor.shutdown(wait=False, cancel_futures=True)
            return 3

        logger.info(
            "slack listening",
            extra={
                "operators": len(self._slack.allowlist),
                "channels": len(self._slack.authorized_channels),
                # feature 130: how many channels grant member paging (0 ⇒ feature off or
                # operators-only) — the one-line health check for the paging gate's wiring.
                "paging_channels": len(paging_channels),
            },
        )
        if self._heartbeat is not None:
            self._heartbeat.start()  # feature 081: beat while connected, so `slack status` can tell
        try:
            stop.wait()
        except KeyboardInterrupt:  # pragma: no cover — belt-and-suspenders alongside the signal
            pass
        finally:
            if self._heartbeat is not None:
                self._heartbeat.stop()  # marks disconnected ⇒ `status` shows "not connected"
            try:
                client.close()
            except Exception:  # noqa: BLE001
                logger.warning("error closing the Socket Mode client")
            executor.shutdown(wait=False, cancel_futures=True)
        return 0
