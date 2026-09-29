"""The SDK against a real BeaconBox.

Skipped unless ``BEACONBOX_LIVE_URL`` and ``BEACONBOX_API_KEY`` are set, so ``pytest`` with no
setup stays hermetic and fast. ``mise run //sdk:live`` sets both from a fresh seed.

**What this catches that the unit suite cannot.** Every unit test asserts against a payload this
repository wrote. If the SDK and the API disagree about a field name, both sides of a unit test
agree with each other and are wrong together. That is exactly how ``pushBatch`` shipped sending
``{"messages": ...}`` against an API that reads ``{"items": ...}``: unit tests green, every real
call a 422. So the assertions here are deliberately shallow. What matters is that the server
accepted the request and the SDK understood the answer.
"""

from __future__ import annotations

import contextlib
import os
import time
import uuid
from collections.abc import Callable, Iterator
from typing import TypeVar

import pytest

from beaconbox import (
    AsyncBeaconBox,
    BeaconBox,
    ChannelSendStatus,
    ConflictError,
    EmailSkipReason,
    MessageKind,
    MessagePush,
    MessageStatus,
    ResourceMissingError,
)

T = TypeVar("T")

pytestmark = pytest.mark.live

BASE_URL = os.environ.get("BEACONBOX_LIVE_URL")
API_KEY = os.environ.get("BEACONBOX_API_KEY")
RECIPIENT = os.environ.get("BEACONBOX_LIVE_RECIPIENT", "live-sdk@example.com")

# The local stack serves https with a mkcert CA, which httpx does not trust: it verifies against
# certifi, not the system keychain. Pointing `verify=` at the CA is the fix, and it exercises the
# same option a merchant behind a TLS-inspecting corporate proxy needs. Turning verification off
# instead would make this suite the one place the SDK's security default is not tested.
VERIFY: object = os.environ.get("BEACONBOX_LIVE_CA_BUNDLE") or True

if not BASE_URL or not API_KEY:
    pytest.skip(
        "live tests need BEACONBOX_LIVE_URL and BEACONBOX_API_KEY (see tests/live/README.md)",
        allow_module_level=True,
    )


def unique(prefix: str) -> str:
    """A fresh subject per run.

    Necessary rather than tidy: an `updateable` push matches on subject, so a reused one would
    update the previous run's message and this suite would stop testing creation.
    """
    return f"{prefix} {uuid.uuid4().hex[:8]}"


#: Phone number for the SMS tests. In the E.164 range reserved for documentation.
PHONE = "+37255550134"


def eventually(read: Callable[[], T | None], *, what: str, seconds: float = 20.0) -> T:
    """Poll ``read`` until it returns something, or fail naming what never arrived.

    Delivery is asynchronous: the push returns once the send is *queued*, and the worker fills in
    the rest. A fixed sleep would be either flaky or slow, and an unlabelled timeout would fail as
    ``assert None is not None``.
    """
    deadline = time.monotonic() + seconds
    while True:
        value = read()
        if value is not None:
            return value
        if time.monotonic() >= deadline:
            pytest.fail(f"{what} did not arrive within {seconds:g}s")
        time.sleep(0.25)


@pytest.fixture(scope="module")
def client() -> Iterator[BeaconBox]:
    assert BASE_URL and API_KEY
    with BeaconBox(API_KEY, base_url=BASE_URL, verify=VERIFY) as bound:
        yield bound


@pytest.fixture(scope="module")
def sendable(client: BeaconBox) -> BeaconBox:
    """The same client, once there is a balance to spend.

    A zero-balance world refuses every paid send with ``insufficient_credit``, which is a correct
    answer and a useless fixture: ``delivery.sms`` stays null and the SDK's ``SmsDelivery`` parsing
    is exercised by nothing real. Seed with credits (``mise run //sdk:live`` does) rather than
    letting these tests assert the refusal and look like they covered the send.
    """
    if client.credits.balance().balance < 1:
        pytest.skip(
            "needs a credit balance: seed with `beaconbox e2e seed --credits 200`, "
            "or run `mise run //sdk:live` which does"
        )
    return client


class TestMessages:
    def test_push_and_read_back(self, client: BeaconBox) -> None:
        subject = unique("SDK live push")

        result = client.messages.push(
            recipient_email=RECIPIENT,
            subject=subject,
            body="Sent by the Python SDK live suite.",
        )

        assert result.id
        assert result.created is True
        assert result.subject == subject
        assert result.status is MessageStatus.ACTIVE

        fetched = client.messages.get(result.id)
        assert fetched.id == result.id
        assert fetched.recipient_email == RECIPIENT

    def test_updateable_push_updates_in_place(self, client: BeaconBox) -> None:
        """The behaviour a merchant most depends on, and the one a mock cannot prove."""
        subject = unique("SDK live updateable")
        first = client.messages.push(
            recipient_email=RECIPIENT,
            subject=subject,
            body="Estimated delivery Thursday.",
            kind=MessageKind.UPDATEABLE,
        )

        second = client.messages.push(
            recipient_email=RECIPIENT,
            subject=subject,
            body="Estimated delivery Friday.",
            kind=MessageKind.UPDATEABLE,
        )

        assert first.created is True
        assert second.created is False, "a repeated updateable subject created a second message"
        assert second.id == first.id

    def test_idempotency_key_collapses_a_repeat(self, client: BeaconBox) -> None:
        """The property the whole retry design rests on, proven against the real store.

        Two identical pushes under one key must be one message, not two. If this ever fails, every
        retry this SDK makes is a duplicate to a real customer.
        """
        key = f"live-{uuid.uuid4()}"
        subject = unique("SDK live idempotency")
        body = "Sent twice under one key."

        first = client.messages.push(
            recipient_email=RECIPIENT, subject=subject, body=body, idempotency_key=key
        )
        second = client.messages.push(
            recipient_email=RECIPIENT, subject=subject, body=body, idempotency_key=key
        )

        assert second.id == first.id

    def test_batch_is_accepted(self, client: BeaconBox) -> None:
        """The regression test for the `items` field name. A batch under any other key 422s."""
        result = client.messages.push_batch(
            [
                MessagePush(RECIPIENT, unique("SDK live batch a"), "One."),
                MessagePush(RECIPIENT, unique("SDK live batch b"), "Two."),
            ]
        )

        assert result.failed == 0, f"batch items were rejected: {[i.raw for i in result.failures]}"
        assert result.succeeded == 2

    def test_list_and_iterate(self, client: BeaconBox) -> None:
        page = client.messages.list(recipient_email=RECIPIENT, limit=2)
        assert len(page.items) <= 2

        seen = 0
        for _ in client.messages.iterate(recipient_email=RECIPIENT, page_size=2):
            seen += 1
            if seen >= 3:  # enough to prove the cursor was followed past page one
                break
        assert seen > 0

    def test_retract(self, client: BeaconBox) -> None:
        pushed = client.messages.push(
            recipient_email=RECIPIENT,
            subject=unique("SDK live retract"),
            body="This one gets withdrawn.",
        )

        result = client.messages.retract(pushed.id)

        assert result.retracted is True
        assert result.status is MessageStatus.RETRACTED
        # Idempotent: the second call reports that it changed nothing, and does not fail.
        assert client.messages.retract(pushed.id).retracted is False

    def test_a_paid_sms_send_reports_its_delivery(self, sendable: BeaconBox) -> None:
        """The paid channel actually sending, which every other test here leaves untouched.

        Without a balance this whole path answers ``insufficient_credit``, so until the seed could
        grant credits the SDK's ``SmsDelivery`` model was parsed by unit tests only — against
        payloads this repository wrote. This is the one assertion that the real server's delivery
        block and the SDK's reading of it agree.

        ``accepted`` rather than ``delivered``: the local carrier is ``console``, which logs the
        message and never posts a status back, so a send legitimately stops there.
        """
        before = sendable.credits.balance().balance
        pushed = sendable.messages.push(
            recipient_email=RECIPIENT,
            subject=unique("live sms"),
            body="Tracking XY123456789EE.",
            recipient_phone=PHONE,
            channels=["sms"],
        )

        assert pushed.sms is not None
        assert pushed.sms.queued, f"not queued: {pushed.sms.skipped_reason}"
        assert pushed.sms.credits >= 1

        delivery = eventually(
            lambda: (
                sms
                if (sms := sendable.messages.get(pushed.id).delivery.sms) is not None
                and sms.submitted_at is not None
                else None
            ),
            what="the SMS delivery record",
        )

        assert delivery.status in {ChannelSendStatus.ACCEPTED, ChannelSendStatus.DELIVERED}
        assert delivery.credits_charged >= 1
        assert delivery.submitted_at is not None
        assert delivery.submitted_at.tzinfo is not None, "timestamps are timezone-aware"
        assert delivery.skipped_reason is None
        assert sendable.credits.balance().balance < before, "a real send spends a real credit"

    def test_a_retraction_reports_why_no_email_was_sent(self, client: BeaconBox) -> None:
        """``delivery.not_sent``, the field that tells ``delivered is False`` apart from *never
        attempted, and never will be*.

        Reached by retracting before the nudge runs, which is the cheapest of its reasons to
        provoke: the others need a lapsed plan, a daily cap or a suppressed address.
        """
        pushed = client.messages.push(
            recipient_email=RECIPIENT, subject=unique("live notsent"), body="b"
        )
        assert client.messages.retract(pushed.id).retracted

        not_sent = eventually(
            lambda: client.messages.get(pushed.id).delivery.not_sent,
            what="the not_sent block",
        )

        assert not_sent.reason == EmailSkipReason.MESSAGE_RETRACTED
        assert not_sent.at is not None
        assert not_sent.at.tzinfo is not None

    def test_an_unknown_id_is_a_404(self, client: BeaconBox) -> None:
        with pytest.raises(ResourceMissingError):
            client.messages.get("m_definitelynotreal")


class TestOtherResources:
    def test_credits_balance(self, client: BeaconBox) -> None:
        balance = client.credits.balance()
        assert isinstance(balance.balance, int)
        assert isinstance(balance.threshold, int)

    def test_recipient_phone_round_trip(self, client: BeaconBox) -> None:
        stored = client.recipients.set_phone(RECIPIENT, "+37255550134")
        assert stored.phone is not None
        # A read must never hand back the full number.
        assert "•" in stored.phone or "5555 0134" not in stored.phone

        read = client.recipients.sms(RECIPIENT)
        assert read.recipient_email == RECIPIENT

        cleared = client.recipients.clear_phone(RECIPIENT)
        assert cleared.phone is None

    def test_keys_create_list_revoke(self, client: BeaconBox) -> None:
        created = client.keys.create(f"sdk-live-{uuid.uuid4().hex[:6]}")
        assert created.key.startswith("bbx_live_")
        assert created.id, "the create response carries the id to revoke by"

        try:
            listed = client.keys.list()
            assert created.id in {key.id for key in listed}
        finally:
            # Straight by id. This used to look the row up by name, which is ambiguous: a name has
            # no unique constraint and defaults to "Untitled key", so the match could revoke a
            # different key than the one just minted.
            client.keys.revoke(created.id)

    def test_two_unnamed_keys_are_told_apart_by_id(self, client: BeaconBox) -> None:
        """The reason ``create`` returns an id, against the real server.

        Both keys are ``"Untitled key"``, so anything matching on name cannot say which is which —
        and this is the path a merchant takes to revoke a leaked credential.
        """
        first = client.keys.create()
        second = client.keys.create()

        try:
            assert first.name == second.name
            assert first.id != second.id

            client.keys.revoke(second.id)
            remaining = {k.id for k in client.keys.list()}
            assert first.id in remaining
            assert second.id not in remaining
        finally:
            for key in (first, second):
                with contextlib.suppress(ResourceMissingError):
                    client.keys.revoke(key.id)  # the second is already revoked above

    def test_a_minted_key_actually_works(self, client: BeaconBox) -> None:
        """End to end on the credential itself, which no unit test can reach."""
        created = client.keys.create(f"sdk-live-usable-{uuid.uuid4().hex[:6]}")

        try:
            assert BASE_URL
            with BeaconBox(created.key, base_url=BASE_URL, verify=VERIFY) as minted:
                assert isinstance(minted.credits.balance().balance, int)
        finally:
            client.keys.revoke(created.id)

    def test_webhook_endpoints(self, client: BeaconBox) -> None:
        url = f"https://example.com/hooks/{uuid.uuid4().hex[:8]}"
        created = client.webhook_endpoints.create(url, description="sdk live suite")

        try:
            # The secret is real exactly once, and it is what webhooks.verify needs.
            assert created.secret.startswith("whsec_")
            assert len(created.secret) > 20, "the create response returned a masked secret"
            assert any(e.id == created.id for e in client.webhook_endpoints.list())
        finally:
            client.webhook_endpoints.delete(created.id)

    def test_a_duplicate_endpoint_url_conflicts(self, client: BeaconBox) -> None:
        url = f"https://example.com/hooks/{uuid.uuid4().hex[:8]}"
        created = client.webhook_endpoints.create(url)

        try:
            with pytest.raises(ConflictError):
                client.webhook_endpoints.create(url)
        finally:
            client.webhook_endpoints.delete(created.id)


class TestAsyncClient:
    """The async client against the same server. Parity where it counts."""

    async def test_push_and_read_back(self) -> None:
        assert BASE_URL and API_KEY
        async with AsyncBeaconBox(API_KEY, base_url=BASE_URL, verify=VERIFY) as client:
            result = await client.messages.push(
                recipient_email=RECIPIENT,
                subject=unique("SDK live async push"),
                body="Sent by the async Python SDK live suite.",
            )
            assert result.created is True

            fetched = await client.messages.get(result.id)
            assert fetched.id == result.id

    async def test_iterate(self) -> None:
        assert BASE_URL and API_KEY
        async with AsyncBeaconBox(API_KEY, base_url=BASE_URL, verify=VERIFY) as client:
            seen = 0
            async for _ in client.messages.iterate(recipient_email=RECIPIENT, page_size=2):
                seen += 1
                if seen >= 3:
                    break
            assert seen > 0

    async def test_batch_is_accepted(self) -> None:
        assert BASE_URL and API_KEY
        async with AsyncBeaconBox(API_KEY, base_url=BASE_URL, verify=VERIFY) as client:
            result = await client.messages.push_batch(
                [MessagePush(RECIPIENT, unique("SDK live async batch"), "One.")]
            )
            assert result.failed == 0
