"""The synchronous resources, and the canonical documentation for every operation.

:mod:`beaconbox.async_resources` mirrors this module method for method. Where an async docstring
is short, this is the one it points at.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import datetime

from . import _ops
from ._transport import SyncTransport
from .enums import Channel, MessageKind, OrderStatus, WebhookEventType
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
    RecipientErasureReport,
    RecipientSms,
    RetractResult,
    SmsOutcome,
    WebhookEndpoint,
    WebhookTestResult,
    WhatsAppErasureReceipt,
    WhatsAppOutcome,
)

__all__ = ["Credits", "Keys", "Messages", "Recipients", "WebhookEndpoints"]

DEFAULT_PAGE_SIZE = 50


class _PageWalk:
    """Guards a cursor walk against a server that never finishes it.

    Shared by the sync and the async ``iterate`` so the rule has one definition.

    A repeated cursor is a server bug. What makes it worth a client-side check is the shape it
    takes in a merchant's process: a worker that never returns and a generator that never ends,
    which is far harder to diagnose than an exception.
    """

    __slots__ = ("_seen",)

    def __init__(self) -> None:
        self._seen: set[str] = set()

    def advance(self, cursor: str | None) -> str | None:
        """The cursor to request next, or ``None`` when the walk is over.

        Not called ``next``: this is not an iterator, and a method of that name on an object handed
        to a ``while`` loop invites a reader to assume it is one.

        :raises BeaconBoxError: if ``cursor`` has been handed out before.
        """
        if not cursor:
            return None
        if cursor in self._seen:
            raise BeaconBoxError(
                f"BeaconBox: pagination did not advance (cursor {cursor!r} repeated). "
                "Stopping rather than looping forever."
            )
        self._seen.add(cursor)
        return cursor


def _check_page_size(page_size: int) -> None:
    """Reject a page size that cannot mean what the caller intended.

    The server clamps ``limit`` into its own range, so ``page_size=0`` is not an error there — it
    quietly becomes one message per request, and a merchant walking a year of history discovers
    that as a job that takes hours rather than as a mistake in their call.

    Both callers are generators, so this runs on the first ``next()`` rather than at the call that
    built them. That is ordinary generator behaviour and the tests pin it: nothing reaches the
    network first.
    """
    if page_size < 1:
        raise ValueError(f"BeaconBox: page_size must be at least 1, got {page_size}")


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
            ``# - _ . /`` only, no spaces, at most 32 characters. It appears in the WhatsApp and
            the SMS nudge, so it has to be an identifier rather than a sentence, and a nudge shows
            it only when it passes a stricter display rule: an optional leading ``#``, then a
            letter or digit, then letters, digits and ``_ / # -``, with at most two dots, each
            directly before a digit (``INV.2026`` is shown, ``shop.com`` is not). A reference
            that fails the rule is stored and echoed but never shown, and such a push sends no
            WhatsApp message.
        :param order_status: What happened to the order, as an
            :class:`~beaconbox.enums.OrderStatus` or its string value (``"shipped"``, say). A
            plain string is sent as given, so a status the API adds later works before this SDK
            knows it. BeaconBox picks the platform-written WhatsApp and SMS wording from it, for
            example "Your order #A-10294 from PhonicBloom has shipped." **A WhatsApp nudge needs
            both** ``reference`` **and** ``order_status``: missing either, WhatsApp is skipped with
            ``template_not_sendable`` and nothing is charged. An in-place update replaces it, as it
            does ``reference``, so an update without it clears it.
        :param recipient_phone: Mobile number for the SMS nudge, E.164 preferred. Stored against
            this recipient for your business and reused on later pushes, so it is optional once
            you have sent it once. A number that cannot be parsed, or cannot be a recipient's phone
            (premium-rate, toll-free, shared-cost, voicemail, service, or a satellite or
            international code such as +881), refuses the whole push with
            :class:`~beaconbox.errors.InvalidRequestError` (``sms.phone_invalid``) before anything
            is written, so you can correct it and retry with the same key.
        :param channels: Which paid channels this push asks for. **A request that can only
            narrow, never switch a channel on.** Unset follows your account settings. Leaving a
            channel out of the list suppresses it for this push. Naming one sends on it only where
            your settings already allow it: a channel in ``off`` mode stays off (``sms_disabled``,
            ``whatsapp_disabled``), one BeaconBox has not yet enabled for your business never
            sends (``sms_not_enabled``, ``whatsapp_not_enabled``), and one in ``on_request`` mode
            sends only on a push that names it. Naming a channel never overrides a country
            restriction, a recipient who opted out, or a recipient who never opted in to WhatsApp.
            Listing both ``sms`` and ``whatsapp`` sends two messages and costs two credits. Email
            is always sent regardless.
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
            The minutes count from the email: from ``send_at`` when you set one, otherwise from
            the push, and if the email is still queued at the deadline it moves to this many
            minutes after the email goes. An email that bounced, failed or was skipped (a
            suppressed address, say) counts as unread, so the paid channel is still sent, subject
            to its own consent and settings: that is the case the paid channel exists for. Only a
            push that queued no nudge at all never escalates. A channel in ``on_request`` mode that
            this push named still sends at the deadline.
        :param obsoletes: Ids of your earlier messages to grey out, for when this update replaces
            them. At most 100 ids of at most 64 characters each; more is refused with
            :class:`~beaconbox.errors.InvalidRequestError`.
        :param whatsapp_opt_in_source: Record that this recipient agreed to be messaged on
            WhatsApp, naming the surface where they agreed in your own words, for example
            ``"checkout tickbox"``. This is the audit answer to "prove they agreed", so it must
            name a real surface of yours. Recording consent is not sending, so it works while your
            WhatsApp channel is still off. Consent is per business and reaches no other merchant.
        :param sms_if_whatsapp_fails: Text this recipient if the WhatsApp nudge proves
            *undeliverable*: Meta refuses it (usually because the number is not on WhatsApp), the
            delivery status comes back failed, or no status arrives within five minutes. It does
            **not** fire for a message that was delivered and not read: someone who read it on
            another device would get the same update twice. The SMS costs its own credit and
            appears as its own send. It also texts at once when WhatsApp cannot carry the message
            at all: the number is already known not to be on WhatsApp (``not_reachable``), or
            there is no template for it (``template_not_sendable`` because the push lacks
            ``reference`` or ``order_status``, or the reference or your business name cannot be
            shown); not while a template is still in review at Meta. Otherwise ignored unless a
            WhatsApp message was actually queued. At most one text either way.
        :param idempotency_key: See below. Generated when omitted.

        **Idempotency.** Every write carries an ``Idempotency-Key``. This SDK generates one per
        call and reuses it across its own retries, so a connection that dies with the answer in
        flight cannot become a duplicate email and a duplicate charged SMS. **Pass your own
        whenever you have a natural key** (an order id, a job id): then a retry from anywhere,
        your queue, a cron, a human clicking twice, collapses onto the same key rather than only
        the retries this SDK makes internally.

        :returns: A :class:`~beaconbox.models.MessagePushResult`. Check ``result.sms`` and
            ``result.whatsapp`` for what the paid channels did.

        A request body over 2 MiB is refused with :class:`~beaconbox.errors.InvalidRequestError`
        (HTTP 413, ``request.too_large``), and text containing a NUL character or a lone surrogate
        with ``request.unstorable_input`` (422). Neither is retried: the same body is refused
        again.

        .. code-block:: python

            result = client.messages.push(
                recipient_email="buyer@example.com",
                subject="Your order has shipped",
                body="Tracking XY123456789EE. Estimated delivery Thursday.",
                kind="updateable",
                reference="#A-10294",
                order_status=OrderStatus.SHIPPED,
                channels=["sms"],
                recipient_phone="+37255550134",
                idempotency_key="order-4711-shipped",
            )

            if result.sms and result.sms.skipped_reason == SkipReason.INSUFFICIENT_CREDIT:
                # Top up, then: client.messages.resend_sms(result.id)
                ...

        Naming a channel cannot switch it on. On an account whose SMS mode is ``off``, or where
        BeaconBox has not enabled SMS yet, the push above still succeeds and reports why no text
        went out:

        .. code-block:: python

            if result.sms and result.sms.skipped_reason in (
                SkipReason.SMS_DISABLED,
                SkipReason.SMS_NOT_ENABLED,
            ):
                ...  # the email went; turn SMS on in the dashboard to text as well
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
        return self._transport.invoke(_ops.push(message.to_payload()), idempotency_key)

    def push_batch(
        self, messages: Sequence[MessagePush], idempotency_key: str | None = None
    ) -> BatchResult:
        """Up to 100 pushes in one call.

        A list of complete, individual pushes, not one message fanned out to many recipients:
        fifty orders means fifty tracking numbers.

        **Answers 200 whenever the batch is processed. Read**
        :attr:`~beaconbox.models.BatchResult.failed` **, not the status code.** One rejected item
        does not fail the batch, because a batch that aborted at item 7 would leave items 8 onwards
        unsent with nothing to say which.

        **The one whole-batch refusal is a 409**, raised as :class:`~beaconbox.errors.ConflictError`
        with ``error_code`` ``idempotency.request_in_progress``: another request with the same key
        is still running this batch, or took it over while this one ran. It is never an item's
        result, and the SDK does not retry it. Wait a few seconds and retry the whole batch with the
        same key: items already sent are replayed, and every item's real outcome comes back.

        **Safe to retry with the same key.** Items that already went out are replayed rather than
        sent a second time, including after a crash partway through the batch. Each item's own key
        is derived from the batch key by appending ``#<index>``, so a batch ``idempotency_key`` is
        limited to 252 characters rather than 255; a longer one is refused with
        :class:`~beaconbox.errors.InvalidRequestError` before anything runs.

        Each item's ``channels`` follows the same rule as :meth:`push`: it can only narrow what
        your account settings allow, never switch a channel on.

        The whole request body may be at most 8 MiB. A larger one is refused as a whole, before any
        item runs, with :class:`~beaconbox.errors.InvalidRequestError` (HTTP 413,
        ``request.too_large``): split it into smaller batches.

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
        _check_page_size(page_size)
        walk = _PageWalk()
        cursor: str | None = None
        while True:
            page = self.list(recipient_email, page_size, cursor)
            yield from page.items
            cursor = walk.advance(page.next_cursor)
            if cursor is None:
                return

    def resend_sms(self, public_id: str, idempotency_key: str | None = None) -> SmsOutcome:
        """Send the SMS for a message whose text never went out.

        The recovery path for a ``skipped_reason``, most often an empty balance that has since
        been topped up.

        Refused with :class:`~beaconbox.errors.ConflictError` (``sms.already_sent``) while one is
        still queued for sending, or after one was accepted or delivered. The only thing a second
        send would add is a second charge and a second interruption. The refusal is decided before
        anything is written, so the idempotency key stays unspent.

        A policy refusal is not an error: it comes back as the outcome's ``skipped_reason``, which
        is ``None`` when the text was queued. Reaching this method counts as naming the channel,
        so an ``on_request`` account sends; an ``off`` one, or one BeaconBox has not enabled for
        SMS, still does not.
        """
        return self._transport.invoke(_ops.resend_sms(public_id), idempotency_key)

    def resend_whatsapp(
        self, public_id: str, idempotency_key: str | None = None
    ) -> WhatsAppOutcome:
        """The same, on WhatsApp.

        A separate call rather than a channel parameter, because a request naming both channels
        would have to mean charging twice and interrupting twice for one update.

        Refused with :class:`~beaconbox.errors.ConflictError` as ``whatsapp.already_sent`` while a
        WhatsApp send is still queued or after one was delivered or read, and as
        ``whatsapp.escalated_to_sms`` when ``sms_if_whatsapp_fails`` has already texted them.
        ``skipped_reason`` on the outcome is ``None`` when the message was queued.
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

    Two erasures, of different reach. :meth:`erase` is the whole Article 17 request for your
    business; :meth:`erase_whatsapp` removes only their WhatsApp history.

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

        A number that cannot be a recipient's phone is refused the same way (``sms.phone_invalid``):
        premium-rate, toll-free, shared-cost, voicemail and service numbers, and the satellite and
        international codes that belong to no country (+800, +808, +870, +878, +881, +882, +883,
        +888, +979). A number in those ranges stored before they were refused stays stored, and a
        push skips SMS to it as ``country_not_allowed``.
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

        ``email`` must be an email address. A blank value or one that is not an address raises
        :class:`ValueError` before anything is sent (the API would answer 422).
        """
        return self._transport.invoke(_ops.erase_whatsapp(email), idempotency_key)

    def erase(self, email: str, idempotency_key: str | None = None) -> RecipientErasureReport:
        """Erase everything held about this recipient for your business. **Not undoable.**

        The API half of an Article 17 request made to you. **Deleted:** the messages you sent them
        (subjects and bodies), their delivery and open events, their SMS and WhatsApp send records
        with the number each went to, the links that opened those messages, their WhatsApp
        replies, and their recipient record (phone number, SMS and WhatsApp consent, unsubscribe
        state). The copies inside webhook deliveries still queued for your endpoint and inside
        stored API responses are scrubbed, and sends still waiting to go out are cancelled. Your
        audit log keeps its entries with the address replaced by a keyed hash.

        **Retained, on purpose:** email suppressions for the address (hard bounces, complaints,
        and an unsubscribe, which is kept as a suppression) and SMS or WhatsApp opt-outs for their
        number. They are what stop this person being contacted again by a later push, and an
        opt-out erased on request stops being one. **Erasing does not undo an unsubscribe:** a
        later push to the address finds them still unsubscribed. The report counts
        both (:attr:`~beaconbox.models.RecipientErasureReport.suppressions_kept`,
        :attr:`~beaconbox.models.RecipientErasureReport.stops_kept`). Other businesses' records
        about the same person are untouched.

        **Copies that already left BeaconBox are yours to erase:** webhook deliveries your endpoint
        received, and the WhatsApp replies your forward email sent to your mailboxes, counted in
        :attr:`~beaconbox.models.RecipientErasureReport.replies_a_forward_email_may_have_carried`.

        It is a ``POST`` and carries an ``Idempotency-Key`` like every other. A repeat erasure
        truthfully reports zeros, so retrying a timed-out call with the **same** key is how you get
        the original counts back. An address you never messaged also answers with zeros, so this
        cannot be used to test whether an address is on your list.

        ``email`` must be an email address. A blank value or one that is not an address raises
        :class:`ValueError` before anything is sent (the API would answer 422), so a slip in the
        value you pass fails at your line rather than as a server validation error.

        .. code-block:: python

            report = client.recipients.erase("buyer@example.com")
            print(report.messages_deleted, report.suppressions_kept, report.stops_kept)
        """
        return self._transport.invoke(_ops.erase_recipient(email), idempotency_key)


class Keys(_Resource):
    """API keys.

    A minted key is shown **once**. BeaconBox stores a hash, so the create response is the only
    place the full value will ever exist.
    """

    def list(self) -> tuple[ApiKey, ...]:
        """Your keys, masked. Useful for auditing what exists and revoking what should not."""
        return self._transport.invoke(_ops.list_keys())

    def create(
        self, name: str | None = None, idempotency_key: str | None = None, *, detach: bool = False
    ) -> NewApiKey:
        """Mint a key. Name it after the service that will hold it, not the person creating it.

        The returned :attr:`~beaconbox.models.NewApiKey.key` is unrecoverable. Write it to a
        secret store before the process exits. Its ``repr`` is redacted so an incidental log line
        cannot leak it, so read the attribute explicitly.

        **A key minted with a key is revoked with it**, and with any key above it, unless it was
        minted with ``detach=True``. To rotate, pass ``detach=True``: the new key keeps working
        after you revoke the key this client uses. Mint it, move your systems to it, then
        :meth:`revoke` the old one. A leaked key can mint a detached key too, so after revoking one,
        :meth:`list` the keys and revoke any whose ``minted_by`` is the leaked key's id.

        Refused with :class:`~beaconbox.errors.PermissionDeniedError` (``plan.read_only``) while
        your plan has lapsed: pay, and minting works again.
        """
        return self._transport.invoke(_ops.create_key(name, detach=detach), idempotency_key)

    def revoke(self, key_id: str) -> None:
        """Revoke a key immediately. In-flight requests using it start failing with 401.

        Revoking the key the calling client is authenticated with is allowed, and is the correct
        response to a leak even though the next call from this client will fail. Every key minted
        with it (and not detached) is revoked too, and the keys those minted in turn. A detached key
        it minted is not: find those by ``minted_by`` in :meth:`list`.
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
            _ops.create_webhook_endpoint(url, [_value(t) for t in event_types], description),
            idempotency_key,
        )

    def test(self, public_id: str) -> WebhookTestResult:
        """Send one sample ``ping`` event to this endpoint and report what came back.

        **The only way to find out your handler works before a real event depends on it.** A
        wrong webhook fails silently by nature: your handler is simply never called, and nothing
        on either side says so.

        The event type is ``ping``, deliberately not a real one — a test carrying
        ``message.delivered`` would be indistinguishable from the genuine article at your end. It
        is signed exactly like a real delivery, so it also proves your signature verification.

        Never raises for a failure at *your* end: read
        :attr:`~beaconbox.models.WebhookTestResult.delivered`. Pressing it in a tight loop is
        refused with :class:`~beaconbox.errors.RateLimitError` (``webhook.test_rate_limited``),
        because each press is a request to your server. Testing does not affect the
        endpoint's health counters, so you can press it as often as you like while fixing a
        handler.
        """
        return self._transport.invoke(_ops.test_webhook_endpoint(public_id))

    def delete(self, public_id: str) -> None:
        """Delete an endpoint. Deliveries already queued for it stop.

        Also the way to re-enable one that was auto-disabled: delete it and register it again,
        which rotates the secret.
        """
        self._transport.invoke(_ops.delete_webhook_endpoint(public_id))


def _value(item: WebhookEventType | str) -> str:
    return item.value if isinstance(item, WebhookEventType) else item
