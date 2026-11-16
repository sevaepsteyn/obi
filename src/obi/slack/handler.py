"""The Slack event → answer handler (feature 009 + 104, contracts/slack-interface.md).

A thin, total handler: it filters non-question events, authorizes the sender (static allowlist
∪ membership of the operator's authorized channels — feature 104 — refusing, never silently,
everyone else and failing **closed** when membership can't be read), answers DMs and direct
@obi mentions inside authorized channels **and group DMs** in-thread, and on the happy path
feeds the message into the **unchanged** chat runtime, attributing every audited access to the
sender's Slack user ID. Mentions anywhere else still get the 009 pointer to DM; ordinary
channel and group-DM chatter is never processed. It holds no business logic of its own
(Constitution III) and never raises to the listener: model/Slack failures degrade to a posted
message + a log, so one bad message can't take the listener down.

It is decoupled from ``slack_sdk``: it speaks plain event dicts and four Protocols — a
:class:`SlackPoster` for sending, an optional :class:`ThreadHistoryFetcher` (feature 045) for
recovering an unknown thread's prior messages (e.g. a watch report being replied to) as session
context, an optional :class:`ChannelMembership` (feature 104) for the authorized channels'
rosters, and an optional :class:`ConversationTypes` for telling a group DM apart from an
ordinary channel when a mention arrives — so it is exercised fully offline (fakes + the stub
LLM + fake vendors).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import replace
from typing import Any, Protocol

from obi.chat.report import slack_footer
from obi.chat.session import ChatRuntime, ask
from obi.core.audit import AuditRecorder, audit_scope
from obi.core.config import PagingSettings
from obi.core.llm import ChatUnavailable
from obi.core.models import AuditOutcome, AuditRecord, _utcnow
from obi.core.textfmt import to_slack_mrkdwn
from obi.core.toolset import ToolDefinition
from obi.paging.chat_tool import build_page_send_tool
from obi.slack.sessions import SessionStore

logger = logging.getLogger("obi")

# Slack hard-limits a message around 4000 chars; split below that, never drop (spec edge case).
SLACK_MAX_CHARS = 3900

# Retired as an outbound message — obi no longer posts a per-session model notice (the DM stays
# quiet until it has something to say). Kept only so :func:`_is_service_message` can still filter
# any such notice left in older threads when seeding context (feature 045).
_TRANSPARENCY = (
    "obi uses a cloud reasoning model. Your questions and the read-only results obi looks up are "
    "sent to it and to Slack; your credentials are not."
)
# A transient status placeholder: posted the instant a question lands (FR-012, immediate life on a
# slow read) and deleted the moment the real reply is ready, so it reads as being replaced rather
# than stacked. ``_WORKING_LEGACY`` is the earlier wording, kept only to filter it out of the
# seeded context of threads that predate this change.
_WORKING = "🧠 thinking…"
_WORKING_LEGACY = "obi is looking into that…"
_REFUSAL = "Sorry — you're not authorized to use obi. Contact the administrator if you need access."
# Membership unverifiable (feature 104): honest and distinct from the flat refusal — the user
# may well be authorized; obi just can't prove it right now (fail closed, FR-010).
_CANT_VERIFY = "obi couldn't verify your access right now — please try again in a moment."
_PLEASE_DM = "Hi! obi doesn't answer in this channel — send me a DM and I'll help there."
# A bare "@obi" in an authorized channel: silence would read as broken, so nudge — without
# spending a model call on an empty question (feature 104, FR-009).
_MENTION_HINT = (
    "Ask me a network question — e.g. `@obi is site-gw-1 up?` — and I'll answer in this thread."
)
_ERROR_REPLY = "Sorry, I couldn't answer that one. Please try again."
_HISTORY_UNAVAILABLE = (
    "obi couldn't read the earlier messages in this thread — answering without that context."
)

# Bounds on seeded thread context (feature 045, FR-006): newest kept, oldest dropped. 40 messages /
# 16k chars comfortably covers a digest with several full threaded reports.
MAX_SEEDED_MESSAGES = 40
MAX_SEEDED_CHARS = 16_000

_CONTEXT_HEADER = (
    "[Slack thread context — earlier messages in the thread the operator is replying to, oldest "
    "first{omitted}. Lines marked 'obi' are obi's own earlier messages and reports.]"
)


class SlackPoster(Protocol):
    """The minimal Slack-send seam the handler needs (satisfied by the real client and a fake)."""

    def post_message(self, channel: str, text: str, thread_ts: str | None = None) -> str | None: ...

    def delete_message(self, channel: str, ts: str) -> None: ...


class ThreadHistoryFetcher(Protocol):
    """The thread-history seam (feature 045): a thread's messages, oldest→newest, parent first.

    Returns plain Slack Web-API message dicts and may raise anything — the handler catches and
    degrades. The real implementation wraps ``conversations.replies``.
    """

    def fetch_thread(self, channel: str, thread_ts: str) -> list[dict[str, Any]]: ...


class ChannelMembership(Protocol):
    """The channel-roster seam (feature 104): the current member user IDs of one channel.

    May raise anything — a raise means membership is UNKNOWN and authorization fails **closed**
    (an honest "couldn't verify" reply, never access). The real implementation is
    ``listener._WebClientMembership``: ``conversations.members`` behind a TTL cache.
    """

    def members(self, channel: str) -> set[str]: ...


class UserNames(Protocol):
    """Resolves a Slack user id to a human display name (feature 130, page attribution).

    Implementations should cache (names change rarely; attribution must not cost an API
    round-trip per page) and may raise — the handler falls back to the raw id, so the
    attribution guarantee is total.
    """

    def display(self, user: str) -> str: ...


class ConversationTypes(Protocol):
    """The conversation-classification seam: is this channel a group DM (mpim)?

    Mentions carry no ``channel_type``, so telling a group DM (answer it — obi was deliberately
    added to that conversation) from an ordinary unauthorized channel (point to DM) needs Slack's
    ``conversations.info``. May raise — a raise means the type is UNKNOWN and the mention fails
    **closed** (an honest "couldn't verify", never an answer, never a wrong pointer). The real
    implementation is ``listener._WebClientConversations``.
    """

    def is_mpim(self, channel: str) -> bool: ...


def _split_message(text: str, limit: int = SLACK_MAX_CHARS) -> list[str]:
    """Split an answer into Slack-sized chunks, preferring line boundaries; never drop content."""

    text = text or "(no answer)"
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:  # a single over-long line: hard-split it
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) > limit:
            chunks.append(current)
            current = line
        else:
            current += line
    if current:
        chunks.append(current)
    return chunks


def _is_service_message(text: str) -> bool:
    """Whether a bot-authored message is obi service noise (transparency/working/notice)."""

    return text.startswith(_TRANSPARENCY) or text in (
        _WORKING,
        _WORKING_LEGACY,
        _HISTORY_UNAVAILABLE,
    )


def render_thread_context(
    fetched: list[dict[str, Any]], *, exclude_ts: str, replier: str
) -> str | None:
    """Render fetched thread history into one labeled context block, or ``None`` if empty.

    Feature 045 (contracts/thread-context.md): chronological ``obi:``/``operator:`` lines; the
    triggering reply (``exclude_ts``) and obi's own service messages are dropped; authorless
    messages are skipped; bounded to the newest :data:`MAX_SEEDED_MESSAGES` /
    :data:`MAX_SEEDED_CHARS`, with the header noting when earlier messages were omitted.
    """

    lines: list[str] = []
    for message in fetched:
        text = str(message.get("text") or "").strip()
        if not text or str(message.get("ts") or "") == exclude_ts:
            continue
        if message.get("bot_id") or message.get("subtype") == "bot_message":
            if _is_service_message(text):
                continue
            lines.append(f"obi: {text}")
        elif isinstance(message.get("user"), str):
            user = message["user"]
            who = "operator" if user == replier else f"operator ({user})"
            lines.append(f"{who}: {text}")
    if not lines:
        return None
    truncated = len(lines) > MAX_SEEDED_MESSAGES
    lines = lines[-MAX_SEEDED_MESSAGES:]
    kept: list[str] = []
    total = 0
    for line in reversed(lines):  # newest kept when the char bound bites (FR-006)
        total += len(line) + 1
        if kept and total > MAX_SEEDED_CHARS:
            truncated = True
            break
        kept.append(line)
    kept.reverse()
    omitted = " (earlier messages omitted)" if truncated else ""
    return "\n".join([_CONTEXT_HEADER.format(omitted=omitted), *kept])


class SlackHandler:
    """Routes one Slack event to a posted answer over the shared chat runtime."""

    def __init__(
        self,
        *,
        runtime: ChatRuntime,
        allowlist: Sequence[str],
        poster: SlackPoster,
        sessions: SessionStore | None = None,
        recorder: AuditRecorder | None = None,
        history: ThreadHistoryFetcher | None = None,
        membership: ChannelMembership | None = None,
        authorized_channels: Sequence[str] = (),
        bot_user_id: str | None = None,
        conversations: ConversationTypes | None = None,
        paging: PagingSettings | None = None,
        user_names: UserNames | None = None,
    ) -> None:
        self._runtime = runtime
        self._allowlist = set(allowlist)
        self._poster = poster
        self._history = history
        self._sessions = sessions or SessionStore()
        self._audit = runtime.ctx.audit
        self._recorder = recorder
        # feature 104: membership of these channels is the second authorization source, and
        # direct @-mentions inside them are answered. bot_user_id (from auth_test) is obi's own
        # mention token, stripped from mention text to recover the question.
        self._membership = membership
        self._authorized_channels = list(authorized_channels)
        self._bot_user_id = bot_user_id
        # group-DM fix: classifies a mention's channel so a group DM answers like an authorized
        # channel instead of getting the "please DM" pointer. None ⇒ the pre-fix pointer behavior.
        self._conversations = conversations
        # feature 130 (constitution 3.0.0): Slack-DM member paging. Armed only when BOTH
        # [paging] and [paging.slack] are configured; otherwise every turn behaves pre-130.
        self._paging = paging if paging is not None and paging.slack is not None else None
        self._user_names = user_names

    def handle_event(self, event: dict[str, Any]) -> None:
        """Handle one inbound Slack event. Total — never raises to the listener."""

        # feature 042: contextvars do not cross the listener's worker-pool threads, so the
        # capture scope opens here, inside the worker. `_answer` narrows it to the thread's
        # session; refusals/errors before that land session-less but still source="slack".
        try:
            with audit_scope(self._recorder, "slack", None):
                self._route(event)
        except Exception:  # noqa: BLE001 — a handler error must never crash the listener
            logger.exception("slack handler error")

    # --- routing ------------------------------------------------------------------------------

    def _route(self, event: dict[str, Any]) -> None:
        # Ignore the bot's own posts, other bots, and edited/deleted/system messages (no loops).
        if event.get("bot_id") or event.get("subtype"):
            return
        etype = event.get("type")
        if etype == "app_mention":
            self._mention(event)
            return
        if etype != "message" or event.get("channel_type") != "im":
            return  # only direct messages are questions; ignore everything else
        user = event.get("user")
        channel = event.get("channel")
        text = str(event.get("text") or "").strip()
        if not isinstance(user, str) or not isinstance(channel, str) or not text:
            return
        verdict = self._authorize(user)
        if verdict == "denied":
            self._refuse(channel, user, event)
            return
        if verdict == "unknown":
            self._refuse_unverified(channel, user, event)
            return
        self._answer(channel, user, text, event, origin="im")

    @staticmethod
    def _root(event: dict[str, Any]) -> str:
        """The thread root: the parent ts if this is a threaded reply, else the message's own ts."""

        return str(event.get("thread_ts") or event.get("ts") or "")

    # --- channel mentions (feature 104) -------------------------------------------------------

    def _mention(self, event: dict[str, Any]) -> None:
        """A direct @obi mention in a channel or a group DM.

        In an operator-authorized channel — or a group DM, a conversation obi was deliberately
        added to: authorize the sender (same gate as DMs), strip obi's own mention token, and
        answer in the mention's thread through the unchanged `_answer` flow — a bare mention gets
        a usage hint instead of a model call (FR-009). In every other channel the 009 behavior
        stands: a brief pointer to DM, never an answer (FR-007); when the conversation's type
        can't be read the mention fails closed with the honest "couldn't verify". Ordinary
        channel chatter never reaches here — Slack emits ``app_mention`` only for direct
        mentions, and plain channel ``message`` events stay ignored (FR-006).
        """

        channel = event.get("channel")
        if not isinstance(channel, str):
            return
        where = "authorized" if channel in self._authorized_channels else self._classify(channel)
        if where == "other":
            self._safe_post(channel, _PLEASE_DM, self._root(event))
            return
        user = event.get("user")
        if not isinstance(user, str):
            return
        if where == "unknown":
            self._refuse_unverified(channel, user, event)
            return
        verdict = self._authorize(user)
        if verdict == "denied":
            self._refuse(channel, user, event)
            return
        if verdict == "unknown":
            self._refuse_unverified(channel, user, event)
            return
        text = self._strip_mention(str(event.get("text") or ""))
        if not text:
            self._safe_post(channel, _MENTION_HINT, self._root(event))
            return
        self._answer(channel, user, text, event, origin="mention")

    def _strip_mention(self, text: str) -> str:
        """The question text of a mention: obi's own ``<@id>`` tokens removed, tidied.

        Only obi's token is stripped — mentions of *other* users stay in the question ("is
        <@U_BOB>'s router up?" keeps its meaning). A leading ``:``/``,`` left by the "@obi:"
        addressing style is dropped with the surrounding whitespace.
        """

        if self._bot_user_id:
            text = text.replace(f"<@{self._bot_user_id}>", " ")
        return " ".join(text.split()).lstrip(":, ").strip()

    def _classify(self, channel: str) -> str:
        """A mention outside the authorized channels: ``"mpim"``, ``"other"``, or ``"unknown"``.

        Without the seam wired the pre-fix behavior stands (everything is ``"other"`` ⇒ the DM
        pointer). A lookup that raises means the type is unknowable right now — fail closed.
        """

        if self._conversations is None:
            return "other"
        try:
            return "mpim" if self._conversations.is_mpim(channel) else "other"
        except Exception:  # noqa: BLE001 — type unreadable ⇒ unknown, never a guess
            logger.warning("slack conversation type lookup failed", extra={"channel": channel})
            return "unknown"

    # --- authorization (US2; widened by feature 104) ------------------------------------------

    def _authorize(self, user: str) -> str:
        """One of ``"allowed"``, ``"denied"``, ``"unknown"`` for this sender.

        Allowlist first — the operator's access never depends on the Slack roster API. Then
        membership of each authorized channel; a roster read that raises makes the final verdict
        ``"unknown"`` (fail closed with the honest message) unless a later channel positively
        grants. An expired-cache fallback is deliberately absent (research §3): revocation stays
        bounded at the cache TTL.
        """

        if user in self._allowlist:
            return "allowed"
        unknown = False
        if self._membership is not None:
            for channel in self._authorized_channels:
                try:
                    if user in self._membership.members(channel):
                        return "allowed"
                except Exception:  # noqa: BLE001 — roster unreadable ⇒ unknown, never open
                    logger.warning("slack membership lookup failed", extra={"channel": channel})
                    unknown = True
        return "unknown" if unknown else "denied"

    # --- member paging (feature 130, constitution 3.0.0) --------------------------------------

    def _page_tool(self, user: str, origin: str) -> ToolDefinition | None:
        """The per-request ``page_send`` tool, or ``None`` when this turn may not page.

        Offered ONLY for a direct-message turn (``origin == "im"`` — group DMs and channel
        mentions arrive as ``"mention"``) with ``[paging.slack]`` configured and the sender
        either an operator or a current member of a paging-authorized channel, checked
        fail-closed through the SAME membership cache the 104 chat gate uses. A denial is
        audited (``slack:page_gate``); an unconfigured feature audits nothing (SC-006 —
        the gate never ran).
        """

        if origin != "im" or self._paging is None:
            return None
        slack = self._paging.slack
        assert slack is not None  # the constructor gated on it
        verdict = "denied"
        if user in self._allowlist:
            verdict = "offered"
        elif self._membership is not None:
            detail = None
            for channel in slack.channels:
                try:
                    if user in self._membership.members(channel):
                        verdict = "offered"
                        break
                except Exception:  # noqa: BLE001 — roster unreadable ⇒ no tool, never open
                    logger.warning("slack page-gate lookup failed", extra={"channel": channel})
                    detail = "membership unreadable"
            if verdict != "offered" and detail is not None:
                self._audit_page_gate(user, detail)
                return None
        if verdict != "offered":
            self._audit_page_gate(user, "not a member of a paging-authorized channel")
            return None
        return build_page_send_tool(
            self._paging, sender_id=user, sender_display=self._display_name(user)
        )

    def _audit_page_gate(self, user: str, detail: str) -> None:
        self._audit.record(
            AuditRecord(
                timestamp=_utcnow(),
                action="slack:page_gate",
                target="page_send",
                reason="member paging gate",
                outcome=AuditOutcome.DENIED,
                detail=detail,
                actor=user,
            )
        )

    def _display_name(self, user: str) -> str:
        """The sender's display name for page attribution; the raw id when unresolvable."""

        if self._user_names is None:
            return user
        try:
            return self._user_names.display(user) or user
        except Exception:  # noqa: BLE001 — attribution must be total; the id is always true
            logger.warning("slack display-name lookup failed", extra={"user": user})
            return user

    def _refuse_unverified(self, channel: str, user: str, event: dict[str, Any]) -> None:
        """Membership unverifiable: fail closed with an honest note + an ERROR audit (FR-010)."""

        self._safe_post(channel, _CANT_VERIFY, self._root(event))
        self._audit.record(
            AuditRecord(
                timestamp=_utcnow(),
                action="slack:auth_error",
                target=user,
                reason="slack request (membership unverifiable)",
                outcome=AuditOutcome.ERROR,
                actor=user,
            )
        )

    def _refuse(self, channel: str, user: str, event: dict[str, Any]) -> None:
        """Refuse a non-allowlisted sender: a brief message + a DENIED audit, and zero access."""

        self._safe_post(channel, _REFUSAL, self._root(event))
        self._audit.record(
            AuditRecord(
                timestamp=_utcnow(),
                action="slack:unauthorized",
                target=user,
                reason="slack request (denied)",
                outcome=AuditOutcome.DENIED,
                actor=user,
            )
        )

    # --- answer flow (US1/US3/US4) ------------------------------------------------------------

    def _answer(
        self, channel: str, user: str, text: str, event: dict[str, Any], *, origin: str = "im"
    ) -> None:
        root = self._root(event)
        with audit_scope(self._recorder, "slack", f"slack-{channel}-{root}"):
            self._answer_scoped(channel, user, text, event, root, origin=origin)

    def _answer_scoped(
        self,
        channel: str,
        user: str,
        text: str,
        event: dict[str, Any],
        root: str,
        *,
        origin: str = "im",
    ) -> None:
        is_new, messages = self._sessions.get_or_create((channel, root))
        if is_new:
            # feature 045: an unknown thread (e.g. a watch-report thread, or any thread from
            # before a restart) recovers its visible history as the conversation so far.
            self._seed_thread_context(channel, user, event, root, messages)
        # A transient "thinking" placeholder for immediate feedback on a slow read (FR-012),
        # deleted below the moment the real reply — answer or error — is ready to post.
        working_ts = self._safe_post(channel, _WORKING, root)
        snapshot = len(messages)
        # A per-request context carries this question + this operator's id, so concurrent
        # operators never share mutable state and every audited access is attributed (FR-008/016).
        # feature 130: an eligible DM turn ALSO carries the page_send tool — injected here, per
        # request, closed over THIS sender's identity; no toolset builder ever returns it
        # (constitution 3.0.0), so mentions, group DMs, and every other surface stay page-free.
        tools = self._runtime.tools
        page_tool = self._page_tool(user, origin)
        if page_tool is not None:
            tools = [*tools, page_tool]
        req_runtime = replace(
            self._runtime, ctx=replace(self._runtime.ctx, question=text, actor=user), tools=tools
        )
        try:
            result = ask(req_runtime, messages, text)
        except ChatUnavailable as exc:
            del messages[snapshot:]  # roll back the partial turn so the session stays valid
            self._delete(channel, working_ts)
            self._safe_post(channel, f"obi can't reach the reasoning model right now: {exc}", root)
            return
        except Exception as exc:  # noqa: BLE001 — never crash the listener on one bad answer
            del messages[snapshot:]
            logger.exception("slack answer failed")
            self._audit.record(
                AuditRecord(
                    timestamp=_utcnow(),
                    action="slack:error",
                    target=channel,
                    reason=text,
                    outcome=AuditOutcome.ERROR,
                    detail=str(exc),
                    actor=user,
                )
            )
            self._delete(channel, working_ts)
            self._safe_post(channel, _ERROR_REPLY, root)
            return
        self._delete(channel, working_ts)
        # Render the model's markdown as Slack mrkdwn so **bold**/#/[t](u) don't arrive as literal
        # symbols (feature 082). Applied here, not in _safe_post, so service/status posts (_WORKING,
        # error replies) stay untouched. Feature 089: one subordinate footer line (tool names +
        # tokens + cost — never arguments or results) rides the same reply; a footer failure must
        # never cost the answer (FR-013).
        reply = to_slack_mrkdwn(result.answer)
        try:
            reply = f"{reply}\n\n{slack_footer(result, self._runtime.model)}"
        except Exception:  # noqa: BLE001 - report chrome must never block the reply
            logger.exception("slack turn footer assembly failed")
        for chunk in _split_message(reply):
            self._safe_post(channel, chunk, root)

    # --- thread-context seeding (feature 045) ---------------------------------------------------

    def _seed_thread_context(
        self,
        channel: str,
        user: str,
        event: dict[str, Any],
        root: str,
        messages: list[dict[str, Any]],
    ) -> None:
        """Seed a NEW session for a threaded reply with the thread's prior messages.

        Only threaded replies (``thread_ts`` present and ≠ the message's own ``ts``) have history
        to recover; top-level DMs seed nothing. A fetch failure degrades to an explicit in-thread
        notice + an unseeded session, and is never sticky (the session now exists).
        """

        if self._history is None:
            return
        ts = str(event.get("ts") or "")
        thread_ts = str(event.get("thread_ts") or "")
        if not thread_ts or thread_ts == ts:
            return
        try:
            fetched = self._history.fetch_thread(channel, thread_ts)
        except Exception:  # noqa: BLE001 — Slack unreadable: degrade, never crash (FR-007)
            logger.warning("slack thread history fetch failed", extra={"channel": channel})
            self._safe_post(channel, _HISTORY_UNAVAILABLE, root)
            return
        block = render_thread_context(fetched, exclude_ts=ts, replier=user)
        if block is not None:
            messages.append({"role": "user", "content": block})

    # --- posting ------------------------------------------------------------------------------

    def _safe_post(self, channel: str, text: str, thread_ts: str | None) -> str | None:
        """Post to Slack, swallowing transport failures (Slack down must not crash the listener).

        Returns the posted message's ``ts`` when the client reports one (so a transient status
        message can later be deleted), or ``None`` if the post failed or no ts came back.
        """

        try:
            return self._poster.post_message(channel, text, thread_ts)
        except Exception:  # noqa: BLE001 — Slack unreachable: log and continue
            logger.warning("slack post failed", extra={"channel": channel})
            return None

    def _delete(self, channel: str, ts: str | None) -> None:
        """Delete a previously-posted message (the transient status), swallowing failures."""

        if not ts:
            return
        try:
            self._poster.delete_message(channel, ts)
        except Exception:  # noqa: BLE001 — a failed cleanup must never crash the listener
            logger.warning("slack delete failed", extra={"channel": channel})
