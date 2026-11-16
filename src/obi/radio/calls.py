"""The directed-call state machine (feature 088, US2) — pure protocol logic.

One call at a time, walked as:

    idle → acking → awaiting_request → composing → answering → open → (closed)

Every event method takes ``now_us`` (no clock inside — Constitution II testability) and
returns **effects**: small data objects the runner executes (open a store row, hand text
to the TX gate, start a compose, close the call). The machine never performs I/O and
never touches the PTT; per tx-gate G1 the only path to RF is the TxExecutor, which the
runner feeds from :class:`TransmitEffect`\\ s.

Protocol decisions encoded here (spec FR-008…FR-013):

- the ack/say-again/unavailability/garble-close texts are canned config, never composed;
- "say again" at most once per call, then the canned ON-AIR garble close ("unable to
  copy, out", feature 093 — previously silent) with the operator note kept;
- the CALLER clears ("…clear"/"…out", or a bare acknowledgment); expiry closes silently —
  obi never transmits a bare "clear";
- shadow dispositions count as success so the shadow stages exercise the whole flow;
- the WINDOW BELONGS TO THE CALLER (feature 109, operator decision 2026-08-28): every
  intelligible in-window transmission composes unless it is clearing, a sign-off, or
  garble — and a different NAMED caller's full hail supersedes from any state ("someone
  makes another call"). Only anonymous tail-less re-fires outside the request states
  remain ``busy_with_call``;
- a NEW CALL STARTS A NEW BUDGET (feature 129, field 2026-09-02): the SAME caller's
  formal re-hail — doubled name or explicit callsign, carrying a request — supersedes
  the open call into a fresh one with a fresh answer budget, immediately from the
  listening states and at the settle drain when held mid-answer; a single-name re-fire
  stays a continuation bound by the open call's budget (the junk-loop belt);
- …but the window belongs to the caller, NOT to the channel (feature 114, field
  2026-08-29): a transmission the wording addresses to ANOTHER station — a hail, or an
  answer to one — closes the window and draws no transmission at all. That is the fourth
  in-window exception, and the only one obi decides for itself;
- OUTBOUND calls (feature 124): the machine can also originate — ``start_outbound``
  walks hailing → awaiting_go → (standing_by →) delivering and then converges on the
  same ``open`` state as an inbound call, so post-delivery behavior is literally the
  code above. The operator's message NEVER airs before a heard go-ahead (contract O2);
  only an elapsed ack window burns a retry (O6); a matched go-ahead outranks the 114
  read INSIDE obi's own window and nowhere else (O8); and any inbound caller outranks
  the pending attempt (O7 — the request requeues via ``OutboundOutcomeEffect``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from obi.radio.outbound import GoAheadDecision, is_decline, is_standby, with_over_sign_off

# Dispositions that count as "it went out (or would have, in shadow)" for flow purposes.
_SUCCESS_DISPOSITIONS = frozenset({"transmitted", "shadow_logged", "shadow_dmed"})

# A short transmission made only of these tokens is an acknowledgment, not a follow-up
# question — it closes the call (the caller clearing per the channel guidelines).
_ACK_TOKENS = frozenset(
    {"copy", "that", "roger", "wilco", "thanks", "thank", "you", "affirm", "affirmative", "ten4"}
)


@dataclass(frozen=True)
class OpenCallEffect:
    caller: str | None
    tier: str
    # Feature 124: 'inbound' (someone hailed obi) or 'outbound' (obi is hailing someone).
    direction: str = "inbound"


@dataclass(frozen=True)
class TransmitEffect:
    kind: str  # a call-flow subset of radiostore.OUTBOUND_KINDS (no 'operator' here)
    text: str


@dataclass(frozen=True)
class Turn:
    """One completed exchange of the open call (feature 091): the caller's request and
    the answer obi actually put on the air — ``TxOutcome.text``, post-cap-truncation,
    canonical spelling — never the model's draft (FR-005)."""

    request: str
    answer: str


@dataclass(frozen=True)
class ComposeEffect:
    request: str
    caller: str | None
    tier: str
    # The call's transcript so far, oldest first, excluding ``request`` (feature 091).
    # A tuple snapshot: safe across the worker-thread boundary, immune to later turns.
    history: tuple[Turn, ...] = ()


@dataclass(frozen=True)
class CloseEffect:
    # cleared | timeout | garble | failure | dropped | superseded | abandoned |
    # third_party ('complete' retired by feature 109 — in-window traffic composes
    # instead; 'third_party' added by 114 — the channel moved to other stations;
    # 124 adds the outbound closures: requeued | unanswered | declined | expired)
    closure: str
    note: str | None  # Slack note text, or None for silent closures


@dataclass(frozen=True)
class OutboundOutcomeEffect:
    """What became of the outbound call REQUEST (feature 124, contract O9).

    Emitted exactly once per attempt while the message is still undelivered — the
    runner maps it onto the ``call_requests`` row: ``requeue`` flips it back to pending
    (attempts preserved — a busy channel or an outranking inbound caller never burns a
    retry, O6/O7); every other value settles it terminally. ``delivered`` is emitted at
    the delivery TX success, with the call still open — the exchange continues as a
    normal call after the request is settled.
    """

    # delivered | unanswered | declined | expired | failed | requeue
    outcome: str
    detail: str | None = None
    attempts: int = 0


Effect = OpenCallEffect | TransmitEffect | ComposeEffect | CloseEffect | OutboundOutcomeEffect


@dataclass(frozen=True)
class TransmissionOutcome:
    """What a heard transmission means for the call, plus a trigger-reason override."""

    effects: tuple[Effect, ...]
    trigger_reason_override: str | None = None


_TRAILING_OVER = re.compile(r"\bover\s*([.!?]*)\s*$", re.IGNORECASE)
_TRAILING_OVER_AND_OUT = re.compile(r"\bover and out\b\s*[.!?]*\s*$", re.IGNORECASE)


def _with_terminal_sign_off(text: str) -> str:
    """FR-012: rewrite a trailing "over" to "over and out" (append when missing).

    Voice procedure: "over" invites a reply, "out" ends the exchange. Deterministic —
    protocol correctness never rides on model compliance (the bracket-scrub precedent).
    """

    if _TRAILING_OVER_AND_OUT.search(text):
        return text
    match = _TRAILING_OVER.search(text)
    if match:
        return text[: match.start()] + "over and out" + match.group(1)
    stripped = text.rstrip()
    return f"{stripped}, over and out" if stripped else text


_TRAILING_OUT = re.compile(r"\bout\s*[.!?]*\s*$", re.IGNORECASE)


def _with_out_sign_off(text: str) -> str:
    """FR-006 (093): canned final phrases end with a bare "out" — never "over".

    A phrase that closes the call invites no reply (shape clarified 2026-08-24), so a
    missing sign-off gets ", out" appended; compliant text (any case, trailing
    punctuation) passes through untouched. Deterministic and idempotent, exactly like
    ``_with_terminal_sign_off`` above — protocol correctness never rides on config.
    """

    if _TRAILING_OUT.search(text):
        return text
    stripped = text.rstrip()
    return f"{stripped}, out" if stripped else text


#: Sign-off shapes (feature 109). The widened in-window gate composes everything that is
#: not clearing, so departures that used to fall through harmlessly ("Quill, back on IT",
#: radio.db call 5) would draw an answer at someone walking away. A sign-off OPENS the
#: transmission (its first token at index 0 or 1 — "back to" deeper in is a REQUEST
#: shape: "set the portal back to red") and sign-offs are short (≤ _SIGN_OFF_MAX_TOKENS).
#: Bare "going to"/"moving" are deliberately absent — "I'm going to need the status"
#: must compose.
_SIGN_OFF_SHAPES: tuple[tuple[str, ...], ...] = (
    ("back", "on"),
    ("back", "to"),
    ("moving", "to"),
    ("moving", "back", "to"),
    ("heading", "to"),
    ("heading", "back", "to"),
    ("going", "back", "to"),
)
_SIGN_OFF_MAX_TOKENS = 8


def _is_sign_off(text: str, extra: list[str]) -> bool:
    """Whether an in-window transmission is the caller leaving the channel.

    ``extra`` is the operator's ``[radio] sign_offs`` channel idiom, matched as a
    normalized substring within the same length bound.
    """

    normalized = re.sub(r"[^a-z0-9']+", " ", text.lower()).strip()
    tokens = normalized.split()
    if not tokens or len(tokens) > _SIGN_OFF_MAX_TOKENS:
        return False
    for phrase in extra:
        squeezed = re.sub(r"[^a-z0-9']+", " ", phrase.lower()).strip()
        if squeezed and squeezed in normalized:
            return True
    for start in (0, 1):
        for shape in _SIGN_OFF_SHAPES:
            if tuple(tokens[start : start + len(shape)]) == shape:
                return True
    return False


def _is_final_clear(text: str) -> bool:
    """The explicit clearing form ("…clear"/"…out") — always honored, flag or no flag."""

    tokens = re.sub(r"[^a-z0-9']+", " ", text.lower()).split()
    return bool(tokens) and tokens[-1] in {"clear", "out"}


def _is_bare_ack(text: str) -> bool:
    """A short all-acknowledgment transmission ("copy that", "thanks") — normally a
    clear, but the caller's ANSWER when obi's last transmission expected a reply
    (feature 109 US4: "affirmative" to "want an animated effect?")."""

    tokens = re.sub(r"[^a-z0-9']+", " ", text.lower()).split()
    return bool(tokens) and len(tokens) <= 4 and all(t in _ACK_TOKENS for t in tokens)


def _is_clearing(text: str) -> bool:
    """Whether a transmission is the caller clearing the call (never obi's decision to TX)."""

    return _is_final_clear(text) or _is_bare_ack(text)


@dataclass
class CallMachine:
    """One station's call protocol. See the module docstring for the walk."""

    ack_text: str
    say_again_text: str
    unavailable_text: str
    call_timeout_s: int
    compose_deadline_s: int
    operator_handles: list[str]
    # Per-call answer budget (operator direction 2026-08-19: "fail on the side of not
    # responding"): after this many composed answers the call closes SILENTLY. Since the
    # no_speech/repetition gates now stop junk upstream, every compose requires a fresh
    # intelligible caller transmission — so the belt is generous (field 2026-08-20: a cap
    # of 2 strangled a live conversation after a mis-heard site name burned two answers).
    max_answers_per_call: int = 4
    # The radio "thinking…" (operator request 2026-08-20): when composing runs longer
    # than this, transmit the canned standby ONCE per call so the caller knows obi is
    # working the request rather than gone.
    standby_text: str = "stand by"
    standby_after_s: int = 4
    # The on-air give-up (feature 093, FR-011): when a garbled exchange spends its one
    # "say again", the call closes ON the air with this canned phrase — a live, engaged
    # caller is never left waiting on a station that silently walked away. The machine
    # appends the terminal ", out" deterministically whatever the config says.
    garble_close_text: str = "unable to copy, out"
    # Channel-specific sign-off idiom (feature 109, ``[radio] sign_offs``) joining the
    # built-in _SIGN_OFF_SHAPES — the widened in-window gate must not answer departures.
    sign_offs: list[str] = field(default_factory=list)
    # Transcript bound (feature 091, FR-004). max_answers_per_call already caps a call
    # at 4 turns; this belt survives future budget changes rather than doing work today.
    max_history_turns: int = 8
    # Outbound calls (feature 124): the listen window after each hail, and how many
    # RE-hails follow the first (0 = a single hail). Only an elapsed window increments
    # the attempt count — busy channels and inbound supersedes requeue instead (O6).
    outbound_ack_window_s: int = 30
    outbound_max_rehails: int = 2

    state: str = "idle"
    caller: str | None = None
    tier: str = "safe"
    # 124: which side originated the open call. 'outbound' from start_outbound until
    # the close funnels through _reset; every inbound path leaves it 'inbound'.
    direction: str = "inbound"
    say_again_used: bool = False
    # The open call's completed turns (feature 091). In-memory only, BY REQUIREMENT
    # (FR-003): history must die with the call, and with the process.
    transcript: list[Turn] = field(default_factory=list, repr=False)
    _composed_request: str | None = field(default=None, repr=False)
    _deadline_us: int | None = field(default=None, repr=False)
    _answer_closes_with: str | None = field(default=None, repr=False)
    _answer_solicits: bool = field(default=False, repr=False)
    # Feature 109 US4: the composer's advisory expects-reply flag for the LAST answer.
    # While set, a bare acknowledgment is the caller's reply (composes), not a clear;
    # the explicit "…out"/"…clear" form always outranks it. Never touches expiry.
    _answer_expects_reply: bool = field(default=False, repr=False)
    _pending_request: str | None = field(default=None, repr=False)
    # Whether the held request arrived as a FORMAL hail — doubled name or explicit
    # callsign (feature 129): at the settle drain it opens a fresh call (fresh budget)
    # instead of spending this call's, matching the immediate-supersede path. The parsed
    # caller rides along so an explicit callsign re-states identity at drain time.
    _pending_new_call: bool = field(default=False, repr=False)
    _pending_caller: str | None = field(default=None, repr=False)
    # The operator note for a deferred close (garble/failure), stashed while the closing
    # transmission is in flight (feature 093 — the CloseEffect follows the TX result).
    _close_note: str | None = field(default=None, repr=False)
    _answers_sent: int = field(default=0, repr=False)
    _standby_deadline_us: int | None = field(default=None, repr=False)
    _standby_sent: bool = field(default=False, repr=False)
    # Outbound-call state (feature 124), live only while direction == 'outbound'.
    _outbound_callee: str | None = field(default=None, repr=False)
    _outbound_message: str | None = field(default=None, repr=False)
    _outbound_expires_us: int | None = field(default=None, repr=False)
    _outbound_attempts: int = field(default=0, repr=False)
    _delivered: bool = field(default=False, repr=False)

    # -- helpers ------------------------------------------------------------------------

    def _tier_for(self, caller: str | None) -> str:
        if caller is None:
            return "safe"
        handles = {h.strip().lower() for h in self.operator_handles}
        return "operator" if caller.strip().lower() in handles else "safe"

    def _reset(self) -> None:
        self.state = "idle"
        self.caller = None
        self.tier = "safe"
        self.say_again_used = False
        self._deadline_us = None
        self._answer_closes_with = None
        self._answer_solicits = False
        self._answer_expects_reply = False
        self._pending_request = None
        self._pending_new_call = False
        self._pending_caller = None
        self._close_note = None
        self._answers_sent = 0
        self._standby_deadline_us = None
        self._standby_sent = False
        # Every closure, every reason, funnels through here — US2 (a new call starts
        # clean) is structural, not per-path bookkeeping.
        self.transcript = []
        self._composed_request = None
        self.direction = "inbound"
        self._outbound_callee = None
        self._outbound_message = None
        self._outbound_expires_us = None
        self._outbound_attempts = 0
        self._delivered = False

    #: How a pre-delivery closure maps onto the request's fate (contract O9). Closures
    #: not listed here mean the attempt died of a fault ('dropped', 'failure', …) —
    #: terminal 'failed', never silently retried into an unknown state.
    _OUTBOUND_CLOSURE_OUTCOMES = {  # noqa: RUF012 — class-level constant, never mutated
        "requeued": "requeue",
        "superseded": "requeue",
        "unanswered": "unanswered",
        "declined": "declined",
        "expired": "expired",
        "timeout": "unanswered",
    }

    def _close(
        self, closure: str, note: str | None, *, outcome_detail: str | None = None
    ) -> list[Effect]:
        effects: list[Effect] = []
        if self.direction == "outbound" and not self._delivered:
            # The request's fate rides ahead of the CloseEffect so the runner settles
            # (or requeues) it while the call's bookkeeping is still in hand (124 O9).
            effects.append(
                OutboundOutcomeEffect(
                    outcome=self._OUTBOUND_CLOSURE_OUTCOMES.get(closure, "failed"),
                    detail=outcome_detail,
                    attempts=self._outbound_attempts,
                )
            )
        self._reset()
        effects.append(CloseEffect(closure=closure, note=note))
        return effects

    # -- outbound calls (feature 124) ---------------------------------------------------

    #: The states in which obi is waiting on the callee's go-ahead. 'hailing' counts:
    #: an eager callee can answer while the hail's TX result is still in flight.
    _OUTBOUND_WINDOW_STATES = ("hailing", "awaiting_go", "standing_by")

    def _hail_text(self) -> str:
        return f"{self._outbound_callee}, {self._outbound_callee}, obi"

    def outbound_callee(self) -> str | None:
        """The callee whose go-ahead obi is waiting on, or None.

        The runner's probe: the loosened go-ahead shapes are computed ONLY while this
        is non-None, which is what scopes them to obi's own ack window (contract O8).
        """

        if self.direction == "outbound" and self.state in self._OUTBOUND_WINDOW_STATES:
            return self._outbound_callee
        return None

    def start_outbound(
        self, now_us: int, *, callee: str, message: str, expires_us: int
    ) -> list[Effect]:
        """Originate a call: open the exchange and hail. Legal only from ``idle`` —
        an empty return means the station is busy and the request stays queued.

        ``message`` is the operator-fixed delivery text (framed at submission,
        contract O3); the idempotent over-guard here is the belt, not the framing.
        """

        if self.state != "idle":
            return []
        self.direction = "outbound"
        self.caller = callee
        self.tier = self._tier_for(callee)
        self._outbound_callee = callee
        self._outbound_message = with_over_sign_off(message)
        self._outbound_expires_us = expires_us
        self._outbound_attempts = 0
        self._delivered = False
        self.state = "hailing"
        # A lost hail TX result must not wedge the attempt: the TTL doubles as the
        # hailing watchdog (the 093 U1 idiom, bounded by the request's own lifetime).
        self._deadline_us = expires_us
        return [
            OpenCallEffect(caller=callee, tier=self.tier, direction="outbound"),
            TransmitEffect(kind="hail", text=self._hail_text()),
        ]

    def _start_delivery(self, now_us: int) -> list[Effect]:
        """The go-ahead was heard — the ONLY entry to transmitting the payload (O2)."""

        assert self._outbound_message is not None
        self.state = "delivering"
        # 093 U1 watchdog: a lost delivery TX result force-fails via tick().
        self._deadline_us = now_us + self.call_timeout_s * 1_000_000
        return [TransmitEffect(kind="delivery", text=self._outbound_message)]

    def _on_outbound_window_rx(
        self,
        now_us: int,
        *,
        text: str,
        intelligible: bool,
        go_ahead: GoAheadDecision | None,
        addressed_elsewhere: bool,
    ) -> TransmissionOutcome:
        """A non-fired transmission heard while waiting on the callee's go-ahead.

        Precedence (D4): a matched go-ahead is consumed BEFORE the 114 read — inside
        obi's own window, "go for <callee>" is ours. Everywhere else in the file the
        114 rule runs untouched, so with no hail pending nothing changed.
        """

        if intelligible and go_ahead is not None and go_ahead.matched:
            return TransmissionOutcome(
                effects=tuple(self._start_delivery(now_us)),
                trigger_reason_override=f"outbound_go_ahead:{go_ahead.shape}",
            )
        if intelligible and is_decline(text):
            return TransmissionOutcome(
                effects=tuple(
                    self._close("declined", None, outcome_detail=f"callee declined: {text!r}")
                ),
                trigger_reason_override="outbound_declined",
            )
        if intelligible and is_standby(text):
            # FR-012: stop re-hailing, keep listening — bounded by the request TTL.
            self.state = "standing_by"
            self._deadline_us = self._outbound_expires_us
            return TransmissionOutcome(effects=(), trigger_reason_override="outbound_standby")
        if addressed_elsewhere:
            # Other stations' traffic during our window: obi's window is not the
            # channel's — keep waiting, keep the re-hail schedule (never a 114 close
            # of the outbound attempt; the payload still needs its go-ahead).
            return TransmissionOutcome(effects=(), trigger_reason_override="third_party_traffic")
        # Garble or unrelated talk: not a go-ahead (O2) — keep waiting.
        return TransmissionOutcome(effects=())

    # -- events -------------------------------------------------------------------------

    def on_transmission(
        self,
        now_us: int,
        *,
        fired: bool,
        caller: str | None,
        text: str,
        intelligible: bool,
        caller_known: bool = False,
        request_tail: str | None = None,
        addressed_elsewhere: bool = False,
        doubled: bool = False,
        go_ahead: GoAheadDecision | None = None,
    ) -> TransmissionOutcome:
        """A channel transmission was heard.

        ``go_ahead`` (feature 124) is the outbound window's read of this transmission —
        computed by the runner (which holds the roster/config), and ONLY while an
        outbound acknowledgment window is open; ``None`` everywhere else. The machine
        never parses go-ahead shapes itself, the same division of labor as
        ``addressed_elsewhere``.

        The runner filters what reaches here: kind='self' (our own bleed) and the
        deterministic no_speech verdicts (carrier/click/beep) never arrive — only real
        received traffic, intelligible or whisper-garbled.

        ``addressed_elsewhere`` (feature 114) is the channel's own etiquette read back:
        this transmission hails, or answers a hail from, a station that is not obi. The
        machine never derives it — ``radio.exchange`` does, from the wording — so this
        layer keeps no roster and no name variants.

        ``doubled`` is the trigger's report that the name was REPEATED in hail position
        ("obi, obi, …"): the channel's formal new-call opener. The machine never parses
        it either — it only uses it to tell new-call intent from a mid-call follow-up."""

        if self.state == "idle":
            if not fired:
                return TransmissionOutcome(effects=())
            self.caller = caller
            self.tier = self._tier_for(caller)
            if request_tail and intelligible:
                # One-breath call: the request rode in with the hail — skip the ack
                # entirely and compose (operator direction 2026-08-20: "go for obi"
                # invites a request already in hand; the immediate standby IS the
                # acknowledgment).
                return TransmissionOutcome(
                    effects=(
                        OpenCallEffect(caller=caller, tier=self.tier),
                        *self._start_compose(now_us, request_tail),
                    )
                )
            self.state = "acking"
            # Personalized ack ONLY on a certain roster identification (operator direction
            # 2026-08-19): "quill, go for obi". A raw/unmatched handle gets the plain ack —
            # never read a mis-transcription back at the channel.
            ack = f"{caller}, {self.ack_text}" if caller_known and caller else self.ack_text
            return TransmissionOutcome(
                effects=(
                    OpenCallEffect(caller=caller, tier=self.tier),
                    TransmitEffect(kind="ack", text=ack),
                )
            )
        if fired:
            # Same-caller re-call carrying a question (field 2026-08-19: "Obi, what is
            # the uptime of the NOC gateway?" mid-call was discarded): treat the tail as
            # the request. An anonymous re-fire counts as the same caller — the trigger
            # only emits an anonymous tail when it is clearly a question, and radio
            # carries no authentication anyway; the call keeps ITS tier (all read-only).
            # A call that never identified ANYBODY used to count as the same caller too,
            # which was never argued and is what swallowed Quill's hail into Kestrel's
            # anonymous call (field 2026-08-29, radio.db tx 3233: "ten nine, say again"
            # instead of an ack, and his next request then ran at the SAFE tier). A
            # roster-CERTAIN identification against a caller-less call is a different
            # station; an unconfirmed handle stays conservative, since the roster cannot
            # tell a second station from a mangled transcription of the first (114).
            same_caller = (
                caller is None
                or (self.caller is None and not caller_known)
                or (
                    self.caller is not None
                    and caller.strip().lower() == self.caller.strip().lower()
                )
            )
            if caller is not None and not same_caller and self.state != "delivering":
                # Feature 109 (operator rule 2026-08-28): "the window belongs to the
                # caller UNLESS someone makes another call" — a different NAMED caller's
                # full hail supersedes from ANY in-call state (was busy-swallowed outside
                # 'open'; "Obi, Obi, Mirah" mid-call went unanswered). In-flight
                # compose/TX results for the dead call are dropped by the runner's 091
                # call-id race guard. The one exception (124): mid-DELIVERY the payload
                # is already committed to the air — the hail waits out the fallthrough
                # below rather than tearing down a transmission in flight. An outbound
                # attempt superseded here requeues via _close's outcome mapping (O7).
                close_effects = self._close(
                    "superseded",
                    None,
                    outcome_detail="an inbound caller outranked the attempt",
                )
                self.caller = caller
                self.tier = self._tier_for(caller)
                if request_tail and intelligible:
                    return TransmissionOutcome(
                        effects=(
                            *close_effects,
                            OpenCallEffect(caller=caller, tier=self.tier),
                            *self._start_compose(now_us, request_tail),
                        )
                    )
                self.state = "acking"
                ack = f"{caller}, {self.ack_text}" if caller_known and caller else self.ack_text
                return TransmissionOutcome(
                    effects=(
                        *close_effects,
                        OpenCallEffect(caller=caller, tier=self.tier),
                        TransmitEffect(kind="ack", text=ack),
                    )
                )
            if self.state in self._OUTBOUND_WINDOW_STATES:
                # Feature 124: a hail to obi while obi is waiting on a go-ahead. The
                # CALLEE hailing back ("obi, obi, dana") IS the go-ahead by protocol;
                # so is any fired transmission the detector reads as one. Anyone obi
                # cannot identify is a real caller owed service (O7) — yield: close the
                # attempt (the request requeues without burning a retry) and serve the
                # inbound call exactly as from idle.
                callee = self._outbound_callee or ""
                hailed_back = caller is not None and (
                    caller.strip().lower() == callee.strip().lower()
                )
                if hailed_back or (
                    intelligible and go_ahead is not None and go_ahead.matched
                ):
                    shape = (
                        go_ahead.shape
                        if go_ahead is not None and go_ahead.matched
                        else "hail_back"
                    )
                    return TransmissionOutcome(
                        effects=tuple(self._start_delivery(now_us)),
                        trigger_reason_override=f"outbound_go_ahead:{shape}",
                    )
                close_effects = self._close(
                    "superseded",
                    None,
                    outcome_detail="a caller hailed obi during the attempt",
                )
                self.caller = caller
                self.tier = self._tier_for(caller)
                if request_tail and intelligible:
                    return TransmissionOutcome(
                        effects=(
                            *close_effects,
                            OpenCallEffect(caller=caller, tier=self.tier),
                            *self._start_compose(now_us, request_tail),
                        )
                    )
                self.state = "acking"
                ack = f"{caller}, {self.ack_text}" if caller_known and caller else self.ack_text
                return TransmissionOutcome(
                    effects=(
                        *close_effects,
                        OpenCallEffect(caller=caller, tier=self.tier),
                        TransmitEffect(kind="ack", text=ack),
                    )
                )
            if (
                self.state in ("awaiting_request", "open")
                and same_caller
                and request_tail
                and intelligible
            ):
                if caller is not None or doubled:
                    # A full re-hail is new-call intent, ALWAYS — supersede: close the
                    # old call, open a fresh one with a fresh budget, composing the
                    # carried question directly. This hatch started exhausted-only
                    # (field 2026-08-20 10:03: the budget belt strangled a live
                    # conversation) and that gating produced the 129 incident (field
                    # 2026-09-02, radio.db call 227: three "Obi, Obi, where is …"
                    # hails folded into ONE call as budget-bound follow-ups, so the
                    # 4th answer's terminal sign-off turned a clarifying question into
                    # "…that the one you mean, over and out?"). Operator ruling: a
                    # formal hail opens a new call, and a new call starts a new budget.
                    #
                    # The DOUBLED name counts on its own, with or without a callsign
                    # (field 2026-09-01, radio.db call 187: "Obi, Obi, revert the tower
                    # to the previous state" met silence). A one-breath request CANNOT
                    # carry a parsed caller — ``detect`` only fills one from a bare
                    # roster handle or the "this is <name>" form — so gating this on the
                    # callsign left the hatch dead for exactly the traffic it was written
                    # for. A SINGLE-name re-fire ("obi, one more question") stays a
                    # continuation, bound by the budget: that belt is the junk-loop stop.
                    carried_caller = caller or self.caller  # _close resets; snapshot first
                    close_effects = self._close("superseded", None)
                    # An anonymous doubled re-hail is the same station carrying on — keep
                    # the identity and tier the old call established rather than silently
                    # demoting a known caller to anonymous/safe.
                    self.caller = carried_caller
                    self.tier = self._tier_for(carried_caller)
                    return TransmissionOutcome(
                        effects=(
                            *close_effects,
                            OpenCallEffect(caller=carried_caller, tier=self.tier),
                            *self._start_compose(now_us, request_tail),
                        )
                    )
                return TransmissionOutcome(
                    effects=tuple(self._start_compose(now_us, request_tail))
                )
            if self.state == "open":
                # Post-answer, ANY fresh hail is new-call intent — the previous
                # exchange is done (field 2026-08-20 15:10: "Obi, Obi, Rook" right
                # after another caller's answer was busy-rejected and went unanswered).
                close_effects = self._close("superseded", None)
                self.caller = caller
                self.tier = self._tier_for(caller)
                if request_tail and intelligible:
                    return TransmissionOutcome(
                        effects=(
                            *close_effects,
                            OpenCallEffect(caller=caller, tier=self.tier),
                            *self._start_compose(now_us, request_tail),
                        )
                    )
                self.state = "acking"
                ack = f"{caller}, {self.ack_text}" if caller_known and caller else self.ack_text
                return TransmissionOutcome(
                    effects=(
                        *close_effects,
                        OpenCallEffect(caller=caller, tier=self.tier),
                        TransmitEffect(kind="ack", text=ack),
                    )
                )
            if (
                self.state in ("composing", "answering")
                and same_caller
                and request_tail
                and intelligible
            ):
                # Feature 109 D3.2 (radio.db call 47: the re-hail carrying the request
                # was swallowed mid-compose): hold it — ONE slot, latest wins — and
                # compose it when the in-flight answer settles. Radio pacing never
                # produces two outstanding requests; a queue would invite ordering bugs
                # the channel can't even express. A FORMAL hail (doubled or callsign)
                # is remembered as new-call intent (129): the drain opens a fresh call
                # rather than spending this one's budget — obi finishes what it is
                # saying, then serves the new call.
                self._pending_request = request_tail
                self._pending_new_call = caller is not None or doubled
                self._pending_caller = caller
                return TransmissionOutcome(
                    effects=(), trigger_reason_override="held_for_settle"
                )
            if self.state == "awaiting_request" and same_caller:
                # Feature 109 D3.1 (radio.db call 75: "Obi, NASA, power, blue" drew
                # 25 s of dead air): addressed, mid-call, nothing extractable — the
                # canned say-again, never silence toward the call's own caller. The
                # once-per-call budget is shared with the garble path (093 rules).
                if self.say_again_used:
                    return TransmissionOutcome(
                        effects=tuple(
                            self._garble_close(
                                now_us,
                                f"radio call from {self.caller or 'unknown'} closed: "
                                "addressed transmission with no extractable request "
                                "after 'say again' was already spent.",
                            )
                        ),
                        trigger_reason_override="say_again_no_tail",
                    )
                self.say_again_used = True
                return TransmissionOutcome(
                    effects=(TransmitEffect(kind="say_again", text=self.say_again_text),),
                    trigger_reason_override="say_again_no_tail",
                )
            # FR-013: one call at a time — logged for the shadow record, never acked.
            return TransmissionOutcome(effects=(), trigger_reason_override="busy_with_call")
        if self.state in self._OUTBOUND_WINDOW_STATES:
            # Feature 124: everything heard inside obi's own ack window routes through
            # the outbound read — BEFORE the 114 branch below, because "go for <callee>"
            # answering OUR hail is ours (D4); outside these states nothing changed.
            return self._on_outbound_window_rx(
                now_us,
                text=text,
                intelligible=intelligible,
                go_ahead=go_ahead,
                addressed_elsewhere=addressed_elsewhere,
            )
        if self.state == "delivering":
            # The payload is committed and owed to the callee (like composing/answering,
            # results land via on_tx_result) — nothing heard now changes that.
            return TransmissionOutcome(effects=())
        if addressed_elsewhere:
            # Feature 114 (field 2026-08-29, radio.db call 118): the window belongs to the
            # caller, NOT to the whole channel. A hail — or an answer to one — naming
            # another station is other stations' traffic: obi transmits nothing and stands
            # down. Reached only when the trigger did not fire, so obi's own name at the
            # start still belongs to the paths above whatever the wording looked like
            # ("Hopi, hopi, Fennec." is a mangled hail TO obi, not traffic to sit out).
            if self.state in ("awaiting_request", "open"):
                return TransmissionOutcome(
                    effects=tuple(self._close("third_party", None)),
                    trigger_reason_override="third_party_traffic",
                )
            # acking/composing/answering: something is already in flight and owed to the
            # caller who asked for it, and closing would cancel it (the runner's 091
            # call-id guard drops results belonging to a closed call). Record why obi
            # stayed quiet; the window closes on the next such transmission, or expires.
            return TransmissionOutcome(
                effects=(), trigger_reason_override="third_party_traffic"
            )
        if self.state in ("composing", "answering"):
            # The caller can always call it off, even while obi is mid-flow — a swallowed
            # "channel clear" left a call stuck open on the air (field, 2026-08-19). The
            # in-flight compose/tx result lands on an idle machine and is dropped.
            if intelligible and _is_clearing(text):
                return TransmissionOutcome(effects=tuple(self._close("cleared", None)))
            return TransmissionOutcome(effects=())
        if self.state not in ("awaiting_request", "open"):
            return TransmissionOutcome(effects=())
        if self.state == "open":
            # Feature 109 (operator decision 2026-08-28): post-answer the WINDOW BELONGS
            # TO THE CALLER — every intelligible transmission composes unless it is a
            # clearing, a sign-off, or garble. The old request-marker allowlist dropped
            # 12+ real follow-ups in three days once feature 103/107 turned callers
            # imperative ("Set portal to 100%", "Yes, set the effect to dancing
            # shadows", the verbless "Just for status of…"). The accepted cost — the old
            # 2026-08-20 14:09 failure mode, chatter ABOUT obi getting answered inside a
            # window — is bounded by the per-call answer budget and window expiry; the
            # idle channel still requires a hail.
            if intelligible and (
                _is_final_clear(text)
                or (not self._answer_expects_reply and _is_bare_ack(text))
            ):
                return TransmissionOutcome(effects=tuple(self._close("cleared", None)))
            if intelligible and _is_sign_off(text, self.sign_offs):
                return TransmissionOutcome(
                    effects=tuple(self._close("cleared", None)),
                    trigger_reason_override="sign_off_close",
                )
            if not intelligible:
                # A garbled transmission right after our answer is most likely the
                # caller following up (operator direction 2026-08-20 15:27: "great
                # candidate for 10-9") — say again once, and re-arm the request
                # expectation so the repeat composes; a second garble closes ON the
                # air (093 FR-011 — the caller is live and engaged, never strand them).
                if self.say_again_used:
                    return TransmissionOutcome(
                        effects=tuple(
                            self._garble_close(
                                now_us,
                                f"radio call from {self.caller or 'unknown'} closed: "
                                "post-answer garble after 'say again' was already spent.",
                            )
                        )
                    )
                self.say_again_used = True
                self.state = "awaiting_request"
                return TransmissionOutcome(
                    effects=(TransmitEffect(kind="say_again", text=self.say_again_text),)
                )
            return TransmissionOutcome(
                effects=tuple(self._start_compose(now_us, text)),
                trigger_reason_override="in_window_composed",
            )
        if not intelligible:
            if self.say_again_used:
                return TransmissionOutcome(
                    effects=tuple(
                        self._garble_close(
                            now_us,
                            f"radio call from {self.caller or 'unknown'} closed: two "
                            "unintelligible requests in a row (said 'say again' once).",
                        )
                    )
                )
            self.say_again_used = True
            return TransmissionOutcome(
                effects=(TransmitEffect(kind="say_again", text=self.say_again_text),)
            )
        if _is_final_clear(text) or (
            not self._answer_expects_reply and _is_bare_ack(text)
        ):
            return TransmissionOutcome(effects=tuple(self._close("cleared", None)))
        if _is_sign_off(text, self.sign_offs):
            # The caller got the ack and then left ("moving to the gate") — close, do
            # not compose an answer at a departure (feature 109, D2).
            return TransmissionOutcome(
                effects=tuple(self._close("cleared", None)),
                trigger_reason_override="sign_off_close",
            )
        return TransmissionOutcome(effects=tuple(self._start_compose(now_us, text)))

    def _start_compose(self, now_us: int, request: str) -> list[Effect]:
        """Enter composing — or close SILENTLY when the per-call answer budget is spent."""

        if self._answers_sent >= self.max_answers_per_call:
            return self._close(
                "abandoned",
                f"radio call from {self.caller or 'unknown'}: per-call answer budget "
                f"({self.max_answers_per_call}) exhausted — staying silent; the caller "
                "has likely moved on.",
            )
        self.state = "composing"
        self._composed_request = request
        self._answer_expects_reply = False  # the flag belongs to ONE answer (109 US4)
        self._deadline_us = now_us + self.compose_deadline_s * 1_000_000
        # One standby per COMPOSE, not per call (field 2026-08-20 15:59: a follow-up
        # question ran 18 s of tool calls in silence — once-per-call predates
        # standby-as-acknowledgment). Every consumed request draws its own "stand by".
        self._standby_sent = False
        self._standby_deadline_us = now_us + self.standby_after_s * 1_000_000
        return [
            ComposeEffect(
                request=request,
                caller=self.caller,
                tier=self.tier,
                history=tuple(self.transcript),
            )
        ]

    def on_compose_result(
        self, now_us: int, text: str | None, *, expects_reply: bool = False
    ) -> list[Effect]:
        """The model produced an answer (or ``None`` on failure). Late results are dropped.

        ``expects_reply`` is the composer's advisory flag (feature 109 US4, parsed from
        the scrubbed ``[expects-reply]`` marker): absent ⇒ exactly the pre-109 walk.
        """

        if self.state != "composing":
            return []  # deadline already fired via tick(), or the call closed underneath
        if text is None:
            return self._compose_failed(now_us)
        self.state = "answering"
        self._answer_closes_with = None
        # An answer that itself asks the caller something ("say again on that third
        # name?") solicits a reply — remembered so the post-answer state expects it.
        # The composer's explicit flag counts as soliciting too (109 US4).
        self._answer_expects_reply = expects_reply
        self._answer_solicits = expects_reply or bool(
            re.search(r"\?|\bsay again\b", text, re.IGNORECASE)
        )
        self._deadline_us = None
        self._answers_sent += 1
        if self._answers_sent >= self.max_answers_per_call:
            # This answer spends the budget: a follow-up would meet the silent
            # 'abandoned' close, so "over" would be a false promise (FR-012).
            text = _with_terminal_sign_off(text)
        return [TransmitEffect(kind="answer", text=text)]

    def _compose_failed(self, now_us: int) -> list[Effect]:
        # The caller heard "go for obi"; silence now would strand them (FR-012).
        self.state = "answering"
        self._answer_closes_with = "failure"
        self._close_note = (
            f"radio call from {self.caller or 'unknown'}: compose failed; "
            "transmitted the canned unavailability phrase."
        )
        # 093 watchdog (analysis U1): a lost TX result must not wedge the call open —
        # tick() force-closes once the call timeout passes with nothing delivered.
        self._deadline_us = now_us + self.call_timeout_s * 1_000_000
        # 093 FR-008: the call closes right after this transmission — it signs off.
        # (The shipped default already ends "…obi out"; this is the custom-config belt.)
        return [
            TransmitEffect(kind="unavailability", text=_with_out_sign_off(self.unavailable_text))
        ]

    def _garble_close(self, now_us: int, note: str) -> list[Effect]:
        """FR-011 (093): close the garbled exchange ON the air, not in silence.

        The CloseEffect is deferred to ``on_tx_result`` — the runner's 091 race guard
        never keys RF for an already-closed call, so a [Transmit, Close] effect pair
        would silently drop the phrase (research D2). The call-timeout deadline doubles
        as the watchdog: a lost TX result force-closes via ``tick()`` (analysis U1).
        """

        self.state = "answering"
        self._answer_closes_with = "garble"
        self._close_note = note
        self._deadline_us = now_us + self.call_timeout_s * 1_000_000
        return [
            TransmitEffect(kind="garble_close", text=_with_out_sign_off(self.garble_close_text))
        ]

    def on_tx_result(
        self, now_us: int, *, kind: str, disposition: str, text: str | None = None
    ) -> list[Effect]:
        """The TX gate resolved one of our TransmitEffects.

        ``text`` is what actually AIRED (``TxOutcome.text``, post-truncation) — a
        successful answer pairs it with its request as a transcript turn (feature 091).
        ``None`` (a legacy caller) simply skips the append."""

        ok = disposition in _SUCCESS_DISPOSITIONS
        if self.state == "acking" and kind == "ack":
            if not ok:
                return self._close(
                    "dropped",
                    f"radio ack to {self.caller or 'unknown'} dropped ({disposition}) — "
                    "channel never cleared within patience.",
                )
            if self._pending_request is not None:
                request = self._pending_request
                self._pending_request = None
                return self._start_compose(now_us, request)
            self.state = "awaiting_request"
            self._deadline_us = now_us + self.call_timeout_s * 1_000_000
            return []
        if kind == "standby":
            return []  # a courtesy aside — the compose in flight is undisturbed either way
        if kind == "say_again":
            if not ok:
                return self._close("dropped", f"radio 'say again' dropped ({disposition}).")
            self._deadline_us = now_us + self.call_timeout_s * 1_000_000
            return []
        if kind == "hail":
            if self.state != "hailing":
                return []  # superseded, or the callee answered before the result landed
            if not ok:
                if disposition == "dropped_busy":
                    # The CHANNEL never cleared — not the callee's silence. Yield; the
                    # request requeues without burning a retry (O6).
                    return self._close(
                        "requeued",
                        None,
                        outcome_detail="channel busy — the hail never aired",
                    )
                return self._close(
                    "dropped",
                    f"radio outbound hail to {self._outbound_callee or 'unknown'} "
                    f"blocked ({disposition}).",
                    outcome_detail=f"hail blocked ({disposition})",
                )
            self.state = "awaiting_go"
            self._deadline_us = now_us + self.outbound_ack_window_s * 1_000_000
            return []
        if kind == "delivery":
            if self.state != "delivering":
                return []
            if not ok:
                return self._close(
                    "dropped",
                    f"radio outbound delivery to {self._outbound_callee or 'unknown'} "
                    f"dropped ({disposition}) AFTER a go-ahead — the callee is waiting "
                    "on a message that never aired.",
                    outcome_detail=f"delivery dropped ({disposition})",
                )
            # Delivered: settle the request (the call stays open — contract O10) and
            # become a normal open call. The delivered text seeds the transcript so a
            # follow-up composes with the exchange's actual context (091 shape).
            self._delivered = True
            attempts = self._outbound_attempts
            if text:
                self.transcript.append(
                    Turn(
                        request=f"(obi delivered the operator's message to {self.caller})",
                        answer=text,
                    )
                )
                del self.transcript[: -self.max_history_turns or None]
            self._answer_solicits = False
            self._answer_expects_reply = False
            self.state = "open"
            self._deadline_us = now_us + self.call_timeout_s * 1_000_000
            return [OutboundOutcomeEffect(outcome="delivered", attempts=attempts)]
        if self.state == "answering" and kind in ("answer", "unavailability", "garble_close"):
            if self._answer_closes_with == "garble" or kind == "garble_close":
                # FR-012 (093): the close depends on neither the phrase airing nor the
                # disposition — any resolution of the closing transmission closes.
                return self._close("garble", self._close_note)
            if self._answer_closes_with == "failure" or kind == "unavailability":
                return self._close(
                    "failure",
                    self._close_note
                    or f"radio call from {self.caller or 'unknown'}: compose failed; "
                    "transmitted the canned unavailability phrase.",
                )
            if not ok:
                return self._close(
                    "dropped",
                    f"radio answer to {self.caller or 'unknown'} dropped ({disposition}) — "
                    "channel never cleared within patience.",
                )
            if self._composed_request is not None and text:
                # The turn is complete: what they asked, what they HEARD (FR-005).
                self.transcript.append(Turn(request=self._composed_request, answer=text))
                del self.transcript[: -self.max_history_turns or None]
                self._composed_request = None
            if self._pending_request is not None:
                # Feature 109 D3.2: the request held while this answer was in flight
                # composes now — the caller re-issued mid-compose and is owed an answer.
                request = self._pending_request
                self._pending_request = None
                if self._pending_new_call:
                    # Feature 129: the held request arrived as a FORMAL hail — the same
                    # supersede the immediate path gives it: close this call, open a
                    # fresh one (fresh budget), compose there. Identity carries per the
                    # immediate hatch: an explicit callsign re-states it, an anonymous
                    # doubled re-hail keeps what this call established.
                    carried_caller = self._pending_caller or self.caller
                    close_effects = self._close("superseded", None)
                    self.caller = carried_caller
                    self.tier = self._tier_for(carried_caller)
                    return [
                        *close_effects,
                        OpenCallEffect(caller=carried_caller, tier=self.tier),
                        *self._start_compose(now_us, request),
                    ]
                return self._start_compose(now_us, request)
            # When OUR answer solicited a reply, the next transmission is the caller's
            # response by protocol — hold the request-expectation state (field
            # 2026-08-20 14:29: the call closed 'complete' on the clarification obi
            # itself asked for). A declarative answer leaves the ball in their court.
            self.state = "awaiting_request" if self._answer_solicits else "open"
            self._deadline_us = now_us + self.call_timeout_s * 1_000_000
            return []
        return []

    def tick(self, now_us: int) -> list[Effect]:
        """Advance time: standby courtesy, call-timeout (silent), compose deadline."""

        # The radio "thinking…": composing is running long — tell the caller ONCE per
        # call to stand by (canned, no model), then keep composing.
        if (
            self.state == "composing"
            and not self._standby_sent
            and self._standby_deadline_us is not None
            and now_us >= self._standby_deadline_us
        ):
            self._standby_sent = True
            self._standby_deadline_us = None
            return [TransmitEffect(kind="standby", text=self.standby_text)]
        if self._deadline_us is None or now_us < self._deadline_us:
            return []
        if self.state == "awaiting_go":
            # An ack window elapsed with no go-ahead — the ONLY event that burns a
            # retry (O6). Re-hail while budget and TTL both allow; otherwise close
            # honestly, transmitting nothing (the payload never airs blind — O2).
            expires_us = self._outbound_expires_us or 0
            self._outbound_attempts += 1
            if self._outbound_attempts <= self.outbound_max_rehails:
                if now_us >= expires_us:
                    return self._close(
                        "expired",
                        None,
                        outcome_detail="request time-to-live expired mid-attempt",
                    )
                self.state = "hailing"
                self._deadline_us = expires_us  # the TTL doubles as the hailing watchdog
                return [TransmitEffect(kind="hail", text=self._hail_text())]
            return self._close(
                "unanswered",
                None,
                outcome_detail=f"no go-ahead after {self._outbound_attempts} hails",
            )
        if self.state in ("hailing", "standing_by"):
            # The request TTL expired: a hail result that never came back (the 093 U1
            # watchdog shape), or a standby that never resolved into a go-ahead.
            detail = (
                "standby never resolved into a go-ahead before the time-to-live"
                if self.state == "standing_by"
                else "request time-to-live expired before the hail resolved"
            )
            return self._close("expired", None, outcome_detail=detail)
        if self.state == "delivering":
            # 093 U1: the delivery's TX result never arrived — close honestly rather
            # than wedge; whether the payload aired is in the outbound record.
            return self._close(
                "dropped",
                f"radio outbound delivery to {self._outbound_callee or 'unknown'}: "
                "the TX result never arrived.",
                outcome_detail="delivery result lost",
            )
        if self.state in ("awaiting_request", "open"):
            # FR-010: silent expiry — a CloseEffect and nothing on the air.
            return self._close("timeout", None)
        if self.state == "composing":
            return self._compose_failed(now_us)
        if self.state == "answering" and self._answer_closes_with in ("garble", "failure"):
            # 093 watchdog (analysis U1): the closing transmission's result never came
            # back — close with the pending closure and note, transmitting nothing.
            assert self._answer_closes_with is not None
            return self._close(self._answer_closes_with, self._close_note)
        return []
