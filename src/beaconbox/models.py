"""What the API returns, as frozen dataclasses.

Three properties every model here has, and the reasons for them:

* **Frozen.** A response is a fact about a moment. Mutating it in application code produces an
  object that claims to be an API answer and is not, and that object then gets logged.
* **``.raw``.** Every model keeps the undecoded payload. A field the API adds tomorrow is
  readable by an SDK built today, so an integration is never blocked on an SDK release.
* **Tolerant.** Unknown enum values fall back to plain strings and unknown keys are ignored
  rather than raising (see :func:`beaconbox.enums.coerce`). A client library that crashes on a
  field it did not expect turns an additive API change into a merchant's outage.

Timestamps are timezone-aware :class:`datetime.datetime` objects in UTC.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from typing import Any

from .enums import (
    Channel,
    ChannelSendStatus,
    MessageKind,
    MessageStatus,
    OrderStatus,
    RecipientStatus,
    SmsConsentSource,
    WebhookEventType,
    WhatsAppReplyForward,
    coerce,
)

__all__ = [
    "ApiKey",
    "BatchItemResult",
    "BatchResult",
    "CreditBalance",
    "DeliveryStatus",
    "EmailOutcome",
    "Failed",
    "Message",
    "MessagePage",
    "MessagePush",
    "MessagePushResult",
    "NewApiKey",
    "NewWebhookEndpoint",
    "NotSent",
    "RecipientErasureReport",
    "RecipientSms",
    "RetractResult",
    "SmsDelivery",
    "SmsOutcome",
    "WebhookEndpoint",
    "WebhookEvent",
    "WebhookTestResult",
    "WhatsAppErasureReceipt",
    "WhatsAppOptInOutcome",
    "WhatsAppOutcome",
]

Payload = dict[str, Any]


#: The range a number in this API can occupy. Named rather than written inline because
#: :func:`_opt_number` checks it twice, once per numeric type, and the two have to mean the same
#: thing.
_INT64_MAX = 2**63 - 1
_INT64_MIN = -(2**63)


def _text(value: Any, default: str = "") -> str:
    """A string field, or ``default`` when the API sent something else.

    The default is an obviously empty value a caller can test, rather than a plausible-looking
    string they cannot: a required field arriving as JSON ``null`` must not become the text
    ``"None"`` and travel on into the next call as an id.
    """
    return value if isinstance(value, str) else default


def _opt_text(value: Any) -> str | None:
    """An optional string field. Anything that is not a string reads as absent.

    Every field built through this is annotated ``str | None``, and this is what keeps that
    promise: a numerically typed ``skipped_reason`` reads as ``None`` rather than being handed to
    a caller who would compare it against :class:`~beaconbox.enums.SkipReason` and never match.
    """
    return value if isinstance(value, str) else None


def _flag(value: Any, default: bool = False) -> bool:
    """A boolean field, or ``default`` when the API sent something else.

    Read strictly rather than through Python's truthiness: a non-boolean in a boolean field is a
    corrupt response, and ``nudged=True`` inferred from the truthiness of a string is a claim the
    API never made.
    """
    return value if isinstance(value, bool) else default


def _number(value: Any, default: int = 0) -> int:
    """An integer field, or ``default`` when the API sent something else.

    An integral ``float`` is accepted because JSON has one number type: a proxy or a language
    boundary that re-serialises ``1`` as ``1.0`` must not silently turn one credit into zero. A
    non-integral float is not coerced, since a truncated ``1.5`` is its own kind of wrong. A
    ``bool`` is not read as a number either: ``True`` being an ``int`` is a fact about Python
    rather than anything the API said.
    """
    coerced = _opt_number(value)
    return coerced if coerced is not None else default


def _opt_number(value: Any) -> int | None:
    """Like :func:`_number`, but an unusable value reads as absent rather than as a number.

    For a field where ``0`` is itself a real answer and "could not be determined" is a different
    one. :attr:`WebhookTestResult.status_code` is exactly that: a ``0`` there would read as an
    HTTP status.

    NaN and infinity are rejected rather than converted. Both are reachable from a JSON body
    (:func:`json.loads` accepts the literals), and ``int()`` raises on either, which would make
    this parse layer fatal on a malformed response in a module whose whole contract is that it is
    not.

    A value outside signed 64-bit range is rejected too. Python's integers are unbounded, so
    nothing here would fail on one: ``{"credits": 1e300}`` would read as a 300-digit number and
    travel on into a merchant's own arithmetic, formatting and database. No count this API reports
    can exceed an ``int64``, so one that does is a corrupt response rather than a large answer, and
    the bound is also what keeps this field-for-field identical to the PHP client, whose integers
    stop there.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if _INT64_MIN <= value <= _INT64_MAX else None
    if isinstance(value, float) and value.is_integer():
        # Strict inequalities, and both matter. `float(_INT64_MAX)` rounds *up* to 2**63, so an
        # inclusive upper bound admits a float one larger than any int64 and `int()` then returns
        # it. `float(_INT64_MIN)` is exact, so the lower bound excludes only the value itself,
        # which is the same trade the PHP client makes for the same reason.
        return int(value) if float(_INT64_MIN) < value < float(_INT64_MAX) else None
    return None


def _parse_datetime(value: Any) -> datetime | None:
    """RFC 3339 to an aware datetime, or ``None``.

    Hand-rolled rather than ``datetime.fromisoformat`` alone because that function did not accept
    a trailing ``Z`` until Python 3.11, and this SDK supports 3.10. A naive result is stamped
    UTC: the API only ever sends UTC, and a naive datetime compared against an aware one raises
    at some later, less obvious call site.
    """
    if not isinstance(value, str) or not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _require_datetime(value: Any) -> datetime:
    """For a field the API declares required. Falls back to the epoch rather than raising."""
    return _parse_datetime(value) or datetime.fromtimestamp(0, tz=timezone.utc)


@dataclass(frozen=True)
class EmailOutcome:
    """The email side of a push: whether the nudge is expected to go, and why not if it is not.

    **Its own type, like :class:`SmsOutcome` and :class:`WhatsAppOutcome`, because suppression is
    per channel.** A recipient who texted STOP has suppressed SMS and nothing else; a hard bounce
    suppresses email and nothing else. Reading one channel's reason off another's block would tell
    you a channel is blocked when it is not.

    ``sending=False`` is reliable — no email will be sent. ``sending=True`` is a strong
    expectation rather than a promise: it is the verdict as known when you called, and it cannot
    see the day's cap filling up between now and the send. Check
    :attr:`Message.delivery` or subscribe to ``message.not_sent`` for what actually happened.
    """

    sending: bool
    skipped_reason: str | None = None
    """An :class:`~beaconbox.enums.EmailSkipReason` value, or ``None`` when sending.

    Treat an unrecognised value as "not sending" rather than as an error — the list grows."""
    raw: Payload = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, payload: Payload) -> EmailOutcome:
        return cls(
            sending=_flag(payload.get("sending")),
            skipped_reason=_opt_text(payload.get("skipped_reason")),
            raw=payload,
        )


@dataclass(frozen=True)
class WebhookTestResult:
    """What a manual test POST to one of your endpoints did.

    ``delivered`` is the field to read. The call itself succeeds whatever your endpoint answered —
    the failure being reported is your server's, not BeaconBox's, so raising would describe the
    wrong hop.
    """

    delivered: bool
    status_code: int | None = None
    """What your endpoint answered. ``None`` when it could not be reached at all."""
    error: str | None = None
    """What went wrong, in words meant for a person."""
    duration_ms: int = 0
    raw: Payload = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, payload: Payload) -> WebhookTestResult:
        return cls(
            delivered=_flag(payload.get("delivered")),
            status_code=_opt_number(payload.get("status_code")),
            error=_opt_text(payload.get("error")),
            duration_ms=_number(payload.get("duration_ms")),
            raw=payload,
        )


@dataclass(frozen=True)
class SmsOutcome:
    """The SMS side of a push, and the *only* way an SMS problem is ever reported.

    ``queued=False`` with a ``skipped_reason`` is a normal, successful outcome. The message is in
    the recipient's inbox and the email nudge went out regardless. A push never fails because of
    SMS, so reporting it as a failed request would misdescribe what happened and invite a retry
    that stores the update twice.

    ``queued=False`` with *no* ``skipped_reason`` and an ``escalates_at`` is a third thing again:
    nothing was refused and nothing has been charged, the send is simply armed for later.
    """

    queued: bool
    credits: int
    """What this send will cost when a carrier accepts it. Charged at send time, not now."""

    skipped_reason: str | None = None
    """A :class:`~beaconbox.enums.SkipReason` value, or ``None`` when nothing was refused."""

    escalates_at: datetime | None = None
    """Set when ``escalate_if_unread_after_minutes`` armed this channel instead of sending it.
    At this moment BeaconBox checks whether the recipient has opened the message, and sends only
    if they have not."""

    raw: Payload = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, payload: Payload) -> SmsOutcome:
        return cls(
            queued=_flag(payload.get("queued")),
            credits=_number(payload.get("credits")),
            skipped_reason=_opt_text(payload.get("skipped_reason")),
            escalates_at=_parse_datetime(payload.get("escalates_at")),
            raw=payload,
        )


@dataclass(frozen=True)
class WhatsAppOutcome:
    """The WhatsApp side of a push. Same contract as :class:`SmsOutcome`: reported, never
    raised."""

    queued: bool
    credits: int
    skipped_reason: str | None = None
    sms_fallback: bool = False
    """Whether an SMS is armed to follow if this WhatsApp message proves undeliverable, or
    queued at once because WhatsApp cannot carry it (``not_reachable``, or ``template_not_sendable``
    because the push lacks ``reference`` or ``order_status``, or the reference or business name
    cannot be shown). Either way it is a separate send with its own credit: poll the message to
    see it."""
    escalates_at: datetime | None = None
    raw: Payload = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, payload: Payload) -> WhatsAppOutcome:
        return cls(
            queued=_flag(payload.get("queued")),
            credits=_number(payload.get("credits")),
            skipped_reason=_opt_text(payload.get("skipped_reason")),
            sms_fallback=_flag(payload.get("sms_fallback")),
            escalates_at=_parse_datetime(payload.get("escalates_at")),
            raw=payload,
        )


@dataclass(frozen=True)
class WhatsAppOptInOutcome:
    """What the push's ``whatsapp_opt_in`` did.

    ``recorded=False`` is an ordinary success in three of its four shapes: consent was already on
    file, the number clashed with another recipient's, or the recipient has withdrawn. The last
    is an answer rather than a failure, and a durable one: an opt-out is permanent for your
    business.

    Read :attr:`status` rather than inferring state from :attr:`skipped_reason`. It is this
    recipient's consent *after* the push.
    """

    recorded: bool
    status: RecipientStatus | str
    skipped_reason: str | None = None
    raw: Payload = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, payload: Payload) -> WhatsAppOptInOutcome:
        return cls(
            recorded=_flag(payload.get("recorded")),
            status=coerce(RecipientStatus, payload.get("status")),
            skipped_reason=_opt_text(payload.get("skipped_reason")),
            raw=payload,
        )


@dataclass(frozen=True)
class MessagePushResult:
    """What a push returns.

    **Read this object, not the status code.** A push answers 201 even when the SMS or WhatsApp
    message was not sent, because the update is already in the inbox and the email has gone. The
    paid channel reports itself in :attr:`sms` and :attr:`whatsapp`.
    """

    id: str
    subject: str
    kind: MessageKind | str
    status: MessageStatus | str
    created: bool
    """True if a new message was created, False if an existing one was updated in place."""
    nudged: bool
    """Whether a nudge was queued. Delivery and bounces arrive later, by webhook."""
    created_at: datetime
    updated_at: datetime
    scheduled_for: datetime | None = None
    """When a deferred nudge will be sent, if ``send_at`` held it back."""
    email: EmailOutcome | None = None
    """Present when an email nudge was queued. ``None`` when none was — ``notify=False``, or an
    update that does not re-nudge — because there is then no email outcome to report."""
    sms: SmsOutcome | None = None
    """Present when SMS was in play, queued or explicitly refused. ``None`` when the account has
    SMS off and this push did not ask for it."""
    sms_units: int = 0
    whatsapp: WhatsAppOutcome | None = None
    whatsapp_opt_in: WhatsAppOptInOutcome | None = None
    """Present exactly when you sent ``whatsapp_opt_in``. Top-level rather than inside
    :attr:`whatsapp` because recording consent is not sending, and an account collecting consent
    ahead of switching the channel on has no :attr:`whatsapp` block at all."""
    reference: str | None = None
    recipient_email: str | None = None
    """The recipient this message was stored for, as BeaconBox normalised it (trimmed, lower
    case). ``None`` only when an idempotent replay returns a response recorded before the API
    started sending it."""
    raw: Payload = field(default_factory=dict, repr=False, compare=False)
    order_status: OrderStatus | str | None = None
    """The ``order_status`` the push carried, or ``None`` when it carried none. A value this SDK
    does not know stays a plain string. Last, so positional construction is not shifted by it."""

    @classmethod
    def from_api(cls, payload: Payload) -> MessagePushResult:
        email = payload.get("email")
        sms = payload.get("sms")
        whatsapp = payload.get("whatsapp")
        opt_in = payload.get("whatsapp_opt_in")
        order_status = payload.get("order_status")
        return cls(
            id=_text(payload.get("id")),
            subject=_text(payload.get("subject")),
            kind=coerce(MessageKind, payload.get("kind")),
            status=coerce(MessageStatus, payload.get("status")),
            created=_flag(payload.get("created")),
            nudged=_flag(payload.get("nudged")),
            created_at=_require_datetime(payload.get("created_at")),
            updated_at=_require_datetime(payload.get("updated_at")),
            scheduled_for=_parse_datetime(payload.get("scheduled_for")),
            email=EmailOutcome.from_api(email) if isinstance(email, dict) else None,
            sms=SmsOutcome.from_api(sms) if isinstance(sms, dict) else None,
            sms_units=_number(payload.get("sms_units")),
            whatsapp=WhatsAppOutcome.from_api(whatsapp) if isinstance(whatsapp, dict) else None,
            whatsapp_opt_in=(
                WhatsAppOptInOutcome.from_api(opt_in) if isinstance(opt_in, dict) else None
            ),
            reference=_opt_text(payload.get("reference")),
            order_status=(
                coerce(OrderStatus, order_status) if isinstance(order_status, str) else None
            ),
            recipient_email=_opt_text(payload.get("recipient_email")),
            raw=payload,
        )


@dataclass(frozen=True)
class BatchItemResult:
    """One item's outcome inside a batch. Items succeed and fail independently."""

    index: int
    """Position in the request you sent, so results can be matched back up."""
    ok: bool
    result: MessagePushResult | None = None
    error_code: str | None = None
    detail: str | None = None
    raw: Payload = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, payload: Payload) -> BatchItemResult:
        result = payload.get("result")
        return cls(
            index=_number(payload.get("index")),
            ok=_flag(payload.get("ok")),
            result=MessagePushResult.from_api(result) if isinstance(result, dict) else None,
            error_code=_opt_text(payload.get("error_code")),
            detail=_opt_text(payload.get("detail")),
            raw=payload,
        )


@dataclass(frozen=True)
class BatchResult:
    """What a batch returns. **HTTP 200 whenever the batch is processed: check :attr:`failed`,
    not the status code.**

    An item that failed is reported, not raised. One bad recipient must not discard the
    forty-nine good pushes alongside it, and a 4xx for the whole call would invite a retry of all
    fifty. The one refusal of the whole batch is a 409 ``idempotency.request_in_progress``, raised
    as :class:`~beaconbox.errors.ConflictError` while the same key is still running it: retry the
    whole batch later with the same key, and items already sent are replayed.
    """

    items: tuple[BatchItemResult, ...]
    succeeded: int
    failed: int
    raw: Payload = field(default_factory=dict, repr=False, compare=False)

    @property
    def failures(self) -> tuple[BatchItemResult, ...]:
        """Just the items that did not go through, for the usual "log what broke" loop."""
        return tuple(item for item in self.items if not item.ok)

    @classmethod
    def from_api(cls, payload: Payload) -> BatchResult:
        items = payload.get("items")
        return cls(
            items=tuple(
                BatchItemResult.from_api(item)
                for item in (items if isinstance(items, list) else [])
                if isinstance(item, dict)
            ),
            succeeded=_number(payload.get("succeeded")),
            failed=_number(payload.get("failed")),
            raw=payload,
        )


@dataclass(frozen=True)
class SmsDelivery:
    """The SMS side of a message's delivery, present only when one was queued."""

    status: ChannelSendStatus | str
    credits_charged: int
    skipped_reason: str | None = None
    submitted_at: datetime | None = None
    last_status_at: datetime | None = None
    raw: Payload = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, payload: Payload) -> SmsDelivery:
        return cls(
            status=coerce(ChannelSendStatus, payload.get("status")),
            credits_charged=_number(payload.get("credits_charged")),
            skipped_reason=_opt_text(payload.get("skipped_reason")),
            submitted_at=_parse_datetime(payload.get("submitted_at")),
            last_status_at=_parse_datetime(payload.get("last_status_at")),
            raw=payload,
        )


@dataclass(frozen=True)
class NotSent:
    """Set when BeaconBox decided not to email a message at all, and that decision still stands.

    **The field that makes ``delivered is False`` readable.** On its own that flag covers two
    opposite situations, *on its way* and *never attempted, and never will be*; this is what tells
    them apart, so a caller polling for delivery knows when to stop. ``None`` is the ordinary case.

    :attr:`reason` is a plain string rather than an enum, unlike :class:`SmsDelivery`'s
    ``skipped_reason`` companion. The server's list grows whenever a refusal is added to the send
    path, and an SDK that raised on an unrecognised value would turn a new server-side reason into
    a client-side crash. Compare against :class:`~beaconbox.enums.EmailSkipReason` for the known
    ones and treat anything else as "not sent".
    """

    reason: str
    at: datetime | None = None
    raw: Payload = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, payload: Payload) -> NotSent:
        return cls(
            reason=_text(payload.get("reason")),
            at=_parse_datetime(payload.get("at")),
            raw=payload,
        )


@dataclass(frozen=True)
class Failed:
    """Set when an email was attempted and BeaconBox has given up on it.

    :class:`NotSent`'s counterpart at the other end of the send path. That one means BeaconBox
    decided not to try; this one means it tried, and the email provider refused the message, every
    retry failed, or the provider's answer was lost (``outcome_unknown``: it may still arrive, and
    is never sent again). A caller polling for delivery can stop waiting, except on
    ``outcome_unknown``: a later delivery clears this back to ``None`` and shows as delivered,
    whatever its timestamp. ``None`` is the ordinary case.

    :attr:`reason` is a plain string for the same reason :attr:`NotSent.reason` is. Compare against
    :class:`~beaconbox.enums.EmailFailureReason` for the known values and treat anything else as
    "failed".
    """

    reason: str
    at: datetime | None = None
    """When the send was given up on."""
    raw: Payload = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, payload: Payload) -> Failed:
        return cls(
            reason=_text(payload.get("reason")),
            at=_parse_datetime(payload.get("at")),
            raw=payload,
        )


@dataclass(frozen=True)
class DeliveryStatus:
    """A message's delivery state, derived from its events.

    ``opened`` is the one worth acting on: it is the difference between "we sent it" and "they
    have it", and it is what ``escalate_if_unread_after_minutes`` waits on.

    ``not_sent`` and ``failed`` are the ones worth checking *before* you wait for any of them.
    Non-``None`` ``not_sent`` means no email was ever attempted and none will be; non-``None``
    ``failed`` means one was attempted and BeaconBox has given up on it. Either way stop polling
    ``delivered`` for this message, unless ``failed.reason`` is ``outcome_unknown``: a delivery
    can still arrive for that one, and clears ``failed``. See :class:`NotSent` and
    :class:`Failed`.
    """

    delivered: bool
    opened: bool
    bounced: bool
    delivered_at: datetime | None = None
    opened_at: datetime | None = None
    bounced_at: datetime | None = None
    sms: SmsDelivery | None = None
    not_sent: NotSent | None = None
    failed: Failed | None = None
    raw: Payload = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, payload: Payload) -> DeliveryStatus:
        sms = payload.get("sms")
        not_sent = payload.get("not_sent")
        failed = payload.get("failed")
        return cls(
            delivered=_flag(payload.get("delivered")),
            opened=_flag(payload.get("opened")),
            bounced=_flag(payload.get("bounced")),
            delivered_at=_parse_datetime(payload.get("delivered_at")),
            opened_at=_parse_datetime(payload.get("opened_at")),
            bounced_at=_parse_datetime(payload.get("bounced_at")),
            sms=SmsDelivery.from_api(sms) if isinstance(sms, dict) else None,
            not_sent=NotSent.from_api(not_sent) if isinstance(not_sent, dict) else None,
            failed=Failed.from_api(failed) if isinstance(failed, dict) else None,
            raw=payload,
        )


@dataclass(frozen=True)
class Message:
    """A message you sent, plus where its delivery got to."""

    id: str
    recipient_email: str
    subject: str
    kind: MessageKind | str
    status: MessageStatus | str
    created_at: datetime
    updated_at: datetime
    delivery: DeliveryStatus
    raw: Payload = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, payload: Payload) -> Message:
        delivery = payload.get("delivery")
        return cls(
            id=_text(payload.get("id")),
            recipient_email=_text(payload.get("recipient_email")),
            subject=_text(payload.get("subject")),
            kind=coerce(MessageKind, payload.get("kind")),
            status=coerce(MessageStatus, payload.get("status")),
            created_at=_require_datetime(payload.get("created_at")),
            updated_at=_require_datetime(payload.get("updated_at")),
            delivery=DeliveryStatus.from_api(delivery if isinstance(delivery, dict) else {}),
            raw=payload,
        )


@dataclass(frozen=True)
class MessagePage:
    """One page of messages, newest first.

    Keyset-paginated. Pass :attr:`next_cursor` back as ``cursor`` for the next page, or use
    ``messages.iterate()``, which does it for you. Offsets are deliberately not offered: a page 3
    read while new messages arrive shows rows page 2 already did.
    """

    items: tuple[Message, ...]
    next_cursor: str | None
    raw: Payload = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, payload: Payload) -> MessagePage:
        items = payload.get("items")
        return cls(
            items=tuple(
                Message.from_api(item)
                for item in (items if isinstance(items, list) else [])
                if isinstance(item, dict)
            ),
            next_cursor=_opt_text(payload.get("next_cursor")),
            raw=payload,
        )


@dataclass(frozen=True)
class RetractResult:
    """What a retraction did.

    ``retracted=False`` does not mean anything failed. It means the message was already not live,
    because you retracted it before or a later push superseded it.
    """

    id: str
    status: MessageStatus | str
    retracted: bool
    sends_cancelled: int
    """Queued sends this call called off. Zero means there was nothing left to stop."""
    already_notified: bool
    """True means the recipient was already told. The message shows as withdrawn in their inbox,
    but an email or text about it is out and cannot be recalled."""
    raw: Payload = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, payload: Payload) -> RetractResult:
        return cls(
            id=_text(payload.get("id")),
            status=coerce(MessageStatus, payload.get("status")),
            retracted=_flag(payload.get("retracted")),
            sends_cancelled=_number(payload.get("sends_cancelled")),
            already_notified=_flag(payload.get("already_notified")),
            raw=payload,
        )


@dataclass(frozen=True)
class CreditBalance:
    """The prepaid balance. One balance, shared by SMS and WhatsApp."""

    balance: int
    low_balance: bool
    threshold: int
    raw: Payload = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, payload: Payload) -> CreditBalance:
        return cls(
            balance=_number(payload.get("balance")),
            low_balance=_flag(payload.get("low_balance")),
            threshold=_number(payload.get("threshold")),
            raw=payload,
        )


@dataclass(frozen=True)
class RecipientSms:
    """A recipient's SMS state.

    :attr:`phone` is **masked**, for example ``+372 •••• 0134``. Reads never return it in full: an
    API key that leaks must not be usable to dump a phone book.
    """

    recipient_email: str
    phone: str | None
    sms_status: RecipientStatus | str
    sms_status_at: datetime | None = None
    sms_consent_at: datetime | None = None
    """When a number was first accepted for texting this recipient. ``None`` for a recipient who
    has never had one, or whose number predates the field. Not moved by a later number change, a
    STOP or a START."""
    sms_consent_source: SmsConsentSource | str | None = None
    """The surface that first accepted it: ``push``, ``api``, ``admin`` or ``inbox`` (see
    :class:`~beaconbox.enums.SmsConsentSource`), or ``None`` when no consent is on record."""
    raw: Payload = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, payload: Payload) -> RecipientSms:
        source = payload.get("sms_consent_source")
        return cls(
            recipient_email=_text(payload.get("recipient_email")),
            phone=_opt_text(payload.get("phone")),
            sms_status=coerce(RecipientStatus, payload.get("sms_status")),
            sms_status_at=_parse_datetime(payload.get("sms_status_at")),
            sms_consent_at=_parse_datetime(payload.get("sms_consent_at")),
            sms_consent_source=(
                coerce(SmsConsentSource, source) if isinstance(source, str) else None
            ),
            raw=payload,
        )


@dataclass(frozen=True)
class WhatsAppErasureReceipt:
    """What an erasure deleted, and what it could not reach.

    Counts only. A receipt quoting the bodies it deleted would be a fresh copy of them.

    **:attr:`replies_a_forward_email_may_have_carried` is your remaining work.** Until you act on
    it the erasure is only ours.
    """

    replies_deleted: int
    webhook_payloads_scrubbed: int
    """Copies removed from ``whatsapp.inbound_message`` deliveries still queued for your endpoint.
    Those deliveries still arrive, now carrying a null ``body``."""
    auto_reply_windows_dropped: int
    consent_withdrawn: bool
    """True when this erasure moved them to stopped, which is permanent for your business."""
    replies_a_forward_email_may_have_carried: int
    """Erased replies whose reply-forward notification had already run. Where your forwarding
    setting was ``full`` at the time, the customer's words went to your admin users' mailboxes,
    and an inbox, an archive, a backup and a helpdesk's search index are all outside anything
    BeaconBox can reach. An upper bound, because the setting on the day is not recorded."""
    forward_setting: WhatsAppReplyForward | str
    """Your forwarding setting **now**, so you know which mailboxes to search from here on. It
    says nothing about what it was when the erased replies arrived."""
    first_erased_at: datetime
    """When this recipient was *first* erased. On a repeat erasure the counts above are zero
    because there was nothing left, not because there never was anything."""
    raw: Payload = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, payload: Payload) -> WhatsAppErasureReceipt:
        return cls(
            replies_deleted=_number(payload.get("replies_deleted")),
            webhook_payloads_scrubbed=_number(payload.get("webhook_payloads_scrubbed")),
            auto_reply_windows_dropped=_number(payload.get("auto_reply_windows_dropped")),
            consent_withdrawn=_flag(payload.get("consent_withdrawn")),
            replies_a_forward_email_may_have_carried=_number(
                payload.get("replies_a_forward_email_may_have_carried")
            ),
            forward_setting=coerce(WhatsAppReplyForward, payload.get("forward_setting")),
            first_erased_at=_require_datetime(payload.get("first_erased_at")),
            raw=payload,
        )


@dataclass(frozen=True)
class RecipientErasureReport:
    """What :meth:`~beaconbox.resources.Recipients.erase` removed from your records about one
    recipient, as counts. **The erasure is not undoable.**

    Counts only, never content: a report quoting what it deleted would be a fresh copy of it.

    Two counts are of things **kept**, on purpose. :attr:`suppressions_kept` and :attr:`stops_kept`
    are what stop this person being contacted again by a later push to the same address or number,
    and an opt-out that is erased on request stops being one.

    A repeat erasure, and an erasure of an address you never messaged, both answer with zeros. To
    get the original counts back after a timed-out request, retry with the **same**
    ``idempotency_key``.
    """

    messages_deleted: int
    """Messages you sent them, subjects and bodies included."""
    message_events_deleted: int
    """Delivery, open and not-sent events about those messages."""
    channel_sends_deleted: int
    """SMS and WhatsApp send records, with the number they went to. Your credit ledger keeps its
    entries; they no longer point at a send."""
    message_links_deleted: int
    """Links that opened one of those messages."""
    whatsapp_replies_deleted: int
    """WhatsApp replies they sent you, bodies included."""
    webhook_payloads_scrubbed: int
    """Webhook deliveries about this person whose address, subject, number or reply text was
    replaced with null. A delivery still queued for your endpoint arrives with those fields
    empty. Copies your endpoint already received are yours to delete."""
    queued_jobs_cancelled: int
    """Nudges, texts and escalations still waiting to go out for those messages, now cancelled."""
    idempotency_records_deleted: int
    """Stored API responses that named this recipient. Retrying one of those requests with its
    original idempotency key runs it again rather than replaying it."""
    audit_entries_pseudonymised: int
    """Entries in your audit log about this recipient. They stay, because they record what your
    staff did; the address in them is replaced by a keyed hash."""
    subscription_deleted: bool
    """Whether their recipient record (phone number, SMS and WhatsApp consent, unsubscribe state)
    was deleted. ``False`` when there was none, for example on a repeat erasure."""
    stops_kept: int
    """SMS or WhatsApp opt-outs for their number that are kept, so nobody can text them again by
    pushing to this address later."""
    suppressions_kept: int
    """Email suppressions (a hard bounce, a complaint, or their unsubscribe from you) for this
    address that are kept, for the same reason."""
    auto_reply_windows_dropped: int
    """Rows holding their number so the automatic WhatsApp acknowledgement would not repeat."""
    replies_a_forward_email_may_have_carried: int
    """**Your remaining work.** An upper bound on erased WhatsApp replies that your reply-forward
    email had already sent to your admin users' mailboxes. Those copies are outside anything
    BeaconBox can reach: search your own mail for this recipient."""
    raw: Payload = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, payload: Payload) -> RecipientErasureReport:
        return cls(
            messages_deleted=_number(payload.get("messages_deleted")),
            message_events_deleted=_number(payload.get("message_events_deleted")),
            channel_sends_deleted=_number(payload.get("channel_sends_deleted")),
            message_links_deleted=_number(payload.get("message_links_deleted")),
            whatsapp_replies_deleted=_number(payload.get("whatsapp_replies_deleted")),
            webhook_payloads_scrubbed=_number(payload.get("webhook_payloads_scrubbed")),
            queued_jobs_cancelled=_number(payload.get("queued_jobs_cancelled")),
            idempotency_records_deleted=_number(payload.get("idempotency_records_deleted")),
            audit_entries_pseudonymised=_number(payload.get("audit_entries_pseudonymised")),
            subscription_deleted=_flag(payload.get("subscription_deleted")),
            stops_kept=_number(payload.get("stops_kept")),
            suppressions_kept=_number(payload.get("suppressions_kept")),
            auto_reply_windows_dropped=_number(payload.get("auto_reply_windows_dropped")),
            replies_a_forward_email_may_have_carried=_number(
                payload.get("replies_a_forward_email_may_have_carried")
            ),
            raw=payload,
        )


@dataclass(frozen=True)
class ApiKey:
    """A key as listed. ``masked`` is all that is ever shown after creation."""

    id: str
    name: str
    masked: str
    created: datetime
    raw: Payload = field(default_factory=dict, repr=False, compare=False)
    minted_by: str | None = None
    """The id of the key that created this one through the API, detached or not; ``None`` for a key
    created in the dashboard (or by a server that predates the field). After a leak, a key still
    listed with the leaked key's id here was created detached with it and is still live."""

    @classmethod
    def from_api(cls, payload: Payload) -> ApiKey:
        return cls(
            id=_text(payload.get("id")),
            name=_text(payload.get("name")),
            masked=_text(payload.get("masked")),
            created=_require_datetime(payload.get("created")),
            raw=payload,
            minted_by=_opt_text(payload.get("minted_by")),
        )


@dataclass(frozen=True)
class NewApiKey:
    """A freshly minted key.

    :attr:`key` is the **only** time the full value exists anywhere outside the caller. BeaconBox
    stores a hash, so it cannot show it again and cannot recover it for you. Put it straight into
    a secret store: a key that reaches a log line or a support ticket has to be revoked.

    **What is guarded, and what is not.** ``repr`` is redacted, which covers ``print``, an f-string
    and a ``%r`` or ``%s`` in a log call — the incidental paths. ``dataclasses.asdict`` and
    :attr:`raw` still carry the value, deliberately: ``raw`` is how a field this SDK predates stays
    readable. Read :attr:`key`, store it, drop the object.
    """

    id: str
    """The handle for revoking this key: pass it to :meth:`~beaconbox.resources.Keys.revoke`.

    Use it rather than matching on :attr:`name`, which carries no unique constraint and defaults to
    ``"Untitled key"``, so two unnamed keys cannot be told apart.
    """
    name: str
    key: str
    masked: str
    created: datetime
    raw: Payload = field(default_factory=dict, repr=False, compare=False)

    def __repr__(self) -> str:
        """Redacted, deliberately. A ``print(key)`` in a debugging session is exactly how a live
        credential reaches a log aggregator. Read :attr:`key` explicitly to get the value.

        The id is included: it is the handle :meth:`~beaconbox.resources.Keys.revoke` takes, and it
        is not the secret.
        """
        return (
            f"NewApiKey(id={self.id!r}, name={self.name!r}, "
            f"key='<redacted>', masked={self.masked!r})"
        )

    @classmethod
    def from_api(cls, payload: Payload) -> NewApiKey:
        return cls(
            id=_text(payload.get("id")),
            name=_text(payload.get("name")),
            key=_text(payload.get("key")),
            masked=_text(payload.get("masked")),
            created=_require_datetime(payload.get("created")),
            raw=payload,
        )


@dataclass(frozen=True)
class WebhookEndpoint:
    """A registered endpoint. :attr:`secret` is masked here.

    :attr:`disabled` goes true once enough consecutive deliveries have exhausted their retries.
    That takes hours of sustained failure, not a blip. Delete and re-create the endpoint once the
    receiver is healthy, which also rotates the secret.
    """

    id: str
    url: str
    description: str
    secret: str
    event_types: tuple[str, ...]
    """Empty means every event type, including ones added later."""
    disabled: bool
    consecutive_failures: int
    created_at: datetime
    last_success_at: datetime | None = None
    last_error: str | None = None
    raw: Payload = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, payload: Payload) -> WebhookEndpoint:
        types = payload.get("event_types")
        return cls(
            id=_text(payload.get("id")),
            url=_text(payload.get("url")),
            description=_text(payload.get("description")),
            secret=_text(payload.get("secret")),
            event_types=tuple(
                t for t in (types if isinstance(types, list) else []) if isinstance(t, str)
            ),
            disabled=_flag(payload.get("disabled")),
            consecutive_failures=_number(payload.get("consecutive_failures")),
            created_at=_require_datetime(payload.get("created_at")),
            last_success_at=_parse_datetime(payload.get("last_success_at")),
            last_error=_opt_text(payload.get("last_error")),
            raw=payload,
        )


@dataclass(frozen=True)
class NewWebhookEndpoint(WebhookEndpoint):
    """The creation response, where :attr:`secret` is the **real** signing secret.

    Shown exactly once. Store it before the process exits: verifying a delivery is impossible
    without it, and the only recovery is to delete the endpoint and register it again.
    """

    def __repr__(self) -> str:
        return f"NewWebhookEndpoint(id={self.id!r}, url={self.url!r}, secret='<redacted>')"

    @classmethod
    def from_api(cls, payload: Payload) -> NewWebhookEndpoint:
        """Narrows the inherited return type. The parent builds ``cls`` either way, so this
        changes nothing at runtime and everything for a caller's type checker."""
        parsed = WebhookEndpoint.from_api(payload)
        return cls(**{f.name: getattr(parsed, f.name) for f in fields(parsed)})


@dataclass(frozen=True)
class WebhookEvent:
    """A verified webhook delivery.

    You only ever get one of these from :func:`beaconbox.webhooks.verify`, so holding one is
    proof the payload was signed with your endpoint's secret and is not a replay.
    """

    id: str
    """The delivery id, also sent as ``X-BeaconBox-Delivery``. **Use it to deduplicate.** A
    webhook that timed out on your side is retried, so the same event id can arrive twice."""
    type: WebhookEventType | str
    occurred_at: datetime
    data: Payload
    raw: Payload = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, payload: Payload) -> WebhookEvent:
        data = payload.get("data")
        return cls(
            id=_text(payload.get("id")),
            type=coerce(WebhookEventType, payload.get("type")),
            occurred_at=_require_datetime(payload.get("occurred_at")),
            data=data if isinstance(data, dict) else {},
            raw=payload,
        )


@dataclass(frozen=True)
class MessagePush:
    """One push, as an object, for building a batch.

    ``messages.push()`` takes these as keyword arguments instead, which is the nicer call for a
    single message. This class exists because ``messages.push_batch()`` takes a list, and a list
    of dictionaries has no field names an editor can complete or a type checker can check.

    Only ``recipient_email``, ``subject`` and ``body`` are required. See
    :meth:`beaconbox.resources.Messages.push` for what each of the rest does, including
    ``channels``, which can only narrow what your account settings allow and never switches a
    channel on, and ``obsoletes``, which takes at most 100 ids of at most 64 characters each.
    """

    recipient_email: str
    subject: str
    body: str
    kind: MessageKind | str = MessageKind.ONE_OFF
    reference: str | None = None
    recipient_phone: str | None = None
    channels: tuple[Channel | str, ...] | None = None
    notify: bool | None = None
    send_at: datetime | None = None
    escalate_if_unread_after_minutes: int | None = None
    obsoletes: tuple[str, ...] = ()
    whatsapp_opt_in_source: str | None = None
    sms_if_whatsapp_fails: bool = False
    # Last, so a caller passing the fields above positionally is not shifted by it.
    order_status: OrderStatus | str | None = None

    def to_payload(self) -> Payload:
        """The request body, with unset optional fields left out entirely.

        Omitted rather than sent as null, because null is not "unset" on several of these fields.
        ``notify=None`` means "apply the default", while ``notify=false`` actively suppresses a
        nudge that would otherwise be sent.
        """
        payload: Payload = {
            "recipient_email": self.recipient_email,
            "subject": self.subject,
            "body": self.body,
            "kind": _enum_value(self.kind),
        }
        if self.reference is not None:
            payload["reference"] = self.reference
        if self.order_status is not None:
            payload["order_status"] = _enum_value(self.order_status)
        if self.recipient_phone is not None:
            payload["recipient_phone"] = self.recipient_phone
        if self.channels is not None:
            payload["channels"] = [_enum_value(channel) for channel in self.channels]
        if self.notify is not None:
            payload["notify"] = self.notify
        if self.send_at is not None:
            payload["send_at"] = _isoformat(self.send_at)
        if self.escalate_if_unread_after_minutes is not None:
            payload["escalate_if_unread_after_minutes"] = self.escalate_if_unread_after_minutes
        if self.obsoletes:
            payload["obsoletes"] = list(self.obsoletes)
        if self.whatsapp_opt_in_source is not None:
            payload["whatsapp_opt_in"] = {"source": self.whatsapp_opt_in_source}
        if self.sms_if_whatsapp_fails:
            payload["sms_if_whatsapp_fails"] = True
        return payload


def _enum_value(value: Any) -> Any:
    return value.value if isinstance(value, Channel | MessageKind | OrderStatus) else value


def _isoformat(value: datetime) -> str:
    """``send_at`` requires a timezone, so a naive datetime is refused here rather than 422'd.

    A naive datetime is ambiguous by construction, and the failure it produces is a nudge
    delivered at the wrong hour in the recipient's day, which nobody notices as a bug.
    """
    if value.tzinfo is None:
        raise ValueError(
            "send_at requires a timezone-aware datetime "
            "(e.g. datetime.now(timezone.utc) + timedelta(hours=8))"
        )
    return value.isoformat()
