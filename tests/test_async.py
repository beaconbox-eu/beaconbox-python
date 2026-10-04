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
from collections.abc import Callable
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
    make_client,
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

    async def test_a_page_size_below_one_is_refused_here_too(self) -> None:
        """Same guard as the sync client, because it is the same object."""
        client, recorder = make_async_client(Reply(200, {"items": [], "next_cursor": None}))

        with pytest.raises(ValueError, match="page_size must be at least 1"):
            [m async for m in client.messages.iterate(page_size=0)]

        assert recorder.requests == []


#: The key the callers below pass explicitly, so a forwarded one can be told from a minted one.
EXPLICIT_KEY = "k"

# Every operation, as (name, how to call it). One list, driven against both clients, because the
# async module is a hand-maintained mirror of the sync one and that is exactly the shape in which a
# copy-paste error lives: `async def retract` calling `_ops.resend_sms` has the right signature,
# the right return type and the wrong URL.
OPERATIONS: list[tuple[str, Callable[[Any], Any]]] = [
    ("messages.push", lambda c: c.messages.push(**PUSH, idempotency_key=EXPLICIT_KEY)),
    (
        "messages.push_batch",
        lambda c: c.messages.push_batch([MessagePush(**PUSH)], idempotency_key=EXPLICIT_KEY),
    ),
    ("messages.get", lambda c: c.messages.get("m_1")),
    ("messages.list", lambda c: c.messages.list(recipient_email="a@example.com", limit=7)),
    ("messages.resend_sms", lambda c: c.messages.resend_sms("m_1", idempotency_key=EXPLICIT_KEY)),
    (
        "messages.resend_whatsapp",
        lambda c: c.messages.resend_whatsapp("m_1", idempotency_key=EXPLICIT_KEY),
    ),
    ("messages.retract", lambda c: c.messages.retract("m_1", idempotency_key=EXPLICIT_KEY)),
    ("credits.balance", lambda c: c.credits.balance()),
    ("recipients.sms", lambda c: c.recipients.sms("buyer@example.com")),
    ("recipients.set_phone", lambda c: c.recipients.set_phone("buyer@example.com", "+37255550134")),
    ("recipients.clear_phone", lambda c: c.recipients.clear_phone("buyer@example.com")),
    (
        "recipients.erase_whatsapp",
        lambda c: c.recipients.erase_whatsapp("buyer@example.com", idempotency_key=EXPLICIT_KEY),
    ),
    ("keys.list", lambda c: c.keys.list()),
    ("keys.create", lambda c: c.keys.create("orders service", idempotency_key=EXPLICIT_KEY)),
    ("keys.revoke", lambda c: c.keys.revoke("key_1")),
    ("webhook_endpoints.list", lambda c: c.webhook_endpoints.list()),
    (
        "webhook_endpoints.create",
        lambda c: c.webhook_endpoints.create(
            "https://example.com/hooks", ["message.bounced"], "orders", idempotency_key=EXPLICIT_KEY
        ),
    ),
    ("webhook_endpoints.test", lambda c: c.webhook_endpoints.test("we_1")),
    ("webhook_endpoints.delete", lambda c: c.webhook_endpoints.delete("we_1")),
]


class TestEveryOperationIssuesTheSameRequestOnBothClients:
    """The claim ``README`` makes as "same client, same signatures, awaited", checked on the wire.

    :class:`TestParity` compares signatures, which cannot see inside a method body. These compare
    the bytes: the method, the URL, the query string, the body and the ``Idempotency-Key``. An
    async method wired to the wrong operation passes every signature check and fails here.

    It is also what executes the async resource methods at all. Before this existed, fifteen of
    the twenty went untouched by the suite while the sync twins were covered, so the half of the
    advertised surface a merchant reaches with ``await`` was the half nobody ran.
    """

    def test_the_operation_list_covers_every_public_method(self) -> None:
        """Otherwise this file grows a method and silently stops testing it."""
        listed = {name for name, _ in OPERATIONS}
        actual = {
            f"{resource}.{method}"
            for resource, cls in [
                ("messages", resources.Messages),
                ("credits", resources.Credits),
                ("recipients", resources.Recipients),
                ("keys", resources.Keys),
                ("webhook_endpoints", resources.WebhookEndpoints),
            ]
            for method in public_methods(cls)
        }
        # `iterate` drives `list` in a loop and is covered by its own tests above.
        assert actual - listed == {"messages.iterate"}, actual - listed

    @pytest.mark.parametrize(("name", "call"), OPERATIONS, ids=[n for n, _ in OPERATIONS])
    async def test_the_two_clients_send_identical_requests(
        self, name: str, call: Callable[[Any], Any]
    ) -> None:
        reply = Reply(200, {"items": [], "id": "m_1", "next_cursor": None})

        sync_client, sync_recorder = make_client(reply)
        call(sync_client)

        async_client, async_recorder = make_async_client(reply)
        await call(async_client)

        assert _shape(sync_recorder.only) == _shape(async_recorder.only), name

    @pytest.mark.parametrize(("name", "call"), OPERATIONS, ids=[n for n, _ in OPERATIONS])
    async def test_every_async_operation_reaches_the_network(
        self, name: str, call: Callable[[Any], Any]
    ) -> None:
        """A method that returns without issuing a request is one nobody would notice was broken."""
        client, recorder = make_async_client(
            Reply(200, {"items": [], "id": "m_1", "next_cursor": None})
        )

        await call(client)

        assert len(recorder.requests) == 1


def _shape(request: httpx.Request) -> tuple[str, str, str, str | None, str | None]:
    """What is worth comparing between the two clients, and nothing that cannot match.

    The ``User-Agent`` is excluded because it names the client, which is the one header the two are
    *supposed* to disagree about.

    An ``Idempotency-Key`` the SDK minted is normalised to ``"<minted>"``, because it is a fresh
    UUID per call: comparing the two clients' values would be comparing two random numbers. What
    still has to match is the part that carries meaning, which is whether the header is there at
    all and whether a key the caller supplied was forwarded rather than replaced.
    """
    key = request.headers.get("Idempotency-Key")
    return (
        request.method,
        request.url.path,
        str(request.url.query, "ascii"),
        request.content.decode() or None,
        key if key in (None, EXPLICIT_KEY) else "<minted>",
    )
