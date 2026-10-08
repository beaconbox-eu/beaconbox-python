"""Every API operation, described once.

A resource method on the sync client and its twin on the async client build the *same*
:class:`~beaconbox._core.Call` from this module, so the URL, the body shape and the response type
of an operation have exactly one definition. What differs between the two clients is the keyword
``await``, and nothing else.

The prose that teaches an operation lives on the resource methods, where an editor will show it.
What lives here is the wire contract.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ._core import Call, erasable_address, path_segment
from .models import (
    ApiKey,
    BatchResult,
    CreditBalance,
    Message,
    MessagePage,
    MessagePushResult,
    NewApiKey,
    NewWebhookEndpoint,
    RecipientErasureReport,
    RecipientSms,
    RetractResult,
    SmsOutcome,
    WebhookEndpoint,
    WebhookTestResult,
    WhatsAppErasureReceipt,
    WhatsAppOutcome,
)

Payload = dict[str, Any]


def _nothing(_: Any) -> None:
    """For a 204. The call succeeded and the body is empty by design."""
    return None


def _keys(body: Any) -> tuple[ApiKey, ...]:
    items = body.get("items") if isinstance(body, dict) else None
    return tuple(ApiKey.from_api(i) for i in (items or []) if isinstance(i, dict))


def _endpoints(body: Any) -> tuple[WebhookEndpoint, ...]:
    items = body.get("items") if isinstance(body, dict) else None
    return tuple(WebhookEndpoint.from_api(i) for i in (items or []) if isinstance(i, dict))


# --- Messages ---------------------------------------------------------------------------


def push(body: Payload) -> Call[MessagePushResult]:
    return Call("POST", "/messages", MessagePushResult.from_api, body=body)


def push_batch(bodies: Sequence[Payload]) -> Call[BatchResult]:
    # `items` is the key the API declares for a batch body. Any other name is a 422.
    return Call("POST", "/messages/batch", BatchResult.from_api, body={"items": list(bodies)})


def get_message(public_id: str) -> Call[Message]:
    return Call(
        "GET", f"/messages/{path_segment(public_id)}", Message.from_api, route="/messages/{id}"
    )


def list_messages(
    recipient_email: str | None, limit: int | None, cursor: str | None
) -> Call[MessagePage]:
    return Call(
        "GET",
        "/messages",
        MessagePage.from_api,
        params={"recipient_email": recipient_email, "limit": limit, "cursor": cursor},
    )


def resend_sms(public_id: str) -> Call[SmsOutcome]:
    return Call(
        "POST",
        f"/messages/{path_segment(public_id)}/sms",
        SmsOutcome.from_api,
        route="/messages/{id}/sms",
    )


def resend_whatsapp(public_id: str) -> Call[WhatsAppOutcome]:
    return Call(
        "POST",
        f"/messages/{path_segment(public_id)}/whatsapp",
        WhatsAppOutcome.from_api,
        route="/messages/{id}/whatsapp",
    )


def retract(public_id: str) -> Call[RetractResult]:
    return Call(
        "POST",
        f"/messages/{path_segment(public_id)}/retract",
        RetractResult.from_api,
        route="/messages/{id}/retract",
    )


# --- Credits ----------------------------------------------------------------------------


def credit_balance() -> Call[CreditBalance]:
    return Call("GET", "/credits", CreditBalance.from_api)


# --- Recipients -------------------------------------------------------------------------


def recipient_sms(email: str) -> Call[RecipientSms]:
    return Call(
        "GET",
        f"/recipients/{path_segment(email)}/sms",
        RecipientSms.from_api,
        route="/recipients/{email}/sms",
    )


def set_recipient_phone(email: str, phone: str) -> Call[RecipientSms]:
    return Call(
        "PUT",
        f"/recipients/{path_segment(email)}/phone",
        RecipientSms.from_api,
        body={"phone": phone},
        route="/recipients/{email}/phone",
    )


def clear_recipient_phone(email: str) -> Call[RecipientSms]:
    return Call(
        "DELETE",
        f"/recipients/{path_segment(email)}/phone",
        RecipientSms.from_api,
        route="/recipients/{email}/phone",
    )


def erase_whatsapp(email: str) -> Call[WhatsAppErasureReceipt]:
    return Call(
        "POST",
        f"/recipients/{path_segment(erasable_address(email))}/whatsapp/erase",
        WhatsAppErasureReceipt.from_api,
        route="/recipients/{email}/whatsapp/erase",
    )


def erase_recipient(email: str) -> Call[RecipientErasureReport]:
    return Call(
        "POST",
        f"/recipients/{path_segment(erasable_address(email))}/erase",
        RecipientErasureReport.from_api,
        route="/recipients/{email}/erase",
    )


# --- Keys -------------------------------------------------------------------------------


def list_keys() -> Call[tuple[ApiKey, ...]]:
    return Call("GET", "/keys", _keys)


def create_key(name: str | None, *, detach: bool = False) -> Call[NewApiKey]:
    body: dict[str, Any] = {"name": name}
    # Sent only when asked for, so a plain create is the same request it always was.
    if detach:
        body["detach"] = True
    return Call("POST", "/keys", NewApiKey.from_api, body=body)


def revoke_key(key_id: str) -> Call[None]:
    return Call("DELETE", f"/keys/{path_segment(key_id)}", _nothing, route="/keys/{id}")


# --- Webhook endpoints ------------------------------------------------------------------


def list_webhook_endpoints() -> Call[tuple[WebhookEndpoint, ...]]:
    return Call("GET", "/webhook-endpoints", _endpoints)


def create_webhook_endpoint(
    url: str, event_types: Sequence[str], description: str
) -> Call[NewWebhookEndpoint]:
    return Call(
        "POST",
        "/webhook-endpoints",
        NewWebhookEndpoint.from_api,
        body={"url": url, "event_types": list(event_types), "description": description},
    )


def test_webhook_endpoint(public_id: str) -> Call[WebhookTestResult]:
    return Call(
        "POST",
        f"/webhook-endpoints/{path_segment(public_id)}/test",
        WebhookTestResult.from_api,
        route="/webhook-endpoints/{id}/test",
        # No body, and no idempotency key: this writes nothing. Sending the same test twice is two
        # tests, which is what somebody pressing it twice means.
        body={},
    )


def delete_webhook_endpoint(public_id: str) -> Call[None]:
    return Call(
        "DELETE",
        f"/webhook-endpoints/{path_segment(public_id)}",
        _nothing,
        route="/webhook-endpoints/{id}",
    )
