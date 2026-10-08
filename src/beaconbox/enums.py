"""The API's closed vocabularies, plus the skip reasons worth branching on.

Every enum here subclasses ``str``, so ``result.status == "active"`` keeps working and the values
serialize without a ``.value``.

**An unknown value is never an error.** Models coerce with :func:`coerce`, which returns the plain
string when the API sends a member this SDK has not heard of. A new ``MessageStatus`` shipped
next quarter must not turn an old SDK into a crash loop in a merchant's job runner, so the cost
of an unrecognised value is that you compare it as a string.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, TypeVar

__all__ = [
    "Channel",
    "ChannelSendStatus",
    "EmailFailureReason",
    "EmailSkipReason",
    "MessageKind",
    "MessageStatus",
    "OrderStatus",
    "RecipientStatus",
    "SkipReason",
    "SmsConsentSource",
    "WebhookEventType",
    "WhatsAppReplyForward",
    "coerce",
]


class MessageKind(str, Enum):
    """Whether a push creates a new message every time, or updates one in place."""

    ONE_OFF = "one_off"
    """Always a new message, always a nudge."""

    UPDATEABLE = "updateable"
    """Matched on subject. Creates and nudges the first time, then updates in place quietly."""


class OrderStatus(str, Enum):
    """What happened to the order a push is about: the value of a push's ``order_status``.

    BeaconBox picks the platform-written WhatsApp and SMS wording from it, for example "Your order
    #A-10294 from PhonicBloom has shipped." A WhatsApp nudge is sent only when the push carries
    both ``reference`` and ``order_status``; otherwise WhatsApp is skipped with
    ``template_not_sendable`` and nothing is charged.

    The list grows on the server, so a push accepts a plain string as well, and a result carrying
    a value this SDK does not know reads it back as that string.
    """

    CONFIRMED = "confirmed"
    SHIPPED = "shipped"
    READY_FOR_PICKUP = "ready_for_pickup"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"
    DELAYED = "delayed"


class MessageStatus(str, Enum):
    """Where a message stands in the recipient's inbox."""

    ACTIVE = "active"
    OBSOLETE = "obsolete"
    """Superseded by a later push that named it in ``obsoletes``. Greyed out, not removed."""
    RETRACTED = "retracted"
    """Withdrawn by the sender. Also greyed out: they may already have read it."""


class Channel(str, Enum):
    """A delivery channel a push may request.

    ``EMAIL`` is implicit and always sent, because it carries the inbox link. Listing it is
    allowed and harmless.

    Naming ``SMS`` or ``WHATSAPP`` in a push's ``channels`` is a request that can only narrow
    what your account settings allow, never switch a channel on. A channel whose mode is ``off``
    stays off, and one BeaconBox has not yet enabled for your business never sends. A channel in
    ``on_request`` mode sends only on a push that names it.
    """

    EMAIL = "email"
    SMS = "sms"
    WHATSAPP = "whatsapp"


class ChannelSendStatus(str, Enum):
    """How far one SMS or WhatsApp send got.

    ``ACCEPTED`` means the carrier or Meta took it. ``DELIVERED`` means the handset confirmed it.
    The gap between those two is where a number that no longer exists lives.
    """

    QUEUED = "queued"
    ACCEPTED = "accepted"
    DELIVERED = "delivered"
    READ = "read"
    FAILED = "failed"
    REJECTED = "rejected"
    SKIPPED = "skipped"


class RecipientStatus(str, Enum):
    """A recipient's consent on one channel, for one business.

    SMS and WhatsApp carry this separately and must never be conflated: holding a number for SMS
    says nothing about whether its owner agreed to be messaged on WhatsApp, and Meta can audit
    that agreement.

    ``STOPPED`` is permanent for your business. It cannot be undone through the API, by design:
    collect consent again out of band.
    """

    NONE = "none"
    ACTIVE = "active"
    STOPPED = "stopped"


class SmsConsentSource(str, Enum):
    """The surface a recipient's number was first accepted for texting through.

    Recorded once, when texting first began, and never overwritten: a later number change, a STOP
    and a START all leave it as the evidence of how it started.
    """

    PUSH = "push"
    """A ``recipient_phone`` on a push."""
    API = "api"
    """:meth:`~beaconbox.resources.Recipients.set_phone`."""
    ADMIN = "admin"
    """A signed-in person on the recipient page in the dashboard."""
    INBOX = "inbox"
    """The recipient adding their own number in their inbox."""


class WhatsAppReplyForward(str, Enum):
    """How much of a customer's WhatsApp reply your notification email carries."""

    FULL = "full"
    """The words themselves, which is what puts them in your mailboxes and your backups."""
    LINK_ONLY = "link_only"
    OFF = "off"


class WebhookEventType(str, Enum):
    """What an endpoint can subscribe to.

    Only things the sender cannot already know. There is no ``message.created``: you just made
    that call and got the message back, so an event for it would be a round trip telling you
    nothing.

    Most are delivery outcomes. The rest — ``credits.low_balance`` and the three ``plan.*`` — are
    about the *account*, and they pass the same test for a sharper reason: nothing in a response
    to one of your own calls would ever mention them, so without these you learn your sending has
    stopped from a customer.

    An endpoint registered with an **empty** list receives every type, including types added
    after it was created. That is the recommended setting, because a narrow subscription is how a
    new event type silently passes an integration by.
    """

    MESSAGE_DELIVERED = "message.delivered"
    MESSAGE_OPENED = "message.opened"
    MESSAGE_BOUNCED = "message.bounced"
    MESSAGE_FAILED = "message.failed"
    """An email was attempted and will never be delivered.

    ``data`` carries ``message_id``, ``recipient_email``, ``subject`` and ``channel``
    (``"email"``), plus ``reason`` when BeaconBox gave up on the send itself: ``esp_refused`` (the
    email provider rejected it outright), ``retries_exhausted`` (every attempt failed) or
    ``outcome_unknown`` (the provider may have taken it and the answer was lost, so it may still
    arrive). See :class:`EmailFailureReason`. The same outcome reads back as ``delivery.failed``
    on the message, so a caller polling for delivery can stop waiting, except on
    ``outcome_unknown``: a later ``message.delivered`` clears that one."""
    MESSAGE_COMPLAINED = "message.complained"
    MESSAGE_NOT_SENT = "message.not_sent"
    """No email was ever attempted, and ``data["reason"]`` says why.

    Deliberately not ``message.failed``: nothing was submitted and nothing bounced, so folding it
    in would put a refusal into the rate you watch for provider trouble."""
    SMS_DELIVERED = "sms.delivered"
    SMS_FAILED = "sms.failed"
    """A text did not reach the handset. ``data`` carries ``message_id``, ``recipient_phone``
    (masked), ``country_code``, ``credits_charged``, ``channel`` (``"sms"``) and ``error_code``.

    ``error_code`` ``sms.submit_outcome_unknown`` means BeaconBox called the carrier and never
    learned the answer. No second copy is sent, because the first may already have gone, and the
    charge stands for the same reason: the recipient may still have received it.

    Sent on the carrier's receipt, and also when BeaconBox ends the send itself: an outcome it
    never learned, or a send given up on after its retries."""
    SMS_REJECTED = "sms.rejected"
    """The text was refused, by the carrier's receipt or at submit."""
    WHATSAPP_DELIVERED = "whatsapp.delivered"
    WHATSAPP_FAILED = "whatsapp.failed"
    """A WhatsApp message did not reach the handset. ``data`` has the same fields as
    :attr:`SMS_FAILED`, with ``channel`` ``"whatsapp"``.

    ``error_code`` ``whatsapp.submit_outcome_unknown`` means BeaconBox called Meta and never
    learned the answer, and no status arrived for it within five minutes. It is not resent and
    the charge stands, as for SMS; ``sms_if_whatsapp_fails`` still texts them.

    Sent on Meta's status, and also when BeaconBox ends the send itself: an outcome it never
    learned, or a send given up on after its retries."""
    WHATSAPP_REJECTED = "whatsapp.rejected"
    """The WhatsApp message was refused, by Meta's status or at submit. Also sent, with
    ``error_code`` ``whatsapp.submit_refused`` and the credit refunded, for a send Meta refused on
    every attempt until BeaconBox gave up."""
    WHATSAPP_READ = "whatsapp.read"
    WHATSAPP_INBOUND_MESSAGE = "whatsapp.inbound_message"
    CREDITS_LOW_BALANCE = "credits.low_balance"
    PLAN_PAST_DUE = "plan.past_due"
    """The card failed. Email sending stops at ``data["period_end"]``, so there is still time."""
    PLAN_LAPSED = "plan.lapsed"
    """**Email sending has stopped.** The one to alert on: every nudge from here produces a
    :attr:`MESSAGE_NOT_SENT` with reason ``plan_lapsed`` until the subscription is paid."""
    PLAN_ALLOWANCE_EXCEEDED = "plan.allowance_exceeded"
    """The period's included email is used up. Sending continues and nothing is charged for it:
    the allowance is fair use."""


class EmailSkipReason(str, Enum):
    """Why a nudge email was never sent — the values ``delivery.not_sent.reason`` carries.

    **Not the parsed type of that field**, deliberately: :class:`~beaconbox.models.NotSent` keeps
    ``reason`` a plain string, because this list grows on the server whenever a refusal is added
    to the send path and an SDK that raised on an unknown value would turn that into a
    client-side crash. Compare against these and treat anything else as "not sent"::

        if message.delivery.not_sent:
            if message.delivery.not_sent.reason == EmailSkipReason.PLAN_LAPSED:
                ...

    The three worth branching on are :attr:`PLAN_LAPSED` (pay, and sending resumes),
    :attr:`DAILY_CAP_REACHED` (nothing is wrong; the day's allowance ran out, and the update needs
    pushing again because it is not retried) and :attr:`SUPPRESSED` (that address will never be
    emailed again for you).
    """

    PLAN_LAPSED = "plan_lapsed"
    """The subscription no longer pays for email. Pay, and sending resumes — nothing has to be
    re-pushed for new messages."""
    DAILY_CAP_REACHED = "daily_cap_reached"
    """The sending domain's daily cap. Nothing is wrong with the account, and later messages the
    same day are refused too.

    **Requires a resend.** This decision is final for the send: the nudge is not retried when the
    cap resets, so push again the next day if the update still matters."""
    SENDING_PAUSED = "sending_paused"
    BUSINESS_INACTIVE = "business_inactive"
    """Your business has been deactivated, so nothing is sent for it."""
    SUPPRESSED = "suppressed"
    """This address hard-bounced or complained for you. It is never emailed for you again."""
    PLATFORM_SUPPRESSED = "platform_suppressed"
    """It hard-bounced for another merchant on the shared sending domain."""
    UNSUBSCRIBED = "unsubscribed"
    MESSAGE_RETRACTED = "message_retracted"
    MESSAGE_OBSOLETE = "message_obsolete"
    """A newer message replaced this one before its nudge ran."""


class EmailFailureReason(str, Enum):
    """Why BeaconBox gave up on an attempted email: the values ``delivery.failed.reason`` and the
    ``message.failed`` webhook carry.

    The counterpart of :class:`EmailSkipReason` at the other end of the send path. That one means
    BeaconBox decided not to try; this one means it tried and the send was refused, never
    succeeded, or ended without an answer (:attr:`OUTCOME_UNKNOWN`). Like that enum it is **not
    the parsed type** of the field: compare against it and treat anything else as "failed",
    because the list grows.
    """

    ESP_REFUSED = "esp_refused"
    """The email provider rejected the message outright."""
    RETRIES_EXHAUSTED = "retries_exhausted"
    """Every attempt failed and BeaconBox stopped trying."""
    OUTCOME_UNKNOWN = "outcome_unknown"
    """The email provider accepted the message, or may have, but its answer was lost. Reported as
    failed so nothing waits on it forever, and never sent again, because a second email would be
    a second live sign-in link. **It may still arrive**: a later delivery from the provider
    clears ``delivery.failed`` (back to ``None``) and shows as delivered, whatever its
    timestamp."""


class SkipReason(str, Enum):
    """Why a paid channel was not sent.

    **A skip is a successful push.** The update is in the recipient's inbox and the email nudge
    has gone. This SDK never raises on one, because treating a skip as an error is what invites a
    retry, and a retry of a push that already succeeded is a second message to a real person.

    Not every reason is actionable, and the useful split is between the ones you can do something
    about and the ones you cannot:

    * :attr:`INSUFFICIENT_CREDIT` is the one to act on. Top up, then call
      ``messages.resend_sms(id)``.
    * :attr:`NO_PHONE` and :attr:`PHONE_CONFLICT` are data problems on your side.
    * :attr:`STOPPED`, :attr:`UNSUBSCRIBED` and :attr:`NOT_OPTED_IN` are the recipient's choice.
      Do not route around them.
    * :attr:`SMS_NOT_ENABLED` and :attr:`WHATSAPP_NOT_ENABLED` mean BeaconBox has not switched
      that channel on for your business yet. Ask for it from the dashboard.
    * The rest are account or destination settings.

    Values not listed here can appear: compare as a string and see
    :meth:`~beaconbox.models.SmsOutcome.skipped_reason`.
    """

    SENDING_PAUSED = "sending_paused"
    UNSUBSCRIBED = "unsubscribed"
    DISABLED_BY_REQUEST = "disabled_by_request"
    """Your own ``channels`` argument left this channel out."""
    SMS_NOT_ENABLED = "sms_not_enabled"
    """SMS has not been enabled for your business yet. Naming ``sms`` in ``channels`` does not
    change that."""
    SMS_DISABLED = "sms_disabled"
    """Your SMS mode is ``off``. Final: naming ``sms`` in ``channels`` does not override it."""
    SMS_WARMUP_CAP_REACHED = "sms_warmup_cap_reached"
    """A newly enabled business sends a limited number of texts a day while it warms up. Decided
    when the send runs, so it reaches you on the message's ``delivery.sms`` rather than in the
    push response."""
    WHATSAPP_NOT_ENABLED = "whatsapp_not_enabled"
    """WhatsApp has not been enabled for your business yet."""
    WHATSAPP_DISABLED = "whatsapp_disabled"
    """Your WhatsApp mode is ``off``. Final, like :attr:`SMS_DISABLED`."""
    COUNTRY_NOT_ALLOWED = "country_not_allowed"
    NO_PHONE = "no_phone"
    PHONE_CONFLICT = "phone_conflict"
    """The number belongs to another of your recipients, so it was not stored."""
    STOPPED = "stopped"
    NOT_OPTED_IN = "not_opted_in"
    NOT_REACHABLE = "not_reachable"
    """The number is not on WhatsApp. Learned by sending, never by asking."""
    TEMPLATE_NOT_SENDABLE = "template_not_sendable"
    INSUFFICIENT_CREDIT = "insufficient_credit"
    RATE_LIMITED = "rate_limited"
    PLATFORM_LIMITED = "platform_limited"
    ALREADY_OPTED_IN = "already_opted_in"
    """Consent was already on file, with its original date and source kept."""
    OPTED_OUT = "opted_out"


_E = TypeVar("_E", bound=Enum)


def coerce(enum_cls: type[_E], value: Any) -> _E | str:
    """Return the enum member for ``value``, or the plain string when it is not one.

    The fallback is the point. See the module docstring.

    **Only ever a member or a ``str``**, which is what every field built through this is annotated
    as. A JSON ``null`` or a number in an enum field would otherwise reach a caller as a value the
    annotation says cannot be there, to be compared against a member and silently never match.

    A non-string reads as the empty string rather than as its ``repr``: ``""`` is obviously absent
    and testable, where ``"None"`` or ``"7"`` looks like a value the server sent.
    """
    if not isinstance(value, str):
        return ""
    try:
        return enum_cls(value)
    except ValueError:
        return value
