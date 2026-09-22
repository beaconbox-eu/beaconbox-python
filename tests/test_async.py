"""The async client, and the two claims it makes that are worth proving.

1. **Parity.** Every method on the sync client exists on the async one, with the same signature.
   Asserted by reflection rather than by hand, so a method added to one and forgotten on the
   other fails here rather than in somebody's editor months later.
2. **It does not block the event loop.** Retry backoff is `asyncio.sleep`, and requests are
   awaited rather than run in a thread. An SDK that wrapped the sync path in an executor would
   look identical from the outside and would be the reason a merchant's API server stalls when
   BeaconBox is slow.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

import httpx
import pytest

from beaconbox import (
    APIConnectionError,
    AsyncBeaconBox,
    BeaconBox,
    ConflictError,
    MessagePush,
    RetryPolicy,
    SkipReason,
    async_resources,
    resources,
)
from tests.conftest import (
    API_KEY,
    BASE_URL,
    MESSAGE_VIEW,
    PUSH_RESULT,
    Reply,
    make_async_client,
    no_sleep,
)

PUSH: dict[str, Any] = {
    "recipient_email": "buyer@example.com",
    "subject": "Your order has shipped",
    "body": "Tracking XY123456789EE.",
}

PAIRS = [
    (resources.Messages, async_resources.AsyncMessages),
    (resources.Credits, async_resources.AsyncCredits),
    (resources.Recipients, async_resources.AsyncRecipients),
    (resources.Keys, async_resources.AsyncKeys),
    (resources.WebhookEndpoints, async_resources.AsyncWebhookEndpoints),
]


def public_methods(cls: type) -> dict[str, inspect.Signature]:
    return {
        name: inspect.signature(member)
        for name, member in inspect.getmembers(cls, inspect.isfunction)
        if not name.startswith("_")
    }


class TestParity:
    @pytest.mark.parametrize(("sync_cls", "async_cls"), PAIRS, ids=lambda c: c.__name__)
    def test_the_same_methods_exist(self, sync_cls: type, async_cls: type) -> None:
        assert set(public_methods(sync_cls)) == set(public_methods(async_cls))

    @pytest.mark.parametrize(("sync_cls", "async_cls"), PAIRS, ids=lambda c: c.__name__)
    def test_the_signatures_match(self, sync_cls: type, async_cls: type) -> None:
        """So that porting a call from sync to async is adding `await`, and nothing else."""
        sync_sigs = public_methods(sync_cls)
        async_sigs = public_methods(async_cls)

        for name, signature in sync_sigs.items():
            assert list(signature.parameters) == list(async_sigs[name].parameters), name

    def test_the_clients_expose_the_same_resources(self) -> None:
        sync_attrs = {n for n in vars(BeaconBox("k", base_url=BASE_URL)) if not n.startswith("_")}
        async_client = AsyncBeaconBox("k", base_url=BASE_URL)
        async_attrs = {n for n in vars(async_client) if not n.startswith("_")}

        assert sync_attrs == async_attrs


class TestOperations:
    async def test_push(self) -> None:
        client, recorder = make_async_client(Reply(201, PUSH_RESULT))

        result = await client.messages.push(**PUSH)

        assert recorder.only.method == "POST"
        assert result.id == "m_8sKq2Vd1"

    async def test_a_skipped_channel_does_not_raise_here_either(self) -> None:
        client, _ = make_async_client(Reply(201, PUSH_RESULT))

        result = await client.messages.push(**PUSH)

        assert result.sms is not None
        assert result.sms.skipped_reason == SkipReason.INSUFFICIENT_CREDIT

    async def test_push_batch_sends_items(self) -> None:
        client, recorder = make_async_client(Reply(200, {"items": [], "succeeded": 0, "failed": 0}))

        await client.messages.push_batch([MessagePush("a@b.c", "s", "b")])

        assert list(recorder.body()) == ["items"]

    async def test_iterate_follows_cursors(self) -> None:
        client, recorder = make_async_client(
            [
                Reply(200, {"items": [MESSAGE_VIEW], "next_cursor": "cur_2"}),
                Reply(200, {"items": [MESSAGE_VIEW], "next_cursor": None}),
            ]
        )

        collected = [message async for message in client.messages.iterate()]

        assert len(collected) == 2
        assert len(recorder.requests) == 2

    async def test_reads_and_deletes(self) -> None:
        client, recorder = make_async_client(Reply(204))

        await client.keys.revoke("k_1")

        assert recorder.only.method == "DELETE"

    async def test_errors_map_the_same_way(self) -> None:
        client, _ = make_async_client(Reply(409, {"error_code": "message.push_conflict"}))

        with pytest.raises(ConflictError):
            await client.messages.push(**PUSH)


class TestDoesNotBlockTheEventLoop:
    async def test_backoff_yields_to_the_loop(self) -> None:
        """The real assertion: while the SDK is backing off, other coroutines run.

        `time.sleep` in the retry loop would make this fail, because the counter task would never
        get a turn.
        """
        ticks = 0

        async def counter() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0)
                ticks += 1

        client, _ = make_async_client(
            [Reply(503), Reply(201, PUSH_RESULT)],
            retry_policy=RetryPolicy(max_retries=1, base_delay=0.05, max_delay=0.05),
        )
        task = asyncio.create_task(counter())

        await client.messages.push(**PUSH)
        task.cancel()

        assert ticks > 0, "the event loop was blocked during retry backoff"

    async def test_concurrent_pushes_overlap(self) -> None:
        """Ten pushes against a transport that awaits should not serialize into ten round
        trips' worth of latency."""
        in_flight = 0
        peak = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1
            return httpx.Response(201, json=PUSH_RESULT)

        client = AsyncBeaconBox(
            API_KEY,
            base_url=BASE_URL,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

        await asyncio.gather(*(client.messages.push(**PUSH) for _ in range(10)))
        await client.aclose()

        assert peak > 1, "requests were serialized rather than overlapped"

    async def test_each_concurrent_push_gets_its_own_key(self) -> None:
        """Ten concurrent pushes are ten messages. Sharing a key would collapse them into one."""
        client, recorder = make_async_client(Reply(201, PUSH_RESULT))

        await asyncio.gather(*(client.messages.push(**PUSH) for _ in range(10)))

        keys = recorder.idempotency_keys()
        assert len(set(keys)) == 10


class TestRetries:
    async def test_reuses_one_key_across_attempts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        no_sleep(monkeypatch)
        client, recorder = make_async_client(
            [Reply(503), Reply(503), Reply(201, PUSH_RESULT)],
            retry_policy=RetryPolicy(max_retries=2),
        )

        await client.messages.push(**PUSH)

        assert len(set(recorder.idempotency_keys())) == 1

    async def test_a_lost_connection_is_retried_then_reported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        no_sleep(monkeypatch)
        client, recorder = make_async_client(
            Reply(raise_error=httpx.ConnectError("refused")),
            retry_policy=RetryPolicy(max_retries=2),
        )

        with pytest.raises(APIConnectionError):
            await client.messages.push(**PUSH)

        assert len(recorder.requests) == 3


class TestPaginationCannotHang:
    async def test_a_repeated_cursor_raises_instead_of_looping(self) -> None:
        from beaconbox import BeaconBoxError

        client, recorder = make_async_client(
            Reply(200, {"items": [MESSAGE_VIEW], "next_cursor": "stuck"})
        )

        with pytest.raises(BeaconBoxError, match="did not advance"):
            [m async for m in client.messages.iterate()]

        assert len(recorder.requests) == 2
