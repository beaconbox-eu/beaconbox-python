"""The asynchronous resources: :mod:`beaconbox.resources`, awaited.

Every method here builds the same :class:`~beaconbox._core.Call` as its synchronous twin, from
the same :mod:`beaconbox._ops` definition, and runs the same retry policy. The signatures match
exactly. Only the ``await`` differs.

Docstrings here are short on purpose and point at the sync class, which carries the full contract
for each operation. Two copies of the same paragraph is two paragraphs that drift.

Nothing in this module blocks the event loop: requests are awaited on ``httpx.AsyncClient`` and
retry backoff is ``asyncio.sleep``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import datetime

from . import _ops
from ._transport import AsyncTransport
from .enums import Channel, MessageKind, OrderStatus, WebhookEventType
from .models import (
    ApiKey,
    BatchResult,
    CreditBalance,
    Message,
    MessagePage,
    MessagePush,
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
from .resources import DEFAULT_PAGE_SIZE, _check_page_size, _PageWalk, _value

__all__ = [
    "AsyncCredits",
    "AsyncKeys",
    "AsyncMessages",
    "AsyncRecipients",
    "AsyncWebhookEndpoints",
]


class _AsyncResource:
    def __init__(self, transport: AsyncTransport) -> None:
        self._transport = transport


class AsyncMessages(_AsyncResource):
    """Pushing updates. See :class:`beaconbox.resources.Messages` for the full contract.

    **Read the response, not the status code.** A push answers 201 even when the SMS or WhatsApp
    message was not sent, and nothing here raises on a skip.
    """

    async def push(
        self,
        *,
        recipient_email: str,
        subject: str,
        body: str,
        kind: MessageKind | str = MessageKind.ONE_OFF,
        reference: str | None = None,
        order_status: OrderStatus | str | None = None,
        recipient_phone: str | None = None,
        channels: Sequence[Channel | str] | None = None,
        notify: bool | None = None,
        send_at: datetime | None = None,
        escalate_if_unread_after_minutes: int | None = None,
        obsoletes: Sequence[str] = (),
        whatsapp_opt_in_source: str | None = None,
        sms_if_whatsapp_fails: bool = False,
        idempotency_key: str | None = None,
    ) -> MessagePushResult:
        """Push an update to a customer.

        Async form of :meth:`beaconbox.resources.Messages.push`, which documents every argument.

        The three things to carry over: check ``result.sms`` and ``result.whatsapp`` rather than
        the status code; ``channels`` can only narrow what your account settings allow, never
        switch a channel on; and pass your own ``idempotency_key`` whenever you have a natural one,
        so a retry from your queue collapses onto the same key.
        """
        message = MessagePush(
            recipient_email=recipient_email,
            subject=subject,
            body=body,
            kind=kind,
            reference=reference,
            order_status=order_status,
            recipient_phone=recipient_phone,
            channels=tuple(channels) if channels is not None else None,
            notify=notify,
            send_at=send_at,
            escalate_if_unread_after_minutes=escalate_if_unread_after_minutes,
            obsoletes=tuple(obsoletes),
            whatsapp_opt_in_source=whatsapp_opt_in_source,
            sms_if_whatsapp_fails=sms_if_whatsapp_fails,
        )
        return await self._transport.invoke(_ops.push(message.to_payload()), idempotency_key)

    async def push_batch(
        self, messages: Sequence[MessagePush], idempotency_key: str | None = None
    ) -> BatchResult:
        """Up to 100 pushes in one call. Answers 200 when processed: read ``result.failed``.

        A 409 ``idempotency.request_in_progress`` raises :class:`~beaconbox.errors.ConflictError`
        for the whole batch, never for one item: the same key is still running it. Retry the whole
        batch later with the same key. A batch ``idempotency_key`` is limited to 252 characters. See
        :meth:`beaconbox.resources.Messages.push_batch`.
        """
        return await self._transport.invoke(
            _ops.push_batch([m.to_payload() for m in messages]), idempotency_key
        )

    async def get(self, public_id: str) -> Message:
        """One message and its delivery status."""
        return await self._transport.invoke(_ops.get_message(public_id))

    async def list(
        self,
        recipient_email: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> MessagePage:
        """One page of messages, newest first. Keyset-paginated on ``next_cursor``."""
        return await self._transport.invoke(_ops.list_messages(recipient_email, limit, cursor))

    async def iterate(
        self, recipient_email: str | None = None, page_size: int = DEFAULT_PAGE_SIZE
    ) -> AsyncIterator[Message]:
        """Every message, following cursors, as an async generator.

        .. code-block:: python

            async for message in client.messages.iterate():
                ...

        Stops if the server ever hands back a cursor it has already given. See
        :meth:`beaconbox.resources.Messages.iterate`.
        """
        _check_page_size(page_size)
        walk = _PageWalk()
        cursor: str | None = None
        while True:
            page = await self.list(recipient_email, page_size, cursor)
            for item in page.items:
                yield item
            cursor = walk.advance(page.next_cursor)
            if cursor is None:
                return

    async def resend_sms(self, public_id: str, idempotency_key: str | None = None) -> SmsOutcome:
        """Send the SMS for a message whose text never went out.

        Raises :class:`~beaconbox.errors.ConflictError` while one is still queued or after one went.
        See :meth:`beaconbox.resources.Messages.resend_sms`.
        """
        return await self._transport.invoke(_ops.resend_sms(public_id), idempotency_key)

    async def resend_whatsapp(
        self, public_id: str, idempotency_key: str | None = None
    ) -> WhatsAppOutcome:
        """The same, on WhatsApp. See :meth:`beaconbox.resources.Messages.resend_whatsapp`."""
        return await self._transport.invoke(_ops.resend_whatsapp(public_id), idempotency_key)

    async def retract(self, public_id: str, idempotency_key: str | None = None) -> RetractResult:
        """Take a message back and call off any queued nudge.

        Idempotent. Check ``already_notified`` to learn whether you were in time.
        """
        return await self._transport.invoke(_ops.retract(public_id), idempotency_key)


class AsyncCredits(_AsyncResource):
    """The prepaid balance. See :class:`beaconbox.resources.Credits`."""

    async def balance(self) -> CreditBalance:
        """Credits remaining, and whether that is below your low-balance threshold."""
        return await self._transport.invoke(_ops.credit_balance())


class AsyncRecipients(_AsyncResource):
    """Contact details and erasure. See :class:`beaconbox.resources.Recipients`.

    Reads return a masked number.
    """

    async def sms(self, email: str) -> RecipientSms:
        """This recipient's stored number (masked) and SMS consent state."""
        return await self._transport.invoke(_ops.recipient_sms(email))

    async def set_phone(self, email: str, phone: str) -> RecipientSms:
        """Store a number, normalised to E.164. Fails loudly on an unparseable one.

        Premium-rate, toll-free, shared-cost, voicemail and service numbers, and satellite and
        international codes, are refused too (``sms.phone_invalid``). See
        :meth:`beaconbox.resources.Recipients.set_phone`.
        """
        return await self._transport.invoke(_ops.set_recipient_phone(email, phone))

    async def clear_phone(self, email: str) -> RecipientSms:
        """Forget the number."""
        return await self._transport.invoke(_ops.clear_recipient_phone(email))

    async def erase_whatsapp(
        self, email: str, idempotency_key: str | None = None
    ) -> WhatsAppErasureReceipt:
        """Erase this recipient's WhatsApp history for your business. **Not undoable.**

        Read ``replies_a_forward_email_may_have_carried`` in the receipt: it is the part of the
        erasure only you can finish. A blank or non-address ``email`` raises :class:`ValueError`
        before anything is sent. See :meth:`beaconbox.resources.Recipients.erase_whatsapp`.
        """
        return await self._transport.invoke(_ops.erase_whatsapp(email), idempotency_key)

    async def erase(self, email: str, idempotency_key: str | None = None) -> RecipientErasureReport:
        """Erase everything held about this recipient for your business. **Not undoable.**

        Deletes their messages, events, send records, WhatsApp replies and recipient record, and
        keeps only their email suppressions (an unsubscribe among them, which erasing does not
        undo) and SMS or WhatsApp opt-outs, so they cannot be contacted again. A blank or
        non-address ``email`` raises :class:`ValueError` before
        anything is sent. See :meth:`beaconbox.resources.Recipients.erase` for the full list.
        """
        return await self._transport.invoke(_ops.erase_recipient(email), idempotency_key)


class AsyncKeys(_AsyncResource):
    """API keys. A minted key is shown once. See :class:`beaconbox.resources.Keys`."""

    async def list(self) -> tuple[ApiKey, ...]:
        """Your keys, masked."""
        return await self._transport.invoke(_ops.list_keys())

    async def create(
        self, name: str | None = None, idempotency_key: str | None = None, *, detach: bool = False
    ) -> NewApiKey:
        """Mint a key. The returned ``key`` is unrecoverable: store it before the process exits.
        ``detach=True`` mints one that survives revoking this client's key, for rotation."""
        return await self._transport.invoke(_ops.create_key(name, detach=detach), idempotency_key)

    async def revoke(self, key_id: str) -> None:
        """Revoke a key immediately."""
        await self._transport.invoke(_ops.revoke_key(key_id))


class AsyncWebhookEndpoints(_AsyncResource):
    """Delivery callbacks. See :class:`beaconbox.resources.WebhookEndpoints`."""

    async def list(self) -> tuple[WebhookEndpoint, ...]:
        """Registered endpoints, secrets masked. Check ``disabled``."""
        return await self._transport.invoke(_ops.list_webhook_endpoints())

    async def create(
        self,
        url: str,
        event_types: Sequence[WebhookEventType | str] = (),
        description: str = "",
        idempotency_key: str | None = None,
    ) -> NewWebhookEndpoint:
        """Register an endpoint. The response carries the real signing secret, once.

        Leave ``event_types`` empty to receive everything, including types added later.
        """
        return await self._transport.invoke(
            _ops.create_webhook_endpoint(url, [_value(t) for t in event_types], description),
            idempotency_key,
        )

    async def test(self, public_id: str) -> WebhookTestResult:
        """Send one sample ``ping`` to this endpoint and report what came back.

        See :meth:`beaconbox.resources.WebhookEndpoints.test`, same contract, awaited.
        """
        return await self._transport.invoke(_ops.test_webhook_endpoint(public_id))

    async def delete(self, public_id: str) -> None:
        """Delete an endpoint."""
        await self._transport.invoke(_ops.delete_webhook_endpoint(public_id))
