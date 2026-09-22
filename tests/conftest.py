"""Test scaffolding: a real client wired to a fake network.

Every test here drives ``BeaconBox`` through its public surface. The only thing replaced is the
socket, via ``httpx.MockTransport``, which means the tests exercise the actual request building,
the actual retry loop and the actual parsing rather than a mock of them. It also means the
``http_client=`` injection point is covered by every test that uses it.

No mocking library, deliberately. A test that patches ``beaconbox._transport.SyncTransport`` is a
test that keeps passing after that class stops working.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from beaconbox import AsyncBeaconBox, BeaconBox, RetryPolicy

API_KEY = "bbx_live_0123456789abcdef0123456789abcdef"
BASE_URL = "https://api.beaconbox.test"


@dataclass
class Reply:
    """One canned response."""

    status_code: int = 200
    json_body: Any = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    raise_error: Exception | None = None
    """When set, the transport raises instead of answering. Simulates a lost connection."""


@dataclass
class Recorder:
    """Captures what the SDK actually sent, and hands back queued replies.

    The last reply repeats once the queue is exhausted, so a test that only cares about one
    request does not have to count the retries.
    """

    replies: list[Reply]
    requests: list[httpx.Request] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        reply = self.replies[min(len(self.requests) - 1, len(self.replies) - 1)]
        if reply.raise_error is not None:
            raise reply.raise_error
        return httpx.Response(
            reply.status_code,
            content=json.dumps(reply.json_body).encode(),
            headers={"Content-Type": "application/json", **reply.headers},
        )

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]

    @property
    def only(self) -> httpx.Request:
        assert len(self.requests) == 1, f"expected exactly one request, got {len(self.requests)}"
        return self.requests[0]

    def body(self, index: int = -1) -> Any:
        return json.loads(self.requests[index].content)

    def idempotency_keys(self) -> list[str | None]:
        return [r.headers.get("Idempotency-Key") for r in self.requests]


def make_client(
    replies: Sequence[Reply] | Reply | None = None,
    *,
    retry_policy: RetryPolicy | None = None,
    **kwargs: Any,
) -> tuple[BeaconBox, Recorder]:
    """A sync client whose network is ``recorder``."""
    recorder = _recorder(replies)
    client = BeaconBox(
        API_KEY,
        base_url=BASE_URL,
        http_client=httpx.Client(transport=httpx.MockTransport(recorder)),
        # Zero by default so a test that is not about retrying does not silently make four
        # requests and pass anyway.
        retry_policy=retry_policy or RetryPolicy(max_retries=0),
        **kwargs,
    )
    return client, recorder


def make_async_client(
    replies: Sequence[Reply] | Reply | None = None,
    *,
    retry_policy: RetryPolicy | None = None,
    **kwargs: Any,
) -> tuple[AsyncBeaconBox, Recorder]:
    """The async twin of :func:`make_client`."""
    recorder = _recorder(replies)
    client = AsyncBeaconBox(
        API_KEY,
        base_url=BASE_URL,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(recorder)),
        retry_policy=retry_policy or RetryPolicy(max_retries=0),
        **kwargs,
    )
    return client, recorder


def _recorder(replies: Sequence[Reply] | Reply | None) -> Recorder:
    if replies is None:
        return Recorder([Reply()])
    if isinstance(replies, Reply):
        return Recorder([replies])
    return Recorder(list(replies))


def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Make backoff instant and record what it would have slept.

    Retry tests assert on the *decision* to wait and on the ordering, not on wall-clock time. A
    suite that really slept its own backoff would take seconds to prove something arithmetic.
    """
    slept: list[float] = []

    def record(seconds: float) -> None:
        slept.append(seconds)

    async def record_async(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr("beaconbox._transport.time.sleep", record)
    monkeypatch.setattr("beaconbox._transport.asyncio.sleep", record_async)
    return slept


# --- Response fixtures ------------------------------------------------------------------
#
# Shaped exactly like the API's own schemas. Where a field is optional in the schema it is
# omitted here rather than sent as null, because that is the harder case for a parser.

PUSH_RESULT: dict[str, Any] = {
    "id": "m_8sKq2Vd1",
    "subject": "Your order has shipped",
    "kind": "updateable",
    "status": "active",
    "created": True,
    "nudged": True,
    "created_at": "2026-08-20T09:15:00Z",
    "updated_at": "2026-08-20T09:15:00Z",
    "sms": {"queued": False, "credits": 1, "skipped_reason": "insufficient_credit"},
    "sms_units": 0,
}

MESSAGE_VIEW: dict[str, Any] = {
    "id": "m_8sKq2Vd1",
    "recipient_email": "buyer@example.com",
    "subject": "Your order has shipped",
    "kind": "one_off",
    "status": "active",
    "created_at": "2026-08-20T09:15:00Z",
    "updated_at": "2026-08-20T09:16:00Z",
    "delivery": {
        "delivered": True,
        "delivered_at": "2026-08-20T09:15:30Z",
        "opened": False,
        "opened_at": None,
        "bounced": False,
        "bounced_at": None,
        "sms": {
            "status": "delivered",
            "skipped_reason": None,
            "credits_charged": 1,
            "submitted_at": "2026-08-20T09:15:10Z",
            "last_status_at": "2026-08-20T09:15:25Z",
        },
    },
}


def handler_for(fn: Callable[[httpx.Request], httpx.Response]) -> httpx.MockTransport:
    return httpx.MockTransport(fn)
