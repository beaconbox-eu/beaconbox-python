"""Verify a webhook BeaconBox sent you.

The header is ``X-BeaconBox-Signature: t=<unix>,v1=<hex>``, an HMAC-SHA256 over
``"<t>.<raw body>"`` with your endpoint's secret. **The timestamp is inside the signed string**,
which is what makes a replay detectable: a captured delivery cannot be re-sent later with a fresh
timestamp, because the signature would no longer match.

Three rules this module enforces so you do not have to remember them:

1. **Verify the raw body, byte for byte.** Decoding to a dict and re-encoding is how a signature
   stops matching over key order or whitespace, and it fails *later*, in production, on a payload
   shaped slightly differently from the one you tested with.
2. **Compare in constant time.** A ``==`` on the hex digest leaks, through timing, how much of a
   guess was right.
3. **Reject a stale timestamp.** Without a freshness window, a delivery captured once is valid
   forever.

What it cannot do for you is deduplicate. A delivery that timed out on your side is retried, so
the same :attr:`~beaconbox.models.WebhookEvent.id` can arrive twice. Treat handling as idempotent
or keep the ids you have seen.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time

from .errors import WebhookVerificationError
from .models import WebhookEvent

__all__ = ["DEFAULT_TOLERANCE_SECONDS", "SIGNATURE_HEADER", "verify"]

SIGNATURE_HEADER = "X-BeaconBox-Signature"

DEFAULT_TOLERANCE_SECONDS = 300
"""How far a delivery's timestamp may be from now. Absorbs clock skew and provider retries."""


def verify(
    payload: bytes | str,
    signature_header: str,
    secret: str,
    *,
    tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
    now: float | None = None,
) -> WebhookEvent:
    """Prove a delivery came from BeaconBox, then decode it.

    :param payload: **The raw request body.** In Flask that is ``request.get_data()``, in FastAPI
        ``await request.body()``, in Django ``request.body``. Not ``request.json``, and not
        anything you have already parsed: see rule 1 in the module docstring.
    :param signature_header: The ``X-BeaconBox-Signature`` header value.
    :param secret: Your endpoint's signing secret, shown once when the endpoint was created. Read
        it from your environment or secret store, never from source.
    :param tolerance_seconds: Freshness window. Widen it only if your clocks are genuinely far
        apart, because every second of it is a second a captured delivery stays replayable.
    :param now: Override the clock, for tests.

    :raises WebhookVerificationError: signature missing, malformed, stale or wrong. Answer 400 and
        do not process the payload.
    :returns: The decoded :class:`~beaconbox.models.WebhookEvent`.

    .. code-block:: python

        @app.post("/hooks/beaconbox")
        async def hook(request: Request):
            try:
                event = webhooks.verify(
                    await request.body(),
                    request.headers["X-BeaconBox-Signature"],
                    os.environ["BEACONBOX_WEBHOOK_SECRET"],
                )
            except WebhookVerificationError:
                raise HTTPException(400)

            if event.type == WebhookEventType.MESSAGE_BOUNCED:
                ...
            return {"ok": True}
    """
    raw = payload.encode() if isinstance(payload, str) else payload
    timestamp, signature = _parse_header(signature_header)

    age = abs((time.time() if now is None else now) - timestamp)
    if age > tolerance_seconds:
        raise WebhookVerificationError(
            f"BeaconBox: webhook timestamp is {int(age)} seconds off, outside the "
            f"{tolerance_seconds}s tolerance."
        )

    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + raw, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, _hex_to_bytes(signature)):
        # **Names the likely cause, because the obvious reading of this error is the wrong one.**
        # "Signature does not match" sounds like a key problem, and the overwhelmingly common cause
        # is not: it is that the bytes checked are not the bytes sent. Any parse-and-re-serialize —
        # a framework handing over ``request.json``, a logging proxy, a copy out of a webhook
        # inspector — reorders keys or re-spaces the JSON and changes the digest completely, while
        # leaving a body that still looks identical when you read it. Someone who reaches for their
        # secret first can lose an afternoon before they think to check the body.
        raise WebhookVerificationError(
            "BeaconBox: webhook signature does not match. The most common cause is verifying "
            "against a re-serialized body rather than the raw request bytes — if you parsed the "
            "JSON first, or copied the body out of a tool that pretty-prints it, verify the "
            "original bytes instead. Failing that, check the secret belongs to this endpoint."
        )

    return _decode(raw)


def _hex_to_bytes(signature: str) -> bytes:
    """The header's ``v1=`` value as raw bytes, for a constant-time compare against the digest.

    **Decoded rather than compared as text**, because the header is written by whoever POSTs to
    your endpoint: ``hmac.compare_digest`` raises ``TypeError`` on a ``str`` carrying any
    non-ASCII character, and a ``v1=é`` that escaped as ``TypeError`` would slip past the
    documented ``except WebhookVerificationError`` and answer 500 instead of 400.

    A value that is not hex cannot be a signature, so this returns bytes that will not match
    rather than raising. The length and shape of a signature are not secret, so short-circuiting
    on them leaks nothing; what must stay constant-time is the comparison of the bytes themselves,
    which is :func:`hmac.compare_digest`'s job above.
    """
    try:
        return bytes.fromhex(signature)
    except ValueError:
        return b""


_UNIX_SECONDS = re.compile(r"[0-9]{1,20}")
"""What the ``t=`` value may be: between one and twenty ASCII digits.

Narrower than ``int()``, which also accepts surrounding whitespace, a leading sign, underscores
as separators and non-ASCII digits from other scripts. The timestamp is part of the signed string,
so every alternative spelling ``int()`` would accept is another way to write a header that has to
be checked against one canonical form.

**The length bound is what makes the ``int()`` below unable to raise, and it is load-bearing.**
``int()`` rejects a string of more than ``sys.get_int_max_str_digits()`` digits (4300 by default)
with a ``ValueError``, and this value comes from a header an unauthenticated caller writes: an
unbounded pattern lets them turn the documented ``except WebhookVerificationError`` into a 500 and
a stack trace. Twenty digits is already far past any Unix timestamp, so widening this gains
nothing and would reopen that hole.
"""


def _parse_header(header: str) -> tuple[int, str]:
    """``t=<unix>,v1=<hex>`` into its parts.

    Tolerant of extra comma-separated pairs so that a future ``v2=`` scheme can be added without
    every deployed verifier rejecting the delivery outright.
    """
    parts: dict[str, str] = {}
    for piece in (header or "").split(","):
        name, sep, value = piece.strip().partition("=")
        if sep:
            parts[name] = value

    signature = parts.get("v1", "")
    raw = parts.get("t", "")
    timestamp = int(raw) if _UNIX_SECONDS.fullmatch(raw) else 0
    if timestamp <= 0 or not signature:
        raise WebhookVerificationError(
            f"BeaconBox: malformed {SIGNATURE_HEADER} header (expected 't=<unix>,v1=<hex>')."
        )
    return timestamp, signature


def _decode(raw: bytes) -> WebhookEvent:
    """Only ever called on bytes already proven to be ours."""
    try:
        document = json.loads(raw)
    except ValueError as exc:
        raise WebhookVerificationError("BeaconBox: webhook body is not JSON.") from exc
    if not isinstance(document, dict):
        raise WebhookVerificationError("BeaconBox: webhook body is not a JSON object.")
    return WebhookEvent.from_api(document)
