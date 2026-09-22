"""The part of this SDK that has no idea HTTP exists.

Everything here is a pure function of its inputs: turn a :class:`Call` into the bytes and headers
that should go on the wire, and turn a status code plus a body back into a model or an exception.
No sockets, no sleeping, no httpx import anywhere in this module.

That line is what lets the sync client and the async client be the same client. Authentication,
the idempotency key, URL construction, JSON encoding, error mapping and response parsing are
written once here; :mod:`beaconbox._transport` adds only the two things that genuinely differ
between them, which are how you send a request and how you wait.

It is also what makes the SDK testable without a network: a test can build a ``Call``, prepare it
and assert on the exact headers, or hand :func:`Core.interpret` a canned 429 and assert on the
exception, with no server and no mocking library involved.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar
from urllib.parse import quote, urlencode, urlsplit

from ._version import __version__
from .errors import APIError, error_class_for

T = TypeVar("T")

API_PREFIX = "/api/v1"
DEFAULT_BASE_URL = "https://api.beaconbox.eu"

_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})


def new_idempotency_key() -> str:
    """A fresh v4 UUID for the ``Idempotency-Key`` header.

    Minted once per logical call and reused for every retry of it. See
    :meth:`Core.prepare`, which is where that pairing is enforced.
    """
    return str(uuid.uuid4())


@dataclass(frozen=True)
class Call(Generic[T]):
    """One API operation, described without reference to how it will be sent.

    ``parse`` is carried alongside the request rather than looked up afterwards, so a resource
    method's return type is fixed at the point the call is described and a transport cannot get
    it wrong.
    """

    method: str
    path: str
    parse: Callable[[Any], T]
    body: dict[str, Any] | None = None
    params: Mapping[str, Any] = field(default_factory=dict)

    route: str | None = None
    """A templated, **log-safe** name for this operation, for example
    ``/recipients/{email}/sms``.

    Set it on every call whose path interpolates a value. ``path`` cannot be logged: a recipient's
    email address is substituted straight into it, so logging the path would write personal data
    into a merchant's log aggregator on every request. Query strings are never logged for the same
    reason (``?recipient_email=``), and neither are bodies or headers.
    """

    @property
    def log_name(self) -> str:
        """What :mod:`beaconbox._transport` logs. Never the interpolated path."""
        return f"{self.method} {self.route or self.path}"

    @property
    def needs_idempotency(self) -> bool:
        """Every ``POST`` in this API requires an ``Idempotency-Key``, and nothing else does.

        ``PUT`` and ``DELETE`` on a recipient's phone number are idempotent by construction:
        setting a number twice leaves one number. Sending a message twice sends two messages and
        charges two credits, which is the whole reason the header is mandatory on ``POST``.
        """
        return self.method == "POST"


@dataclass(frozen=True)
class PreparedRequest:
    """Exactly what should go on the wire. Built once, sent as many times as it takes."""

    method: str
    url: str
    headers: dict[str, str]
    content: bytes | None


class Core:
    """Request building and response interpretation, shared by both clients."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        user_agent_suffix: str | None = None,
    ) -> None:
        if not api_key or not api_key.strip():
            raise ValueError(
                "BeaconBox: an API key is required. Pass api_key= or set BEACONBOX_API_KEY."
            )
        self._api_key = api_key.strip()
        self.base_url = _validated_base_url(base_url)
        suffix = f" {user_agent_suffix}" if user_agent_suffix else ""
        self.user_agent = f"beaconbox-python/{__version__}{suffix}"

    def __repr__(self) -> str:
        """No key, ever. This object ends up inside client reprs, tracebacks and log lines."""
        return f"Core(base_url={self.base_url!r})"

    def prepare(self, call: Call[Any], idempotency_key: str | None = None) -> PreparedRequest:
        """Build the request for ``call``.

        **Call this once per logical operation, outside any retry loop.** The idempotency key is
        minted here, so preparing again per attempt would mint a second key, and a second key is
        what turns a retry into a duplicate message and a duplicate charge.
        """
        url = self.base_url + API_PREFIX + call.path
        query = {k: _query_value(v) for k, v in call.params.items() if v is not None}
        if query:
            url += "?" + urlencode(query)

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
            "User-Agent": self.user_agent,
        }
        content: bytes | None = None
        if call.body is not None:
            headers["Content-Type"] = "application/json"
            content = json.dumps(call.body, separators=(",", ":")).encode()
        if call.needs_idempotency:
            headers["Idempotency-Key"] = idempotency_key or new_idempotency_key()

        return PreparedRequest(method=call.method, url=url, headers=headers, content=content)

    def interpret(
        self,
        call: Call[T],
        status_code: int,
        headers: Mapping[str, str],
        content: bytes,
    ) -> T:
        """Turn a response into a model, or raise the matching :class:`~beaconbox.errors.APIError`.

        Only called for a response the transport has decided not to retry, so anything reaching
        here with a 4xx or 5xx is final.
        """
        if status_code >= 400:
            raise self._to_error(status_code, headers, content)
        return call.parse(_decode(content))

    def _to_error(self, status_code: int, headers: Mapping[str, str], content: bytes) -> APIError:
        body = _decode(content)
        body = body if isinstance(body, dict) else {"raw": body}
        error_code = body.get("error_code") if isinstance(body.get("error_code"), str) else None
        detail = body.get("detail")
        summary = detail if isinstance(detail, str) else error_code or "request failed"
        request_id = _header(headers, "x-request-id")
        return error_class_for(status_code)(
            f"BeaconBox: {summary} (HTTP {status_code})",
            status_code=status_code,
            error_code=error_code,
            body=body,
            request_id=request_id,
            retry_after=retry_after_seconds(headers),
        )


def retry_after_seconds(headers: Mapping[str, str]) -> float | None:
    """``Retry-After`` in seconds, when the server sent a sane one.

    Only the delta-seconds form is honoured. The HTTP-date form is legal and essentially never
    used by an API, and parsing it would mean trusting the caller's clock to agree with the
    server's, which is the assumption that makes it worse than the SDK's own backoff.
    """
    value = _header(headers, "retry-after")
    if value is None:
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def _header(headers: Mapping[str, str], name: str) -> str | None:
    """Case-insensitive lookup, because a raw mapping in a test is not httpx's ``Headers``."""
    for key, value in headers.items():
        if key.lower() == name:
            return value
    return None


def _decode(content: bytes) -> Any:
    """Body to Python. A 204 has none, and a non-JSON body is preserved rather than swallowed."""
    if not content or not content.strip():
        return {}
    try:
        return json.loads(content)
    except ValueError:
        return {"raw": content.decode("utf-8", "replace")}


def _query_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _validated_base_url(base_url: str) -> str:
    """Reject a base URL that would put an API key on the wire in clear.

    ``http`` is allowed only for a loopback host, which is what the local development stack and
    the SDK's own live tests run against. Anywhere else it means the bearer token, the recipient's
    email address and the body of the message are readable by anything on the path, and an SDK
    that shrugs at that is the reason it happens in production.
    """
    cleaned = base_url.strip().rstrip("/")
    parts = urlsplit(cleaned)
    if parts.scheme not in {"http", "https"}:
        raise ValueError(f"BeaconBox: base_url must be http or https, got {base_url!r}")
    if not parts.netloc:
        raise ValueError(f"BeaconBox: base_url must include a host, got {base_url!r}")
    if parts.scheme == "http" and (parts.hostname or "") not in _LOCAL_HOSTS:
        raise ValueError(
            f"BeaconBox: refusing to send an API key over plain http to {parts.hostname!r}. "
            "Use https (http is permitted for localhost only)."
        )
    return cleaned


def path_segment(value: str) -> str:
    """Percent-encode one path segment.

    ``safe=""`` so that an email address's ``@``, and above all a ``/``, cannot walk out of the
    segment they were substituted into and address a different endpoint.
    """
    return quote(value, safe="")
