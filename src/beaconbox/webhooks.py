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

    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise WebhookVerificationError("BeaconBox: webhook signature does not match.")

    return _decode(raw)


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
    try:
        timestamp = int(parts["t"])
    except (KeyError, ValueError):
        timestamp = 0
    if timestamp <= 0 or not signature:
        raise WebhookVerificationError(
            f"BeaconBox: malformed {SIGNATURE_HEADER} header (expected 't=<unix>,v1=<hex>')."
        )
    return timestamp, signature


def _decode(raw: bytes) -> WebhookEvent:
    """Only ever called on bytes already proven to be ours."""
    import json

    try:
        document = json.loads(raw)
    except ValueError as exc:
        raise WebhookVerificationError("BeaconBox: webhook body is not JSON.") from exc
    if not isinstance(document, dict):
        raise WebhookVerificationError("BeaconBox: webhook body is not a JSON object.")
    return WebhookEvent.from_api(document)
