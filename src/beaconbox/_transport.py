"""The two things that genuinely differ between a sync client and an async one.

Everything else lives in :mod:`beaconbox._core`. What is left here is: how a request is sent, and
how the process waits between attempts. Both transports run the *same* retry decision, over the
same prepared request, with the same idempotency key, because that logic is not duplicated.

The async transport blocks nothing. ``httpx.AsyncClient`` is awaited rather than run in a thread,
and the backoff is ``asyncio.sleep``, so a merchant's event loop keeps serving other requests
while BeaconBox is slow. This is the difference the `async` half of this SDK exists to buy, and
wrapping the sync path in ``run_in_executor`` would have thrown it away while looking identical
from the outside.
"""

from __future__ import annotations

import asyncio
import logging
import os
import ssl
import time
from typing import Any, TypeVar

import httpx

from ._core import Call, Core, PreparedRequest, new_idempotency_key, retry_after_seconds
from ._logging import get_logger, safe_extra
from .errors import APIConnectionError
from .retry import RetryPolicy

T = TypeVar("T")

logger = get_logger(__name__)

DEFAULT_TIMEOUT = 30.0
DEFAULT_CONNECT_TIMEOUT = 10.0
DEFAULT_MAX_CONNECTIONS = 20


def default_timeout(timeout: float | httpx.Timeout) -> httpx.Timeout:
    """A read timeout the caller chooses, and a connect timeout they rarely should.

    Connecting either works quickly or is not going to. Letting a 300-second overall timeout also
    govern the handshake means a merchant's worker sits for five minutes on a host that is simply
    not there.
    """
    if isinstance(timeout, httpx.Timeout):
        return timeout
    return httpx.Timeout(timeout, connect=min(DEFAULT_CONNECT_TIMEOUT, timeout))


def client_kwargs(
    timeout: float | httpx.Timeout,
    verify: Any,
    max_connections: int = DEFAULT_MAX_CONNECTIONS,
) -> dict[str, Any]:
    """Shared httpx settings, and the security-relevant ones are not options.

    ``follow_redirects=False``: httpx would re-send the ``Authorization`` header to wherever a
    redirect points, so a compromised or misconfigured hop could harvest a live API key. BeaconBox
    never redirects, so anything that does is not BeaconBox.

    ``verify`` is exposed only because a corporate TLS-inspecting proxy is a real thing that needs
    a custom CA bundle. Pass a path or an ``ssl.SSLContext``. Passing ``False`` disables
    certificate verification entirely and there is no legitimate production reason to.

    ``max_connections`` is a *ceiling on concurrency*, not a tuning knob for throughput, and it is
    exposed because the failure it causes does not look like a limit being reached. Work beyond it
    queues for a free connection, that wait is governed by the pool timeout, and a wait that
    expires surfaces as ``PoolTimeout`` — which this SDK retries and then reports as
    :class:`~beaconbox.errors.APIConnectionError`, "the request did not complete". A caller
    fanning out several hundred concurrent pushes against a slow API therefore sees connection
    errors rather than slowness, and nothing in that message points at a pool.
    """
    if max_connections < 1:
        raise ValueError(f"BeaconBox: max_connections must be at least 1, got {max_connections}")
    return {
        "timeout": default_timeout(timeout),
        "verify": _ssl_verify(verify),
        "follow_redirects": False,
        "limits": httpx.Limits(max_connections=max_connections),
    }


def _ssl_verify(verify: Any) -> Any:
    """Turn a CA bundle path into an ``SSLContext`` before httpx sees it.

    httpx deprecated ``verify=<str>`` in favour of building the context yourself. Doing that here
    rather than in the docs means the person who correctly passes their company's CA bundle does
    not get a ``DeprecationWarning`` for their trouble, and does not go looking for a way to make
    it stop that ends in ``verify=False``.
    """
    if isinstance(verify, (str, os.PathLike)):
        return ssl.create_default_context(cafile=os.fspath(verify))
    return verify


class SyncTransport:
    """Sends prepared requests with ``httpx.Client``, retrying under :class:`RetryPolicy`."""

    def __init__(self, core: Core, http_client: httpx.Client, retry_policy: RetryPolicy) -> None:
        self._core = core
        self._http = http_client
        self._retry = retry_policy

    def invoke(self, call: Call[T], idempotency_key: str | None = None) -> T:
        """Run ``call`` to a final answer, or raise.

        The key is minted before the loop and the request prepared once, which is what makes
        every attempt after the first a genuine *retry* rather than a second push.
        """
        key = idempotency_key or (new_idempotency_key() if call.needs_idempotency else None)
        request = self._core.prepare(call, key)

        attempt = 0
        while True:
            _log_request(call, key, attempt)
            started = time.monotonic()
            try:
                response = self._send(request)
            except httpx.HTTPError as exc:
                if not self._retry.should_retry(None, attempt):
                    _log_gave_up(call, key, attempt, type(exc).__name__)
                    raise _connection_error(exc) from exc
                delay = self._retry.delay(attempt)
                _log_retry(call, key, attempt, type(exc).__name__, delay)
                time.sleep(delay)
                attempt += 1
                continue

            _log_response(call, key, attempt, response.status_code, time.monotonic() - started)
            # Read once and pass to both: the decision to retry now depends on how long the
            # server asked for, not only on the status. See RetryPolicy.should_retry.
            retry_after = retry_after_seconds(response.headers)
            if response.status_code < 400 or not self._retry.should_retry(
                response.status_code, attempt, retry_after
            ):
                return self._core.interpret(
                    call, response.status_code, response.headers, response.content
                )

            delay = self._retry.delay(attempt, retry_after)
            _log_retry(call, key, attempt, f"HTTP {response.status_code}", delay)
            time.sleep(delay)
            attempt += 1

    def _send(self, request: PreparedRequest) -> httpx.Response:
        return self._http.request(
            request.method,
            request.url,
            headers=request.headers,
            content=request.content,
        )


class AsyncTransport:
    """The same, awaited. See the module docstring for why this is not the sync path in a
    thread."""

    def __init__(
        self, core: Core, http_client: httpx.AsyncClient, retry_policy: RetryPolicy
    ) -> None:
        self._core = core
        self._http = http_client
        self._retry = retry_policy

    async def invoke(self, call: Call[T], idempotency_key: str | None = None) -> T:
        """Async form of :meth:`SyncTransport.invoke`, with identical retry semantics."""
        key = idempotency_key or (new_idempotency_key() if call.needs_idempotency else None)
        request = self._core.prepare(call, key)

        attempt = 0
        while True:
            _log_request(call, key, attempt)
            started = time.monotonic()
            try:
                response = await self._send(request)
            except httpx.HTTPError as exc:
                if not self._retry.should_retry(None, attempt):
                    _log_gave_up(call, key, attempt, type(exc).__name__)
                    raise _connection_error(exc) from exc
                delay = self._retry.delay(attempt)
                _log_retry(call, key, attempt, type(exc).__name__, delay)
                await asyncio.sleep(delay)
                attempt += 1
                continue

            _log_response(call, key, attempt, response.status_code, time.monotonic() - started)
            # Read once and pass to both, exactly as the sync transport does.
            retry_after = retry_after_seconds(response.headers)
            if response.status_code < 400 or not self._retry.should_retry(
                response.status_code, attempt, retry_after
            ):
                return self._core.interpret(
                    call, response.status_code, response.headers, response.content
                )

            delay = self._retry.delay(attempt, retry_after)
            _log_retry(call, key, attempt, f"HTTP {response.status_code}", delay)
            await asyncio.sleep(delay)
            attempt += 1

    async def _send(self, request: PreparedRequest) -> httpx.Response:
        return await self._http.request(
            request.method,
            request.url,
            headers=request.headers,
            content=request.content,
        )


def _log_request(call: Call[Any], key: str | None, attempt: int) -> None:
    """The ``isEnabledFor`` guard is not premature: without it every request formats a message and
    builds a dict that a disabled logger throws away, on the hot path of a library whose whole job
    is making requests."""
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "request %s (attempt %d)",
            call.log_name,
            attempt + 1,
            extra=safe_extra(route=call.log_name, attempt=attempt + 1, idempotency_key=key),
        )


def _log_response(
    call: Call[Any], key: str | None, attempt: int, status_code: int, elapsed: float
) -> None:
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "response %s %d in %dms",
            call.log_name,
            status_code,
            round(elapsed * 1000),
            extra=safe_extra(
                route=call.log_name,
                attempt=attempt + 1,
                status_code=status_code,
                elapsed_ms=round(elapsed * 1000),
                idempotency_key=key,
            ),
        )


def _log_retry(call: Call[Any], key: str | None, attempt: int, reason: str, delay: float) -> None:
    """A warning rather than a debug: a retry means something went wrong, and it is the signal that
    explains why a merchant's job took four seconds instead of one.

    It names the idempotency key so a reader can see that the retry reused it. A retry that minted
    a fresh key would be a duplicate message, and this line is where that would be visible.
    """
    logger.warning(
        "retrying %s after %s, attempt %d in %dms",
        call.log_name,
        reason,
        attempt + 2,
        round(delay * 1000),
        extra=safe_extra(
            route=call.log_name,
            attempt=attempt + 1,
            reason=reason,
            retry_in_ms=round(delay * 1000),
            idempotency_key=key,
        ),
    )


def _log_gave_up(call: Call[Any], key: str | None, attempt: int, reason: str) -> None:
    """Logged *and* raised, which is usually double reporting and here is not: the caller sees an
    exception saying the answer was lost, and the operator needs the same fact correlated with the
    retries above it."""
    logger.warning(
        "giving up on %s after %s (%d attempt(s))",
        call.log_name,
        reason,
        attempt + 1,
        extra=safe_extra(
            route=call.log_name, attempts=attempt + 1, reason=reason, idempotency_key=key
        ),
    )


def _connection_error(exc: httpx.HTTPError) -> APIConnectionError:
    """The message says what is and is not known, because the distinction is the whole point.

    A caller who reads this as "it did not send" and pushes again with a fresh key has sent the
    customer two emails.
    """
    return APIConnectionError(
        f"BeaconBox: the request did not complete ({type(exc).__name__}: {exc}). "
        "This is not proof the work did not happen: retry with the same idempotency key."
    )
