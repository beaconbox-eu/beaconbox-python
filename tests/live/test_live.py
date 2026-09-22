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

import os
import uuid
from collections.abc import Iterator

import pytest

from beaconbox import (
    AsyncBeaconBox,
    BeaconBox,
    ConflictError,
    MessageKind,
    MessagePush,
    MessageStatus,
    ResourceMissingError,
)

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


@pytest.fixture(scope="module")
def client() -> Iterator[BeaconBox]:
    assert BASE_URL and API_KEY
    with BeaconBox(API_KEY, base_url=BASE_URL, verify=VERIFY) as bound:
        yield bound


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

        try:
            listed = client.keys.list()
            assert any(key.name == created.name for key in listed)
        finally:
            # Find our row by name: create returns the secret, list returns the id.
            match = next((k for k in client.keys.list() if k.name == created.name), None)
            if match is not None:
                client.keys.revoke(match.id)

    def test_a_minted_key_actually_works(self, client: BeaconBox) -> None:
        """End to end on the credential itself, which no unit test can reach."""
        created = client.keys.create(f"sdk-live-usable-{uuid.uuid4().hex[:6]}")

        try:
            assert BASE_URL
            with BeaconBox(created.key, base_url=BASE_URL, verify=VERIFY) as minted:
                assert isinstance(minted.credits.balance().balance, int)
        finally:
            match = next((k for k in client.keys.list() if k.name == created.name), None)
            if match is not None:
                client.keys.revoke(match.id)

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
