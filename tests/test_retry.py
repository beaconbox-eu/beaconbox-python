"""Retry behaviour, and above all the property that makes retrying safe at all.

If only one test in this file survives, it should be
:meth:`TestTheKeyIsReused.test_the_same_key_goes_out_on_every_attempt`. Retrying a write without
reusing the key is not a milder bug than not retrying: it is a second order-update email and a
second charged SMS to a real person, on the exact failure the retry exists to survive.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from beaconbox import (
    APIConnectionError,
    ConflictError,
    RateLimitError,
    RetryPolicy,
    ServerError,
)
from tests.conftest import PUSH_RESULT, Reply, make_client, no_sleep

PUSH: dict[str, Any] = {
    "recipient_email": "buyer@example.com",
    "subject": "Your order has shipped",
    "body": "Tracking XY123456789EE.",
}


class TestPolicy:
    @pytest.mark.parametrize("status", [429, 500, 502, 503])
    def test_retries_what_might_work_next_time(self, status: int) -> None:
        assert RetryPolicy().should_retry(status, attempt=0) is True

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422])
    def test_never_retries_a_client_error(self, status: int) -> None:
        """Sending the same wrong request again asks the same question."""
        assert RetryPolicy().should_retry(status, attempt=0) is False

    def test_retries_when_no_response_arrived(self) -> None:
        assert RetryPolicy().should_retry(None, attempt=0) is True

    def test_stops_at_the_budget(self) -> None:
        policy = RetryPolicy(max_retries=2)
        assert policy.should_retry(500, attempt=1) is True
        assert policy.should_retry(500, attempt=2) is False

    def test_zero_retries_disables_it(self) -> None:
        assert RetryPolicy(max_retries=0).should_retry(500, attempt=0) is False

    def test_backoff_grows_and_is_capped(self) -> None:
        policy = RetryPolicy(base_delay=0.2, max_delay=2.0)
        assert all(0 <= policy.delay(attempt) <= 2.0 for attempt in range(10))
        assert max(policy.delay(0) for _ in range(200)) <= 0.2

    def test_backoff_is_jittered(self) -> None:
        """Unjittered backoff makes every worker retry in lockstep, which is a thundering herd
        arriving exactly when the service is least able to answer."""
        policy = RetryPolicy(base_delay=1.0, max_delay=10.0)
        assert len({policy.delay(3) for _ in range(50)}) > 1

    def test_retry_after_is_honoured_in_full_not_clamped_to_max_delay(self) -> None:
        """``max_delay`` caps a delay the SDK invented; a ``Retry-After`` is the server's own
        answer to when it will be ready, and shortening it only earns a second 429."""
        policy = RetryPolicy(max_delay=2.0, max_retry_after=30.0)
        assert policy.delay(0, retry_after=10.0) >= 10.0

    def test_retry_after_is_never_waited_out_below_what_the_server_asked(self) -> None:
        """The floor, sampled hard. Full jitter *within* the directive would undercut it."""
        policy = RetryPolicy(max_delay=2.0, max_retry_after=30.0)
        assert all(policy.delay(0, retry_after=5.0) >= 5.0 for _ in range(200))

    def test_retry_after_is_jittered_on_top_of_itself(self) -> None:
        """The herd is at its *worst* on this path: every worker that hit the same 429 was handed
        the same number, so obeying it exactly reconstructs the lockstep jitter exists to break."""
        policy = RetryPolicy(max_delay=2.0, max_retry_after=30.0)
        waits = {policy.delay(0, retry_after=5.0) for _ in range(50)}
        assert len(waits) > 1, "every worker would retry in the same millisecond"
        assert max(waits) <= 5.0 + 1.0, "spread is bounded to a fraction of the wait"

    def test_a_hostile_header_still_cannot_park_a_caller(self) -> None:
        """The original reason for the clamp survives, moved onto its own cap."""
        policy = RetryPolicy(max_retry_after=30.0)
        assert policy.delay(0, retry_after=3600.0) <= 30.0 * (1 + 0.2)

    def test_a_wait_longer_than_we_will_sit_through_stops_the_retry(self) -> None:
        """Retrying early is not a compromise: it is a request the server has already said it
        will refuse. Better to hand the caller the number and let them schedule it."""
        policy = RetryPolicy(max_retries=3, max_retry_after=30.0)
        assert policy.should_retry(429, attempt=0, retry_after=10.0) is True
        assert policy.should_retry(429, attempt=0, retry_after=300.0) is False

    def test_no_retry_after_header_is_unaffected(self) -> None:
        policy = RetryPolicy(max_retries=3, max_retry_after=30.0)
        assert policy.should_retry(429, attempt=0, retry_after=None) is True
        assert policy.should_retry(503, attempt=0) is True


class TestTheKeyIsReused:
    def test_the_same_key_goes_out_on_every_attempt(self) -> None:
        """The property this whole module rests on. See the module docstring."""
        client, recorder = make_client(
            [Reply(503), Reply(503), Reply(201, PUSH_RESULT)],
            retry_policy=RetryPolicy(max_retries=2, base_delay=0, max_delay=0),
        )

        client.messages.push(**PUSH)

        keys = recorder.idempotency_keys()
        assert len(keys) == 3
        assert len(set(keys)) == 1, f"a retry minted a new key: {keys}"

    def test_a_caller_key_survives_the_retries_too(self) -> None:
        client, recorder = make_client(
            [Reply(500), Reply(201, PUSH_RESULT)],
            retry_policy=RetryPolicy(max_retries=1, base_delay=0, max_delay=0),
        )

        client.messages.push(**PUSH, idempotency_key="order-4711-shipped")

        assert recorder.idempotency_keys() == ["order-4711-shipped"] * 2

    def test_two_separate_calls_get_separate_keys(self) -> None:
        """The other half: reuse within a call, never across calls. Two pushes are two
        messages."""
        client, recorder = make_client(Reply(201, PUSH_RESULT))

        client.messages.push(**PUSH)
        client.messages.push(**PUSH)

        keys = recorder.idempotency_keys()
        assert keys[0] != keys[1]


class TestRetryLoop:
    def test_retries_a_lost_connection_then_succeeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        no_sleep(monkeypatch)
        client, recorder = make_client(
            [Reply(raise_error=httpx.ConnectError("refused")), Reply(201, PUSH_RESULT)],
            retry_policy=RetryPolicy(max_retries=2),
        )

        result = client.messages.push(**PUSH)

        assert result.id == "m_8sKq2Vd1"
        assert len(recorder.requests) == 2

    def test_gives_up_and_says_what_is_not_known(self, monkeypatch: pytest.MonkeyPatch) -> None:
        no_sleep(monkeypatch)
        client, _ = make_client(
            Reply(raise_error=httpx.ConnectTimeout("timed out")),
            retry_policy=RetryPolicy(max_retries=1),
        )

        with pytest.raises(APIConnectionError) as caught:
            client.messages.push(**PUSH)

        assert "not proof the work did not happen" in str(caught.value)

    def test_raises_after_exhausting_retries_on_5xx(self, monkeypatch: pytest.MonkeyPatch) -> None:
        no_sleep(monkeypatch)
        client, recorder = make_client(Reply(503), retry_policy=RetryPolicy(max_retries=2))

        with pytest.raises(ServerError):
            client.messages.push(**PUSH)

        assert len(recorder.requests) == 3

    def test_honours_retry_after_on_a_429(self, monkeypatch: pytest.MonkeyPatch) -> None:
        slept = no_sleep(monkeypatch)
        client, _ = make_client(
            Reply(429, headers={"Retry-After": "1"}),
            retry_policy=RetryPolicy(max_retries=1, max_delay=5.0),
        )

        with pytest.raises(RateLimitError):
            client.messages.push(**PUSH)

        assert len(slept) == 1
        assert 1.0 <= slept[0] <= 1.2, "the directive is a floor, plus a bounded jitter"

    def test_a_long_retry_after_fails_fast_and_hands_back_the_number(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The caller waits nothing and learns when to come back, instead of blocking for the
        cap and being refused anyway."""
        slept = no_sleep(monkeypatch)
        client, recorder = make_client(
            Reply(429, headers={"Retry-After": "600"}),
            retry_policy=RetryPolicy(max_retries=3, max_retry_after=30.0),
        )

        with pytest.raises(RateLimitError) as caught:
            client.messages.push(**PUSH)

        assert slept == [], "slept through none of a wait it had already decided not to honour"
        assert len(recorder.requests) == 1, "and spent no further calls on a rate-limited service"
        assert caught.value.retry_after == 600.0

    def test_retry_after_survives_onto_the_exception(self) -> None:
        client, _ = make_client(Reply(429, headers={"Retry-After": "42"}))

        with pytest.raises(RateLimitError) as caught:
            client.messages.push(**PUSH)

        assert caught.value.retry_after == 42.0

    def test_an_error_without_the_header_has_no_retry_after(self) -> None:
        client, _ = make_client(Reply(429))

        with pytest.raises(RateLimitError) as caught:
            client.messages.push(**PUSH)

        assert caught.value.retry_after is None

    def test_a_conflict_is_reported_not_waited_out(self) -> None:
        """ "Your earlier attempt is still running" is a truthful answer to give a caller. A
        client that quietly blocked for a second instead would be hiding it."""
        client, recorder = make_client(
            Reply(409, {"error_code": "message.push_conflict"}),
            retry_policy=RetryPolicy(max_retries=3),
        )

        with pytest.raises(ConflictError):
            client.messages.push(**PUSH)

        assert len(recorder.requests) == 1

    def test_a_read_is_retried_too(self, monkeypatch: pytest.MonkeyPatch) -> None:
        no_sleep(monkeypatch)
        client, recorder = make_client(
            [Reply(500), Reply(200, {"balance": 10, "low_balance": False, "threshold": 5})],
            retry_policy=RetryPolicy(max_retries=1),
        )

        assert client.credits.balance().balance == 10
        assert len(recorder.requests) == 2
