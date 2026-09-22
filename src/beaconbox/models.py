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
    RecipientStatus,
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
    "Message",
    "MessagePage",
    "MessagePush",
    "MessagePushResult",
    "NewApiKey",
    "NewWebhookEndpoint",
    "NotSent",
    "RecipientSms",
    "RetractResult",
    "SmsDelivery",
    "SmsOutcome",
    "WebhookEndpoint",
    "WebhookEvent",
    "WhatsAppErasureReceipt",
    "WhatsAppOptInOutcome",
    "WhatsAppOutcome",
]

Payload = dict[str, Any]


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
            queued=bool(payload.get("queued", False)),
            credits=int(payload.get("credits", 0)),
            skipped_reason=payload.get("skipped_reason"),
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
    """Whether an SMS is armed to follow if this WhatsApp message proves undeliverable. If it
    fires it is a separate send with its own credit: poll the message to see it."""
    escalates_at: datetime | None = None
    raw: Payload = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, payload: Payload) -> WhatsAppOutcome:
        return cls(
            queued=bool(payload.get("queued", False)),
            credits=int(payload.get("credits", 0)),
            skipped_reason=payload.get("skipped_reason"),
            sms_fallback=bool(payload.get("sms_fallback", False)),
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
            recorded=bool(payload.get("recorded", False)),
            status=coerce(RecipientStatus, payload.get("status")),
            skipped_reason=payload.get("skipped_reason"),
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
    raw: Payload = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, payload: Payload) -> MessagePushResult:
        sms = payload.get("sms")
        whatsapp = payload.get("whatsapp")
        opt_in = payload.get("whatsapp_opt_in")
        return cls(
            id=str(payload.get("id", "")),
            subject=str(payload.get("subject", "")),
            kind=coerce(MessageKind, payload.get("kind")),
            status=coerce(MessageStatus, payload.get("status")),
            created=bool(payload.get("created", False)),
            nudged=bool(payload.get("nudged", False)),
            created_at=_require_datetime(payload.get("created_at")),
            updated_at=_require_datetime(payload.get("updated_at")),
            scheduled_for=_parse_datetime(payload.get("scheduled_for")),
            sms=SmsOutcome.from_api(sms) if isinstance(sms, dict) else None,
            sms_units=int(payload.get("sms_units", 0)),
            whatsapp=WhatsAppOutcome.from_api(whatsapp) if isinstance(whatsapp, dict) else None,
            whatsapp_opt_in=(
                WhatsAppOptInOutcome.from_api(opt_in) if isinstance(opt_in, dict) else None
            ),
            reference=payload.get("reference"),
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
            index=int(payload.get("index", 0)),
            ok=bool(payload.get("ok", False)),
            result=MessagePushResult.from_api(result) if isinstance(result, dict) else None,
            error_code=payload.get("error_code"),
            detail=payload.get("detail"),
            raw=payload,
        )


@dataclass(frozen=True)
class BatchResult:
    """What a batch returns. **Always HTTP 200: check :attr:`failed`, not the status code.**

    An item that failed is reported, not raised. One bad recipient must not discard the
    forty-nine good pushes alongside it, and a 4xx for the whole call would invite a retry of all
    fifty.
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
            succeeded=int(payload.get("succeeded", 0)),
            failed=int(payload.get("failed", 0)),
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
            credits_charged=int(payload.get("credits_charged", 0)),
            skipped_reason=payload.get("skipped_reason"),
            submitted_at=_parse_datetime(payload.get("submitted_at")),
            last_status_at=_parse_datetime(payload.get("last_status_at")),
            raw=payload,
        )


@dataclass(frozen=True)
class NotSent:
    """Set when BeaconBox decided not to email a message at all, and that decision still stands.

    **The field that makes ``delivered is False`` readable.** Without it that flag meant two
    opposite things — *on its way* and *never attempted, and never will be* — so a caller polling
    for delivery had no way to know when to stop. ``None`` is the ordinary case.

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
            reason=str(payload.get("reason", "")),
            at=_parse_datetime(payload.get("at")),
            raw=payload,
        )


@dataclass(frozen=True)
class DeliveryStatus:
    """A message's delivery state, derived from its events.

    ``opened`` is the one worth acting on: it is the difference between "we sent it" and "they
    have it", and it is what ``escalate_if_unread_after_minutes`` waits on.

    ``not_sent`` is the one worth checking *before* you wait for any of them. Non-``None`` means
    no email was ever attempted and none will be, so polling ``delivered`` for this message will
    never terminate. See :class:`NotSent`.
    """

    delivered: bool
    opened: bool
    bounced: bool
    delivered_at: datetime | None = None
    opened_at: datetime | None = None
    bounced_at: datetime | None = None
    sms: SmsDelivery | None = None
    not_sent: NotSent | None = None
    raw: Payload = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, payload: Payload) -> DeliveryStatus:
        sms = payload.get("sms")
        not_sent = payload.get("not_sent")
        return cls(
            delivered=bool(payload.get("delivered", False)),
            opened=bool(payload.get("opened", False)),
            bounced=bool(payload.get("bounced", False)),
            delivered_at=_parse_datetime(payload.get("delivered_at")),
            opened_at=_parse_datetime(payload.get("opened_at")),
            bounced_at=_parse_datetime(payload.get("bounced_at")),
            sms=SmsDelivery.from_api(sms) if isinstance(sms, dict) else None,
            not_sent=NotSent.from_api(not_sent) if isinstance(not_sent, dict) else None,
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
            id=str(payload.get("id", "")),
            recipient_email=str(payload.get("recipient_email", "")),
            subject=str(payload.get("subject", "")),
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
            next_cursor=payload.get("next_cursor"),
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
            id=str(payload.get("id", "")),
            status=coerce(MessageStatus, payload.get("status")),
            retracted=bool(payload.get("retracted", False)),
            sends_cancelled=int(payload.get("sends_cancelled", 0)),
            already_notified=bool(payload.get("already_notified", False)),
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
            balance=int(payload.get("balance", 0)),
            low_balance=bool(payload.get("low_balance", False)),
            threshold=int(payload.get("threshold", 0)),
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
    raw: Payload = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, payload: Payload) -> RecipientSms:
        return cls(
            recipient_email=str(payload.get("recipient_email", "")),
            phone=payload.get("phone"),
            sms_status=coerce(RecipientStatus, payload.get("sms_status")),
            sms_status_at=_parse_datetime(payload.get("sms_status_at")),
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
            replies_deleted=int(payload.get("replies_deleted", 0)),
            webhook_payloads_scrubbed=int(payload.get("webhook_payloads_scrubbed", 0)),
            auto_reply_windows_dropped=int(payload.get("auto_reply_windows_dropped", 0)),
            consent_withdrawn=bool(payload.get("consent_withdrawn", False)),
            replies_a_forward_email_may_have_carried=int(
                payload.get("replies_a_forward_email_may_have_carried", 0)
            ),
            forward_setting=coerce(WhatsAppReplyForward, payload.get("forward_setting")),
            first_erased_at=_require_datetime(payload.get("first_erased_at")),
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

    @classmethod
    def from_api(cls, payload: Payload) -> ApiKey:
        return cls(
            id=str(payload.get("id", "")),
            name=str(payload.get("name", "")),
            masked=str(payload.get("masked", "")),
            created=_require_datetime(payload.get("created")),
            raw=payload,
        )


@dataclass(frozen=True)
class NewApiKey:
    """A freshly minted key.

    :attr:`key` is the **only** time the full value exists anywhere outside the caller. BeaconBox
    stores a hash, so it cannot show it again and cannot recover it for you. Put it straight into
    a secret store: a key that reaches a log line or a support ticket has to be revoked.
    """

    name: str
    key: str
    masked: str
    created: datetime
    raw: Payload = field(default_factory=dict, repr=False, compare=False)

    def __repr__(self) -> str:
        """Redacted, deliberately. A ``print(key)`` in a debugging session is exactly how a live
        credential reaches a log aggregator. Read :attr:`key` explicitly to get the value."""
        return f"NewApiKey(name={self.name!r}, key='<redacted>', masked={self.masked!r})"

    @classmethod
    def from_api(cls, payload: Payload) -> NewApiKey:
        return cls(
            name=str(payload.get("name", "")),
            key=str(payload.get("key", "")),
            masked=str(payload.get("masked", "")),
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
            id=str(payload.get("id", "")),
            url=str(payload.get("url", "")),
            description=str(payload.get("description", "")),
            secret=str(payload.get("secret", "")),
            event_types=tuple(str(t) for t in (types if isinstance(types, list) else [])),
            disabled=bool(payload.get("disabled", False)),
            consecutive_failures=int(payload.get("consecutive_failures", 0)),
            created_at=_require_datetime(payload.get("created_at")),
            last_success_at=_parse_datetime(payload.get("last_success_at")),
            last_error=payload.get("last_error"),
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
            id=str(payload.get("id", "")),
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
    :meth:`beaconbox.resources.Messages.push` for what each of the rest does.
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
    return value.value if isinstance(value, Channel | MessageKind) else value


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
