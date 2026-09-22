"""The synchronous resources, and the canonical documentation for every operation.

:mod:`beaconbox.async_resources` mirrors this module method for method. Where an async docstring
is short, this is the one it points at.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import datetime

from . import _ops
from ._transport import SyncTransport
from .enums import Channel, MessageKind, WebhookEventType
from .errors import BeaconBoxError
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
    RecipientSms,
    RetractResult,
    SmsOutcome,
    WebhookEndpoint,
    WhatsAppErasureReceipt,
    WhatsAppOutcome,
)

__all__ = ["Credits", "Keys", "Messages", "Recipients", "WebhookEndpoints"]

DEFAULT_PAGE_SIZE = 50


class _Resource:
    def __init__(self, transport: SyncTransport) -> None:
        self._transport = transport


class Messages(_Resource):
    """Pushing updates, and everything that happens to one afterwards.

    **Read the response, not the status code.** A push answers 201 even when the SMS or WhatsApp
    message was not sent. The update is in the customer's inbox and the email nudge has gone, so
    a paid channel that could not send reports a ``skipped_reason`` and the request still
    succeeded. Nothing in this SDK raises on a skip, deliberately: treating one as an error is
    what invites a retry, and a retry of a push that already succeeded is a second message to a
    real person.
    """

    def push(
        self,
        *,
        recipient_email: str,
        subject: str,
        body: str,
        kind: MessageKind | str = MessageKind.ONE_OFF,
        reference: str | None = None,
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

        The customer gets an email whose link opens their inbox already signed in. No password,
        no account to create.

        :param recipient_email: Who this is for. They do not need a BeaconBox account.
        :param subject: What the update is about. For ``kind="updateable"`` this is also the
            **matching key**: pushing the same subject again overwrites that message in place.
        :param body: Plain text and clickable links only.
        :param kind: ``one_off`` always creates a new message and nudges.
            ``updateable`` creates and nudges the first time, then updates in place quietly, which
            is what you want for a shipping estimate that moves twice before the parcel arrives.
        :param reference: Your own order number, for example ``#A-10294``. Letters, digits and
            ``# - _ . /`` only, no spaces, at most 32 characters. It appears in the WhatsApp nudge,
            which Meta reviews, so it has to be an identifier rather than a sentence.
        :param recipient_phone: Mobile number for the SMS nudge, E.164 preferred. Stored against
            this recipient for your business and reused on later pushes, so it is optional once
            you have sent it once.
        :param channels: Override your account's channel settings for this one request. Unset
            follows the account defaults. Listing both ``sms`` and ``whatsapp`` sends two messages
            and costs two credits. Email is always sent regardless. An explicit request overrides
            your account default only: it never overrides a country restriction, a recipient who
            opted out, or a recipient who never opted in to WhatsApp.
        :param notify: Unset applies the default (create nudges, in-place update stays silent).
            ``True`` forces a nudge on an update, ``False`` suppresses one even on first create.
        :param send_at: Hold the *nudge* until this moment. **Timezone required**, and a naive
            datetime is refused here rather than becoming a 422. The message is readable
            immediately either way: only the ping waits, because the inbox is the source of truth
            and the nudge is what should land at a civilised hour. At most 90 days out.
        :param escalate_if_unread_after_minutes: **Hold the paid channel back until the recipient
            has had a chance to read the email.** The SMS or WhatsApp message named in
            ``channels`` is not sent now. It is sent only if they still have not opened the
            message after this many minutes (5 to 10080). If they open it first, nothing is sent
            and nothing is charged. That inverts what a paid channel usually costs you: instead of
            paying to interrupt everybody, you pay only for the people the email did not reach.
        :param obsoletes: Ids of your earlier messages to grey out, for when this update replaces
            them.
        :param whatsapp_opt_in_source: Record that this recipient agreed to be messaged on
            WhatsApp, naming the surface where they agreed in your own words, for example
            ``"checkout tickbox"``. This is the audit answer to "prove they agreed", so it must
            name a real surface of yours. Recording consent is not sending, so it works while your
            WhatsApp channel is still off. Consent is per business and reaches no other merchant.
        :param idempotency_key: See below. Generated when omitted.

        **Idempotency.** Every write carries an ``Idempotency-Key``. This SDK generates one per
        call and reuses it across its own retries, so a connection that dies with the answer in
        flight cannot become a duplicate email and a duplicate charged SMS. **Pass your own
        whenever you have a natural key** (an order id, a job id): then a retry from anywhere,
        your queue, a cron, a human clicking twice, collapses onto the same key rather than only
        the retries this SDK makes internally.

        :returns: A :class:`~beaconbox.models.MessagePushResult`. Check ``result.sms`` and
            ``result.whatsapp`` for what the paid channels did.

        .. code-block:: python

            result = client.messages.push(
                recipient_email="buyer@example.com",
                subject="Your order has shipped",
                body="Tracking XY123456789EE. Estimated delivery Thursday.",
                kind="updateable",
                channels=["sms"],
                recipient_phone="+37255550134",
                idempotency_key="order-4711-shipped",
            )

            if result.sms and result.sms.skipped_reason == SkipReason.INSUFFICIENT_CREDIT:
                # Top up, then: client.messages.resend_sms(result.id)
                ...
        """
        message = MessagePush(
            recipient_email=recipient_email,
            subject=subject,
            body=body,
            kind=kind,
            reference=reference,
            recipient_phone=recipient_phone,
            channels=tuple(channels) if channels is not None else None,
            notify=notify,
            send_at=send_at,
            escalate_if_unread_after_minutes=escalate_if_unread_after_minutes,
            obsoletes=tuple(obsoletes),
            whatsapp_opt_in_source=whatsapp_opt_in_source,
            sms_if_whatsapp_fails=sms_if_whatsapp_fails,
        )
        return self._transport.invoke(_ops.push(message.to_payload()), idempotency_key)

    def push_batch(
        self, messages: Sequence[MessagePush], idempotency_key: str | None = None
    ) -> BatchResult:
        """Up to 100 pushes in one call.

        A list of complete, individual pushes, not one message fanned out to many recipients:
        fifty orders means fifty tracking numbers.

        **Always answers 200. Read** :attr:`~beaconbox.models.BatchResult.failed` **, not the
        status code.** One rejected item does not fail the batch, because a batch that aborted at
        item 7 would leave items 8 onwards unsent with nothing to say which.

        Safe to retry: each item carries its own key derived from the batch's, so a retry after a
        crash replays the items that already landed instead of pushing them again.

        .. code-block:: python

            result = client.messages.push_batch([
                MessagePush(recipient_email="a@example.com", subject="Shipped", body="..."),
                MessagePush(recipient_email="b@example.com", subject="Shipped", body="..."),
            ])
            for item in result.failures:
                log.warning("item %d rejected: %s", item.index, item.error_code)
        """
        return self._transport.invoke(
            _ops.push_batch([m.to_payload() for m in messages]), idempotency_key
        )

    def get(self, public_id: str) -> Message:
        """One message and its delivery status.

        Delivery and opens arrive asynchronously, so this is the poll-shaped answer to the same
        question a webhook subscription answers by being told.
        """
        return self._transport.invoke(_ops.get_message(public_id))

    def list(
        self,
        recipient_email: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> MessagePage:
        """One page of messages, newest first.

        Keyset-paginated: pass the previous page's ``next_cursor`` back as ``cursor``. Prefer
        :meth:`iterate` unless you are pausing between pages, for instance to render them.
        """
        return self._transport.invoke(_ops.list_messages(recipient_email, limit, cursor))

    def iterate(
        self, recipient_email: str | None = None, page_size: int = DEFAULT_PAGE_SIZE
    ) -> Iterator[Message]:
        """Every message, following cursors.

        A generator rather than a list: a merchant with a year of history should not have to hold
        it in memory to count it.

        .. code-block:: python

            for message in client.messages.iterate(recipient_email="buyer@example.com"):
                print(message.id, message.delivery.opened)

        Stops if the server ever hands back a cursor it has already given, rather than looping on
        the same page forever. That is a server bug if it happens, but the shape it takes in a
        merchant's process is a worker that never returns and a generator that never ends, which
        is far harder to diagnose than the exception raised here.
        """
        cursor: str | None = None
        seen: set[str] = set()
        while True:
            page = self.list(recipient_email, page_size, cursor)
            yield from page.items
            cursor = page.next_cursor
            if not cursor:
                return
            if cursor in seen:
                raise BeaconBoxError(
                    f"BeaconBox: pagination did not advance (cursor {cursor!r} repeated). "
                    "Stopping rather than looping forever."
                )
            seen.add(cursor)

    def resend_sms(self, public_id: str, idempotency_key: str | None = None) -> SmsOutcome:
        """Send the SMS for a message whose text never went out.

        The recovery path for a ``skipped_reason``, most often an empty balance that has since
        been topped up.

        Refused with :class:`~beaconbox.errors.ConflictError` if one is already queued or
        delivered. The only thing a second send would add is a second charge and a second
        interruption.
        """
        return self._transport.invoke(_ops.resend_sms(public_id), idempotency_key)

    def resend_whatsapp(
        self, public_id: str, idempotency_key: str | None = None
    ) -> WhatsAppOutcome:
        """The same, on WhatsApp.

        A separate call rather than a channel parameter, because a request naming both channels
        would have to mean charging twice and interrupting twice for one update.
        """
        return self._transport.invoke(_ops.resend_whatsapp(public_id), idempotency_key)

    def retract(self, public_id: str, idempotency_key: str | None = None) -> RetractResult:
        """Take a message back, and call off any nudge still queued for it.

        Marked withdrawn for the recipient rather than deleted: they may already have read it. A
        cancelled send costs nothing, because credits are charged at send time.

        Idempotent. A second retraction reports ``retracted=False`` and the current status, which
        is not a failure. Check
        :attr:`~beaconbox.models.RetractResult.already_notified` to learn whether you were in
        time: true means an email or text about it is already out and cannot be recalled.
        """
        return self._transport.invoke(_ops.retract(public_id), idempotency_key)


class Credits(_Resource):
    """The prepaid balance. One balance, shared by SMS and WhatsApp."""

    def balance(self) -> CreditBalance:
        """Credits remaining, and whether that is below your low-balance threshold.

        SMS and WhatsApp stop at zero. Email is unaffected, so a customer never stops receiving
        their order updates because a balance ran out.
        """
        return self._transport.invoke(_ops.credit_balance())


class Recipients(_Resource):
    """A recipient's contact details, and their right to be forgotten.

    Reads return a **masked** number. An API key that leaks should not be usable to dump a phone
    book.
    """

    def sms(self, email: str) -> RecipientSms:
        """This recipient's stored number (masked) and SMS consent state."""
        return self._transport.invoke(_ops.recipient_sms(email))

    def set_phone(self, email: str, phone: str) -> RecipientSms:
        """Store a number, normalised to E.164.

        Unlike a push, this one fails loudly: storing the number is the entire request, so an
        unparseable number raises :class:`~beaconbox.errors.InvalidRequestError` and a clash with
        another of your recipients raises :class:`~beaconbox.errors.ConflictError`.
        """
        return self._transport.invoke(_ops.set_recipient_phone(email, phone))

    def clear_phone(self, email: str) -> RecipientSms:
        """Forget the number. Their inbox and their email nudges are unaffected."""
        return self._transport.invoke(_ops.clear_recipient_phone(email))

    def erase_whatsapp(
        self, email: str, idempotency_key: str | None = None
    ) -> WhatsAppErasureReceipt:
        """Erase this recipient's WhatsApp history for your business. **Not undoable.**

        The API half of an Article 17 request. It deletes their stored replies, removes the copies
        inside webhook deliveries still queued for your endpoint, and withdraws WhatsApp consent
        permanently for your business.

        **Read**
        :attr:`~beaconbox.models.WhatsAppErasureReceipt.replies_a_forward_email_may_have_carried`
        **in the receipt.** It counts erased replies whose words may already be sitting in *your*
        mailboxes, which nothing here can reach. That number is your remaining work, and until you
        act on it the erasure is only ours.
        """
        return self._transport.invoke(_ops.erase_whatsapp(email), idempotency_key)


class Keys(_Resource):
    """API keys.

    A minted key is shown **once**. BeaconBox stores a hash, so the create response is the only
    place the full value will ever exist.
    """

    def list(self) -> tuple[ApiKey, ...]:
        """Your keys, masked. Useful for auditing what exists and revoking what should not."""
        return self._transport.invoke(_ops.list_keys())

    def create(self, name: str | None = None, idempotency_key: str | None = None) -> NewApiKey:
        """Mint a key. Name it after the service that will hold it, not the person creating it.

        The returned :attr:`~beaconbox.models.NewApiKey.key` is unrecoverable. Write it to a
        secret store before the process exits. Its ``repr`` is redacted so an incidental log line
        cannot leak it, so read the attribute explicitly.
        """
        return self._transport.invoke(_ops.create_key(name), idempotency_key)

    def revoke(self, key_id: str) -> None:
        """Revoke a key immediately. In-flight requests using it start failing with 401.

        Revoking the key the calling client is authenticated with is allowed, and is the correct
        response to a leak even though the next call from this client will fail.
        """
        self._transport.invoke(_ops.revoke_key(key_id))


class WebhookEndpoints(_Resource):
    """Where BeaconBox pushes delivery outcomes.

    Verify what arrives with :func:`beaconbox.webhooks.verify`.
    """

    def list(self) -> tuple[WebhookEndpoint, ...]:
        """Registered endpoints, secrets masked.

        Worth checking :attr:`~beaconbox.models.WebhookEndpoint.disabled` here: an endpoint
        switched off after sustained delivery failure is silent, and silence looks the same as
        nothing having happened.
        """
        return self._transport.invoke(_ops.list_webhook_endpoints())

    def create(
        self,
        url: str,
        event_types: Sequence[WebhookEventType | str] = (),
        description: str = "",
        idempotency_key: str | None = None,
    ) -> NewWebhookEndpoint:
        """Register an endpoint. The response carries the **real** signing secret, once.

        ``url`` must be https and must resolve to a public address. Private, loopback and
        link-local addresses are refused, and redirects are never followed, because a public URL
        that redirects to an internal one is the cheapest way around a firewall.

        **Leave ``event_types`` empty to receive everything**, including types added later. That
        is the recommended setting: a narrow subscription is how a new event type silently passes
        an integration by.
        """
        return self._transport.invoke(
            _ops.create_webhook_endpoint(url, [str(_value(t)) for t in event_types], description),
            idempotency_key,
        )

    def delete(self, public_id: str) -> None:
        """Delete an endpoint. Deliveries already queued for it stop.

        Also the way to re-enable one that was auto-disabled: delete it and register it again,
        which rotates the secret.
        """
        self._transport.invoke(_ops.delete_webhook_endpoint(public_id))


def _value(item: WebhookEventType | str) -> str:
    return item.value if isinstance(item, WebhookEventType) else item
