"""The two clients. Same resources, same behaviour, one of them awaited."""

from __future__ import annotations

import os
from types import TracebackType
from typing import Any

import httpx

from ._core import DEFAULT_BASE_URL, Core
from ._transport import (
    DEFAULT_MAX_CONNECTIONS,
    DEFAULT_TIMEOUT,
    AsyncTransport,
    SyncTransport,
    client_kwargs,
)
from .async_resources import (
    AsyncCredits,
    AsyncKeys,
    AsyncMessages,
    AsyncRecipients,
    AsyncWebhookEndpoints,
)
from .resources import Credits, Keys, Messages, Recipients, WebhookEndpoints
from .retry import RetryPolicy

__all__ = ["AsyncBeaconBox", "BeaconBox"]

API_KEY_ENV = "BEACONBOX_API_KEY"
BASE_URL_ENV = "BEACONBOX_BASE_URL"


def _resolve_api_key(api_key: str | None) -> str:
    """Argument first, then ``BEACONBOX_API_KEY``.

    The environment variable exists so the key never has to appear in source. A key in a
    repository is a key in every clone, every CI log and every fork of it, and revoking it is the
    only remedy.
    """
    resolved = api_key or os.environ.get(API_KEY_ENV)
    if not resolved:
        raise ValueError(
            f"BeaconBox: no API key. Pass api_key= or set the {API_KEY_ENV} environment variable."
        )
    return resolved


def _resolve_base_url(base_url: str | None) -> str:
    return base_url or os.environ.get(BASE_URL_ENV) or DEFAULT_BASE_URL


class _Unset:
    """Sentinel, so "not passed" is distinguishable from "passed the default"."""

    def __repr__(self) -> str:
        return "<unset>"


UNSET = _Unset()


def _or_default(value: int | _Unset, default: int) -> int:
    """Resolve a possibly-unset numeric option. Keeps the two client constructors symmetrical."""
    return default if isinstance(value, _Unset) else value


def _check_http_client_conflict(
    timeout: object, verify: object, max_connections: object = UNSET
) -> None:
    """Refuse ``http_client=`` together with ``timeout=``, ``verify=`` or ``max_connections=``.

    All three are settings on the httpx client, so when you bring your own they have nowhere to
    go. Accepting them and quietly doing nothing is the worse failure: a caller who passes
    ``timeout=5`` alongside their own client believes they set a five second timeout, and finds out
    otherwise during an incident. Configure them on the client you are passing in.
    """
    conflicting = [
        name
        for name, value in (
            ("timeout", timeout),
            ("verify", verify),
            ("max_connections", max_connections),
        )
        if not isinstance(value, _Unset)
    ]
    if conflicting:
        joined = " and ".join(conflicting)
        raise ValueError(
            f"BeaconBox: {joined} cannot be combined with http_client, because "
            f"{'they are' if len(conflicting) > 1 else 'it is'} a setting on the HTTP client "
            "itself. Set it on the client you are passing in: "
            "httpx.Client(timeout=..., verify=..., limits=httpx.Limits(max_connections=...))."
        )


class BeaconBox:
    """The BeaconBox API, as five resources.

    .. code-block:: python

        from beaconbox import BeaconBox

        client = BeaconBox()  # reads BEACONBOX_API_KEY

        result = client.messages.push(
            recipient_email="buyer@example.com",
            subject="Your order has shipped",
            body="Tracking XY123456789EE.",
        )

    Two things worth knowing before anything else:

    * **It supplies the ``Idempotency-Key`` every write requires**, and reuses it across its own
      retries, so a dropped connection cannot become a duplicate message to a real customer.
    * **It does not raise on a skipped channel.** A push whose SMS was skipped for an empty
      balance is a *successful* push: the update is in the inbox and the email went. Read
      ``result.sms.skipped_reason``.

    Reuse one client. It holds a connection pool, so building one per request throws away
    connection reuse and TLS session resumption. It is safe to share between threads.

    Close it when you are finished, or use it as a context manager:

    .. code-block:: python

        with BeaconBox() as client:
            client.messages.push(...)

    :param api_key: Defaults to ``$BEACONBOX_API_KEY``.
    :param base_url: Defaults to ``$BEACONBOX_BASE_URL``, then the production API. Plain http is
        refused for anything but localhost, so a misconfiguration cannot put your key on the wire
        in clear.
    :param timeout: Seconds, or an ``httpx.Timeout``. The connect timeout is capped separately.
    :param retry_policy: :class:`~beaconbox.retry.RetryPolicy`. Pass ``RetryPolicy(max_retries=0)``
        inside a job runner that already owns its own retry schedule, so the two do not multiply.
    :param http_client: Bring your own ``httpx.Client`` for proxies, custom transports, mounts or
        instrumentation. You then own closing it: this client will not, and ``timeout`` and
        ``verify`` are refused alongside it because they are settings on the client you supplied.
    :param verify: TLS verification, for a corporate CA bundle. Turning it off has no legitimate
        production use.
    :param max_connections: Ceiling on connections held open at once (default
        :data:`~beaconbox._transport.DEFAULT_MAX_CONNECTIONS`, 20). Raise it if you fan out more
        concurrent calls than that and see
        :class:`~beaconbox.errors.APIConnectionError` under load: work past the ceiling
        queues for a free connection and a queue wait that expires is reported as a failed
        request, which does not read as "the pool was full".
    :param user_agent_suffix: Appended to the ``User-Agent``, for identifying your integration in
        support conversations.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        timeout: float | httpx.Timeout | _Unset = UNSET,
        retry_policy: RetryPolicy | None = None,
        http_client: httpx.Client | None = None,
        verify: Any = UNSET,
        max_connections: int | _Unset = UNSET,
        user_agent_suffix: str | None = None,
    ) -> None:
        self._core = Core(
            _resolve_api_key(api_key),
            base_url=_resolve_base_url(base_url),
            user_agent_suffix=user_agent_suffix,
        )
        self._owns_http = http_client is None
        if http_client is not None:
            _check_http_client_conflict(timeout, verify, max_connections)
        self._http = http_client or httpx.Client(
            **client_kwargs(
                DEFAULT_TIMEOUT if isinstance(timeout, _Unset) else timeout,
                True if isinstance(verify, _Unset) else verify,
                _or_default(max_connections, DEFAULT_MAX_CONNECTIONS),
            )
        )
        transport = SyncTransport(self._core, self._http, retry_policy or RetryPolicy())

        self.messages = Messages(transport)
        self.credits = Credits(transport)
        self.recipients = Recipients(transport)
        self.keys = Keys(transport)
        self.webhook_endpoints = WebhookEndpoints(transport)

    @property
    def base_url(self) -> str:
        return self._core.base_url

    def __repr__(self) -> str:
        """Never the key. A client often ends up in a traceback or a debug log line."""
        return f"BeaconBox(base_url={self.base_url!r})"

    def close(self) -> None:
        """Release the connection pool. A no-op for an injected client, which you own."""
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> BeaconBox:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


class AsyncBeaconBox:
    """The async client. Identical to :class:`BeaconBox`, awaited.

    .. code-block:: python

        from beaconbox import AsyncBeaconBox

        async with AsyncBeaconBox() as client:
            result = await client.messages.push(
                recipient_email="buyer@example.com",
                subject="Your order has shipped",
                body="Tracking XY123456789EE.",
            )

    Nothing here blocks the event loop, including retry backoff. Reuse one client for the
    lifetime of your application: creating one per request defeats the connection pool, and an
    ``AsyncClient`` that is never closed leaks its connections.

    See :class:`BeaconBox` for the parameters, which are the same. **``max_connections`` matters
    more here than there**, because this is the client you can trivially point a thousand
    concurrent tasks at: ``asyncio.gather`` over more calls than the ceiling does not fail, it
    queues, and the part of that queue whose wait outlives the pool timeout comes back as
    :class:`~beaconbox.errors.APIConnectionError` rather than as anything mentioning a pool.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        timeout: float | httpx.Timeout | _Unset = UNSET,
        retry_policy: RetryPolicy | None = None,
        http_client: httpx.AsyncClient | None = None,
        verify: Any = UNSET,
        max_connections: int | _Unset = UNSET,
        user_agent_suffix: str | None = None,
    ) -> None:
        self._core = Core(
            _resolve_api_key(api_key),
            base_url=_resolve_base_url(base_url),
            user_agent_suffix=user_agent_suffix,
        )
        self._owns_http = http_client is None
        if http_client is not None:
            _check_http_client_conflict(timeout, verify, max_connections)
        self._http = http_client or httpx.AsyncClient(
            **client_kwargs(
                DEFAULT_TIMEOUT if isinstance(timeout, _Unset) else timeout,
                True if isinstance(verify, _Unset) else verify,
                _or_default(max_connections, DEFAULT_MAX_CONNECTIONS),
            )
        )
        transport = AsyncTransport(self._core, self._http, retry_policy or RetryPolicy())

        self.messages = AsyncMessages(transport)
        self.credits = AsyncCredits(transport)
        self.recipients = AsyncRecipients(transport)
        self.keys = AsyncKeys(transport)
        self.webhook_endpoints = AsyncWebhookEndpoints(transport)

    @property
    def base_url(self) -> str:
        return self._core.base_url

    def __repr__(self) -> str:
        return f"AsyncBeaconBox(base_url={self.base_url!r})"

    async def aclose(self) -> None:
        """Release the connection pool. A no-op for an injected client, which you own."""
        if self._owns_http:
            await self._http.aclose()

    async def __aenter__(self) -> AsyncBeaconBox:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()
