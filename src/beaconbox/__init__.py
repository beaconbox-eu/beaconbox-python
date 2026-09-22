"""Official Python SDK for BeaconBox.

BeaconBox is a private inbox where only businesses a customer has bought from can reach them, so
shipping, tracking, payment and firmware updates never get lost to spam.

.. code-block:: python

    from beaconbox import BeaconBox

    client = BeaconBox()  # reads BEACONBOX_API_KEY

    result = client.messages.push(
        recipient_email="buyer@example.com",
        subject="Your order has shipped",
        body="Tracking XY123456789EE. Estimated delivery Thursday.",
    )
    print(result.id)

Everything is also available awaited, from :class:`AsyncBeaconBox`, with identical signatures.

The two conventions this SDK exists to keep you from having to remember are on
:class:`BeaconBox`: it supplies the idempotency key every write requires, and it never raises on
a channel that was skipped.
"""

from ._logging import install_null_handler
from ._version import __version__
from .client import AsyncBeaconBox, BeaconBox
from .enums import (
    Channel,
    ChannelSendStatus,
    EmailSkipReason,
    MessageKind,
    MessageStatus,
    RecipientStatus,
    SkipReason,
    WebhookEventType,
    WhatsAppReplyForward,
)
from .errors import (
    APIConnectionError,
    APIError,
    AuthenticationError,
    BeaconBoxError,
    ConflictError,
    InvalidRequestError,
    PermissionDeniedError,
    RateLimitError,
    ResourceMissingError,
    ServerError,
    WebhookVerificationError,
)
from .models import (
    ApiKey,
    BatchItemResult,
    BatchResult,
    CreditBalance,
    DeliveryStatus,
    Message,
    MessagePage,
    MessagePush,
    MessagePushResult,
    NewApiKey,
    NewWebhookEndpoint,
    NotSent,
    RecipientSms,
    RetractResult,
    SmsDelivery,
    SmsOutcome,
    WebhookEndpoint,
    WebhookEvent,
    WhatsAppErasureReceipt,
    WhatsAppOptInOutcome,
    WhatsAppOutcome,
)
from .retry import RetryPolicy

# A library must be silent until the application asks otherwise. See beaconbox._logging: this
# attaches a NullHandler to the `beaconbox` logger and configures nothing else, so importing this
# SDK never prints anything and never touches the root logger.
install_null_handler()

__all__ = [
    "APIConnectionError",
    "APIError",
    "ApiKey",
    "AsyncBeaconBox",
    "AuthenticationError",
    "BatchItemResult",
    "BatchResult",
    "BeaconBox",
    "BeaconBoxError",
    "Channel",
    "ChannelSendStatus",
    "ConflictError",
    "CreditBalance",
    "DeliveryStatus",
    "EmailSkipReason",
    "InvalidRequestError",
    "Message",
    "MessageKind",
    "MessagePage",
    "MessagePush",
    "MessagePushResult",
    "MessageStatus",
    "NewApiKey",
    "NewWebhookEndpoint",
    "NotSent",
    "PermissionDeniedError",
    "RateLimitError",
    "RecipientSms",
    "RecipientStatus",
    "ResourceMissingError",
    "RetractResult",
    "RetryPolicy",
    "ServerError",
    "SkipReason",
    "SmsDelivery",
    "SmsOutcome",
    "WebhookEndpoint",
    "WebhookEvent",
    "WebhookEventType",
    "WebhookVerificationError",
    "WhatsAppErasureReceipt",
    "WhatsAppOptInOutcome",
    "WhatsAppOutcome",
    "WhatsAppReplyForward",
    "__version__",
]
