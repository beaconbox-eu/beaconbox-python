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
    "MessageKind",
    "MessageStatus",
    "RecipientStatus",
    "SkipReason",
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


class WhatsAppReplyForward(str, Enum):
    """How much of a customer's WhatsApp reply your notification email carries."""

    FULL = "full"
    """The words themselves, which is what puts them in your mailboxes and your backups."""
    LINK_ONLY = "link_only"
    OFF = "off"


class WebhookEventType(str, Enum):
    """What an endpoint can subscribe to.

    Only outcomes the sender cannot already know. There is no ``message.created``: you just made
    that call and got the message back, so an event for it would be a round trip telling you
    nothing.

    An endpoint registered with an **empty** list receives every type, including types added
    after it was created. That is the recommended setting, because a narrow subscription is how a
    new event type silently passes an integration by.
    """

    MESSAGE_DELIVERED = "message.delivered"
    MESSAGE_OPENED = "message.opened"
    MESSAGE_BOUNCED = "message.bounced"
    MESSAGE_FAILED = "message.failed"
    MESSAGE_COMPLAINED = "message.complained"
    SMS_DELIVERED = "sms.delivered"
    SMS_FAILED = "sms.failed"
    SMS_REJECTED = "sms.rejected"
    WHATSAPP_DELIVERED = "whatsapp.delivered"
    WHATSAPP_FAILED = "whatsapp.failed"
    WHATSAPP_REJECTED = "whatsapp.rejected"
    WHATSAPP_READ = "whatsapp.read"
    WHATSAPP_INBOUND_MESSAGE = "whatsapp.inbound_message"
    CREDITS_LOW_BALANCE = "credits.low_balance"


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
    * The rest are account or destination settings.

    Values not listed here can appear: compare as a string and see
    :meth:`~beaconbox.models.SmsOutcome.skipped_reason`.
    """

    SENDING_PAUSED = "sending_paused"
    UNSUBSCRIBED = "unsubscribed"
    DISABLED_BY_REQUEST = "disabled_by_request"
    """Your own ``channels`` argument left this channel out."""
    SMS_DISABLED = "sms_disabled"
    WHATSAPP_DISABLED = "whatsapp_disabled"
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


def coerce(enum_cls: type[_E], value: Any) -> Any:
    """Return the enum member for ``value``, or ``value`` unchanged when it is not one.

    The fallback is the point. See the module docstring.
    """
    try:
        return enum_cls(value)
    except ValueError:
        return value
