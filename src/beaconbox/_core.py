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
import re
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

_UNSAFE_IN_HEADER = re.compile(r"[^\x20-\x7e]+")
"""Runs of anything an HTTP header value cannot carry: control characters and non-ASCII.

**One definition, two uses.** :func:`_fits_in_a_header` refuses a value containing any of it (the
API key, where silently altering a credential would be worse than an error) and :func:`_header_safe`
strips it (the ``User-Agent`` suffix, a label where an error would be worse). Written once, because
two spellings of the same character class is one of them drifting later.
"""


def _header_safe(value: str) -> str:
    """``value`` with anything a header cannot carry collapsed to a single space, then trimmed.

    For the ``User-Agent`` suffix, which is a label a human reads rather than a credential.
    Sanitised rather than refused: a stray newline in a caller's own configuration should not fail
    the constructor, and must not reach the header either.
    """
    return _UNSAFE_IN_HEADER.sub(" ", value).strip()


def _fits_in_a_header(value: str) -> bool:
    """Whether ``value`` can go in an HTTP header: printable ASCII, nothing else.

    An API key is base62, so this rejects nothing legitimate. What it catches is a key pasted with
    a trailing newline still inside it, or with a smart quote substituted by a word processor. A
    ``\r\n`` within a credential must never reach the ``Authorization`` header, and the refusal
    names the problem where it happened rather than surfacing as an encoding error at the first
    request.
    """
    return _UNSAFE_IN_HEADER.search(value) is None


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
        """Send an ``Idempotency-Key`` on every ``POST``, which is a deliberate superset.

        The API *requires* one on seven operations, all of them POSTs that send a message or move
        something a person can see: the push, the batch, the retract, both resends, and both
        erasures. ``POST /keys``, ``POST /webhook-endpoints`` and that endpoint's
        ``/test`` accept a request without one — creating a second key or a second endpoint is
        visible and undoable, so repeating it costs nobody a message or a credit.

        Sending it on those three anyway is harmless, and the rule stays "every POST" rather than
        a list because a list here would be a second copy of a decision the API already makes. The
        failure mode of the broad rule is an ignored header; the failure mode of a stale list is a
        push that goes out twice.

        ``PUT`` and ``DELETE`` carry none and need none: they are idempotent by construction, so
        setting a recipient's number twice leaves one number. Sending a message twice sends two
        messages and charges two credits, which is the whole reason the header exists.
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
        if not _fits_in_a_header(self._api_key):
            # Says what is wrong without quoting the key: this message ends up in a log.
            raise ValueError(
                "BeaconBox: the API key contains a character that cannot go in an HTTP header "
                "(a control character, a newline or a non-ASCII character). A key read from a "
                "file or an environment variable often keeps a stray newline or a substituted "
                "quote mark; check the value rather than this call."
            )
        self.base_url = _validated_base_url(base_url)
        cleaned = _header_safe(user_agent_suffix or "")
        self.user_agent = f"beaconbox-python/{__version__}" + (f" {cleaned}" if cleaned else "")

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
        # The request id goes in the message as well as on the attribute, because the message is
        # what reaches a log line or a bug report, and it is the one value support can search for.
        where = f"HTTP {status_code}, request {request_id}" if request_id else f"HTTP {status_code}"
        return error_class_for(status_code)(
            f"BeaconBox: {summary} ({where})",
            status_code=status_code,
            error_code=error_code,
            body=body,
            request_id=request_id,
            retry_after=retry_after_seconds(headers),
        )


_DELAY_SECONDS = re.compile(r"[0-9]{1,20}(\.[0-9]+)?")
r"""The delta-seconds grammar this SDK accepts in a ``Retry-After``.

ASCII digits, with an optional fractional part, and nothing else. Narrow on purpose: ``float()``
would also read ``1e3`` as a thousand seconds, ``+5`` as five, and ``inf`` as a wait no policy can
bound. None of those is a ``Retry-After`` a server means to send, and honouring them would let a
malformed header choose how long a caller blocks.

``[0-9]`` rather than ``\d``, which matches every Unicode decimal digit: ``float()`` reads those
too, so ``\d`` would accept an Arabic-Indic five as five seconds.

**The bound on the integer part keeps the result finite.** ``float()`` does not raise on a long
run of digits, it returns ``inf``, and an ``inf`` reaching
:attr:`~beaconbox.errors.APIError.retry_after` is a number a caller may hand to ``sleep()``. Twenty
digits is already longer than any wait a server means to ask for.

The *fraction* is deliberately unbounded. No number of digits after the point can overflow a float,
so a limit there could only reject a precise value a server legitimately sent.
"""


def retry_after_seconds(headers: Mapping[str, str]) -> float | None:
    """``Retry-After`` in seconds, when the server sent a sane one.

    Only the delta-seconds form is honoured. The HTTP-date form is legal and essentially never
    used by an API, and parsing it would mean trusting the caller's clock to agree with the
    server's, which is the assumption that makes it worse than the SDK's own backoff.

    A fractional value counts: a server that answers ``Retry-After: 1.5`` has said something more
    precise than ``2``, and rounding it up is the SDK inventing a wait the server did not ask for.
    """
    value = _header(headers, "retry-after")
    if value is None:
        return None
    if not _DELAY_SECONDS.fullmatch(value.strip()):
        return None
    return float(value.strip())


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

    A **path** is allowed, for a deployment behind a gateway that mounts BeaconBox under a prefix.
    A **query string or fragment** is not: :meth:`Core.prepare` appends ``/api/v1/...`` and then
    its own ``?``, so ``https://host?x=1`` would produce ``https://host?x=1/api/v1/credits``, a URL
    where the whole API path has become part of a query value. Such a request does not fail loudly,
    it goes somewhere else, which is the worst shape a configuration mistake can take.
    """
    cleaned = base_url.strip().rstrip("/")
    parts = urlsplit(cleaned)
    if parts.scheme not in {"http", "https"}:
        raise ValueError(f"BeaconBox: base_url must be http or https, got {base_url!r}")
    if not parts.netloc:
        raise ValueError(f"BeaconBox: base_url must include a host, got {base_url!r}")
    # The delimiters themselves, not `parts.query`/`parts.fragment`. Those are the empty string for
    # a bare trailing `?` or `#`, which is falsy, and that URL is broken in exactly the same way:
    # `https://host?` yields `https://host?/api/v1/credits`, with the whole API path inside a query
    # value. A literal `?` or `#` has no legitimate place in a base URL, encoded or not.
    if "?" in cleaned or "#" in cleaned:
        raise ValueError(
            f"BeaconBox: base_url must not carry a query string or fragment, got {base_url!r}. "
            "A path prefix is fine (https://gateway.example.com/beaconbox)."
        )
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


def erasable_address(email: str) -> str:
    """The address an erasure is for, trimmed, or a ``ValueError`` before anything is sent.

    The API answers 422 for anything that is not an email address, so this costs nothing it
    would not refuse anyway. It is here because an erasure is the one call where a slip should
    fail as early and as plainly as possible: a blank or mangled value from a form or a CSV should
    say so at the line that passed it, not as a validation error from the server.

    Deliberately loose (something, ``@``, a dotted domain, no whitespace) so that no address the
    server would accept is refused here. The server's check stays the authority.
    """
    cleaned = email.strip() if isinstance(email, str) else ""
    local, at, domain = cleaned.rpartition("@")
    if (
        not at
        or not local
        or "." not in domain.strip(".")
        or any(character.isspace() for character in cleaned)
    ):
        # The value is not quoted: it is whatever the caller had instead of an address (a name,
        # a phone number), and this message ends up in a log.
        problem = "is not an email address" if cleaned else "is blank"
        raise ValueError(
            f"BeaconBox: erasing a recipient needs their email address, and the value given "
            f"{problem}. Nothing was sent."
        )
    return cleaned
