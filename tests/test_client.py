"""Client construction, configuration and lifecycle."""

from __future__ import annotations

from typing import Any, cast

import httpx
import pytest

from beaconbox import AsyncBeaconBox, BeaconBox
from beaconbox._transport import DEFAULT_MAX_CONNECTIONS
from tests.conftest import API_KEY, BASE_URL, Reply, make_client


class TestApiKeyResolution:
    def test_reads_the_environment_when_no_key_is_passed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """So a key never has to appear in source. A key in a repository is a key in every
        clone, every CI log and every fork of it."""
        monkeypatch.setenv("BEACONBOX_API_KEY", API_KEY)

        assert BeaconBox().base_url == "https://api.beaconbox.eu"

    def test_the_argument_wins_over_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BEACONBOX_API_KEY", "bbx_live_from_env")
        client, recorder = make_client(
            Reply(200, {"balance": 0, "low_balance": False, "threshold": 0})
        )

        client.credits.balance()

        assert recorder.only.headers["Authorization"] == f"Bearer {API_KEY}"

    def test_no_key_anywhere_fails_at_construction(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Not at the first request, where it would surface as a confusing 401."""
        monkeypatch.delenv("BEACONBOX_API_KEY", raising=False)

        with pytest.raises(ValueError, match="no API key"):
            BeaconBox()

    def test_base_url_can_come_from_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BEACONBOX_BASE_URL", "http://localhost:8000")

        assert BeaconBox(API_KEY).base_url == "http://localhost:8000"


class TestSecrets:
    def test_the_key_is_not_in_the_repr(self) -> None:
        client, _ = make_client()
        assert API_KEY not in repr(client)
        assert BASE_URL in repr(client)

    def test_the_key_is_not_in_an_error_from_a_failed_request(self) -> None:
        """Exceptions get logged with their full text far more often than anyone intends."""
        from beaconbox import AuthenticationError

        client, _ = make_client(Reply(401, {"error_code": "business.invalid_api_key"}))

        with pytest.raises(AuthenticationError) as caught:
            client.credits.balance()

        assert API_KEY not in str(caught.value)
        assert API_KEY not in repr(caught.value.body)


class TestLifecycle:
    def test_context_manager_closes_the_pool(self) -> None:
        with BeaconBox(API_KEY, base_url=BASE_URL) as client:
            assert client.base_url == BASE_URL
        assert client._http.is_closed

    def test_close_is_a_no_op_for_an_injected_client(self) -> None:
        """You passed it in, so you own its lifetime. Closing a client somebody else is still
        using is a bug this SDK should not be able to cause."""
        injected = httpx.Client()
        client = BeaconBox(API_KEY, base_url=BASE_URL, http_client=injected)

        client.close()

        assert not injected.is_closed
        injected.close()

    async def test_async_context_manager_closes_the_pool(self) -> None:
        async with AsyncBeaconBox(API_KEY, base_url=BASE_URL) as client:
            assert client.base_url == BASE_URL
        assert client._http.is_closed


class TestSecurityDefaults:
    def test_redirects_are_never_followed(self) -> None:
        """httpx would re-send the Authorization header to wherever the redirect points, so a
        compromised hop could harvest a live API key."""
        client, _ = make_client()
        assert client._http.follow_redirects is False

    def test_the_connect_timeout_is_capped_below_the_read_timeout(self) -> None:
        """Connecting either works quickly or is not going to. A 300-second overall timeout
        should not also govern the handshake."""
        client = BeaconBox(API_KEY, base_url=BASE_URL, timeout=300.0)

        assert client._http.timeout.connect == 10.0
        assert client._http.timeout.read == 300.0

    def test_an_httpx_timeout_object_is_passed_through_untouched(self) -> None:
        client = BeaconBox(API_KEY, base_url=BASE_URL, timeout=httpx.Timeout(5.0, connect=1.0))

        assert client._http.timeout.connect == 1.0


def _pool_ceiling(client: BeaconBox | AsyncBeaconBox) -> int:
    """The ceiling as the connection pool actually received it.

    This reaches through httpx's internals on purpose. The assertion worth making is that the
    number reached the *pool* — a test that only checked the argument was stored would pass just
    as happily if the plumbing between the two were cut, which is the bug being guarded against.
    httpx exposes no public accessor, hence the cast.
    """
    return int(cast(Any, client._http._transport)._pool._max_connections)


class TestConnectionCeiling:
    """The pool is a ceiling on concurrency, and exceeding it does not look like a limit: the
    overflow queues, and a queue wait that expires is reported as a failed request."""

    def test_there_is_a_default(self) -> None:
        assert _pool_ceiling(BeaconBox(API_KEY, base_url=BASE_URL)) == DEFAULT_MAX_CONNECTIONS

    def test_a_caller_fanning_out_widely_can_raise_it(self) -> None:
        client = BeaconBox(API_KEY, base_url=BASE_URL, max_connections=200)

        assert _pool_ceiling(client) == 200

    def test_the_async_client_takes_it_too(self) -> None:
        """The one that matters: `asyncio.gather` makes exceeding this trivial."""
        client = AsyncBeaconBox(API_KEY, base_url=BASE_URL, max_connections=500)

        assert _pool_ceiling(client) == 500

    def test_a_ceiling_below_one_is_refused_rather_than_deadlocking(self) -> None:
        with pytest.raises(ValueError, match="at least 1"):
            BeaconBox(API_KEY, base_url=BASE_URL, max_connections=0)

    def test_it_cannot_be_combined_with_an_injected_client(self) -> None:
        """Same rule as timeout and verify: it is a setting on the client you supplied."""
        with pytest.raises(ValueError, match="cannot be combined with http_client"):
            BeaconBox(API_KEY, base_url=BASE_URL, http_client=httpx.Client(), max_connections=100)


class TestUserAgent:
    def test_identifies_the_sdk_and_its_version(self) -> None:
        from beaconbox import __version__

        client, recorder = make_client(
            Reply(200, {"balance": 0, "low_balance": False, "threshold": 0})
        )
        client.credits.balance()

        assert recorder.only.headers["User-Agent"] == f"beaconbox-python/{__version__}"

    def test_an_integration_can_add_its_own(self) -> None:
        client, recorder = make_client(
            Reply(200, {"balance": 0, "low_balance": False, "threshold": 0}),
            user_agent_suffix="acme-orders/2.1",
        )
        client.credits.balance()

        assert recorder.only.headers["User-Agent"].endswith(" acme-orders/2.1")


class TestNoSilentlyIgnoredOptions:
    """A setting that is accepted and quietly does nothing is worse than one that is refused. A
    caller who passes `timeout=5` alongside their own client believes they set a five second
    timeout, and finds out otherwise during an incident."""

    def test_timeout_with_an_injected_client_is_refused(self) -> None:
        with pytest.raises(ValueError, match="cannot be combined with http_client"):
            BeaconBox(API_KEY, base_url=BASE_URL, http_client=httpx.Client(), timeout=5.0)

    def test_verify_with_an_injected_client_is_refused(self) -> None:
        with pytest.raises(ValueError, match="cannot be combined with http_client"):
            BeaconBox(API_KEY, base_url=BASE_URL, http_client=httpx.Client(), verify="/ca.pem")

    def test_both_are_named_in_the_message(self) -> None:
        with pytest.raises(ValueError, match="timeout and verify"):
            BeaconBox(
                API_KEY,
                base_url=BASE_URL,
                http_client=httpx.Client(),
                timeout=5.0,
                verify="/ca.pem",
            )

    def test_an_injected_client_alone_is_fine(self) -> None:
        injected = httpx.Client()
        client = BeaconBox(API_KEY, base_url=BASE_URL, http_client=injected)

        assert client._http is injected
        injected.close()

    def test_the_async_client_refuses_the_same_combination(self) -> None:
        with pytest.raises(ValueError, match="cannot be combined with http_client"):
            AsyncBeaconBox(API_KEY, base_url=BASE_URL, http_client=httpx.AsyncClient(), timeout=5.0)

    def test_defaults_still_apply_when_no_client_is_injected(self) -> None:
        client = BeaconBox(API_KEY, base_url=BASE_URL)

        assert client._http.timeout.read == 30.0
        client.close()
