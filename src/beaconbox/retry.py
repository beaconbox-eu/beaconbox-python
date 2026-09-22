"""When to try again, and how long to wait.

**Retrying a write is only safe because every write carries an ``Idempotency-Key``**, and the
transport reuses *the same key* across attempts. Without that pairing this module would be a
duplicate-message generator: the one failure it exists to survive, a connection that died with
the answer in flight, is exactly the failure where the server may already have stored the
message and charged the credit.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

__all__ = ["RetryPolicy"]

RETRY_AFTER_SPREAD = 0.2
"""How much jitter to add on top of a server-directed wait, as a fraction of it.

See :meth:`RetryPolicy.delay`. Added *on top* rather than sampled from zero, because
``Retry-After`` is a floor the server set and waiting less than it is guaranteed to 429 again.
"""


@dataclass(frozen=True)
class RetryPolicy:
    """How many times to retry, and how long to back off.

    ``RetryPolicy(max_retries=0)`` disables retries entirely, which is the right setting inside a
    job runner that already owns its own retry schedule and would otherwise multiply the two — and
    the right setting for a caller who cannot afford ``max_retry_after`` seconds of blocking.
    """

    max_retries: int = 2
    base_delay: float = 0.2
    max_delay: float = 2.0

    max_retry_after: float = 30.0
    """The longest server-directed wait this policy will sit through.

    Deliberately much larger than ``max_delay``, because the two answer different questions.
    ``max_delay`` caps a delay *the SDK invented* and should stay small; this caps one **the server
    asked for**, and capping that at two seconds means ignoring the only accurate information
    anybody has about when the service will be ready.

    Beyond this the policy stops retrying rather than retrying early — see
    :meth:`should_retry`. Lower it (or set ``max_retries=0``) if a blocked thread is worse for you
    than a failed call; the total a single call can sleep is roughly ``max_retries`` times this.
    """

    def should_retry(
        self, status_code: int | None, attempt: int, retry_after: float | None = None
    ) -> bool:
        """Retry a connection failure, a 429 and a 5xx, and nothing else.

        A 4xx is the server saying the request itself is wrong. Sending it again unchanged asks
        the same question and spends the caller's time on the same answer. 409 is excluded even
        though it can mean "still in flight", for the reason given on
        :class:`~beaconbox.errors.ConflictError`.

        ``status_code=None`` means no response arrived at all.

        **A ``Retry-After`` longer than ``max_retry_after`` stops the retry rather than shortening
        it.** Retrying before the moment the server named is not a compromise, it is a request
        guaranteed to be refused: the caller waits ``max_retry_after`` seconds, fails anyway, and
        the rate-limited service absorbs another pointless call on the way. Failing immediately
        hands them :class:`~beaconbox.errors.RateLimitError` with ``retry_after`` on it, which is
        the number they need to schedule a real retry.
        """
        if attempt >= self.max_retries:
            return False
        if status_code is None:
            return True
        if not (status_code == 429 or status_code >= 500):
            return False
        return retry_after is None or retry_after <= self.max_retry_after

    def delay(self, attempt: int, retry_after: float | None = None) -> float:
        """Seconds to wait before attempt ``attempt + 1``.

        Exponential backoff with full jitter. Jittered because the failure this guards against is
        synchronised: a provider blip makes every one of a merchant's workers retry at once, and
        unjittered backoff makes them do it in lockstep, arriving together precisely when the
        service is least able to answer.

        **A ``Retry-After`` is honoured in full, and jittered on top of itself rather than within
        itself.** The herd problem is at its worst here and not at its mildest: every worker that
        hit the same 429 was handed the *same number*, so obeying it exactly reconstructs the
        lockstep this class exists to break — but sampling from zero would wait less than the
        server asked for, which only earns another 429. So the wait is the directive plus up to
        ``RETRY_AFTER_SPREAD`` of it, bounded by ``max_delay`` so the spread stays a spread.
        """
        if retry_after is not None and retry_after > 0:
            honoured = min(retry_after, self.max_retry_after)
            spread = min(self.max_delay, honoured * RETRY_AFTER_SPREAD)
            return honoured + random.uniform(0, spread)
        ceiling = min(self.max_delay, self.base_delay * (2**attempt))
        return random.uniform(0, ceiling)
