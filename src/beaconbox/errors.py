"""Everything this SDK raises.

One root, :class:`BeaconBoxError`, so an application that only wants "did the BeaconBox call
work" can catch a single type. Below it the split that matters operationally is not by status
code but by *what you know afterwards*:

* :class:`APIError` and its subclasses mean the server answered. Whatever it says happened,
  happened.
* :class:`APIConnectionError` means no answer arrived. **That is not proof the work was not
  done.** The request may have been received, the message stored and the SMS charged, with only
  the response lost. This is the failure the idempotency key exists for, and it is why retrying
  a BeaconBox write is safe only when the same key is reused (see :mod:`beaconbox.retry`).
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "APIConnectionError",
    "APIError",
    "AuthenticationError",
    "BeaconBoxError",
    "ConflictError",
    "InvalidRequestError",
    "PermissionDeniedError",
    "RateLimitError",
    "ResourceMissingError",
    "ServerError",
    "WebhookVerificationError",
]


class BeaconBoxError(Exception):
    """Base class for every error this SDK raises."""


class APIConnectionError(BeaconBoxError):
    """The request did not complete: a DNS failure, a refused connection, a timeout.

    **Not proof the work did not happen.** The server may have done everything and only the
    answer was lost. The SDK has already retried this (with the same idempotency key, which is
    what makes that safe) before it reaches you.
    """


class WebhookVerificationError(BeaconBoxError):
    """A webhook delivery could not be proven to have come from BeaconBox.

    Deliberately not an :class:`APIError`: nothing was requested and no status code exists. Treat
    it as a 400 back to the caller and do not process the payload.
    """


class APIError(BeaconBoxError):
    """The server answered with a status of 400 or above.

    ``error_code`` is the machine-readable dotted code from the response body (for example
    ``message.not_found``). Branch on it rather than on the message text, which is written for a
    human and may be reworded.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        error_code: str | None = None,
        body: dict[str, Any] | None = None,
        request_id: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.body: dict[str, Any] = body or {}
        self.request_id = request_id

        self.retry_after = retry_after
        """Seconds the server asked you to wait, from its ``Retry-After`` header, or ``None``.

        Set on whatever status carried the header — usually a 429, sometimes a 503. It survives
        onto the exception because the SDK may deliberately *not* have waited it out: see
        :meth:`~beaconbox.retry.RetryPolicy.should_retry`.
        """


class AuthenticationError(APIError):
    """401. The key is missing, malformed or revoked."""


class PermissionDeniedError(APIError):
    """403. The key is valid but not allowed to do this."""


class ResourceMissingError(APIError):
    """404. No such id.

    Also what *another business's* id looks like from here, deliberately: a probe must not be
    able to tell "does not exist" from "exists and is not yours".
    """


class ConflictError(APIError):
    """409. Already sent, or an identical request is still in flight.

    Not retried by the SDK. "Your earlier attempt is still running" is a truthful answer to give
    a caller, and a client that quietly blocked for a second instead would be hiding it.
    """


class InvalidRequestError(APIError):
    """422 (and any other 4xx). A malformed field, or an idempotency key reused with a
    different body."""


class RateLimitError(APIError):
    """429, after the SDK has already retried and backed off.

    **Check ``retry_after``.** A wait longer than the policy's ``max_retry_after`` is reported
    here rather than slept through, so this can arrive seconds after the call started with the
    server's own answer to "when should I come back" sitting on it.
    """


class ServerError(APIError):
    """5xx, after the SDK has already retried."""


_BY_STATUS: dict[int, type[APIError]] = {
    401: AuthenticationError,
    403: PermissionDeniedError,
    404: ResourceMissingError,
    409: ConflictError,
    429: RateLimitError,
}


def error_class_for(status_code: int) -> type[APIError]:
    """Map an HTTP status onto the exception this SDK raises for it."""
    if status_code in _BY_STATUS:
        return _BY_STATUS[status_code]
    if status_code >= 500:
        return ServerError
    return InvalidRequestError
