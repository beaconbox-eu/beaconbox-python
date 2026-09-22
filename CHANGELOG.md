# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] — 2026-09-22

### Added

- `delivery.not_sent` on a message: why no email was ever sent, and when we decided. Present only
  when that decision stands — a later successful send withdraws it. It is what tells a
  `delivered` of `False` apart from *never attempted, and never will be*, so a loop polling for
  delivery now has something to stop on. `EmailSkipReason` lists the known values; `reason` stays a plain string,
  because the server's list grows and an unknown value must not raise.
- `message.not_sent` webhook event, carrying the same `reason`. Deliberately not folded into
  `message.failed`: nothing was attempted and nothing bounced.
- `plan.past_due`, `plan.lapsed` and `plan.allowance_exceeded` webhook events. The second is the
  one to alert on — it means email sending has stopped, and every nudge from then on produces a
  `message.not_sent` with `reason: "plan_lapsed"` until the subscription is paid.

### Fixed

- `MessagePush` and the two types above are listed in their own modules' `__all__`. `MessagePush`
  had been missing since 0.1.0: it was re-exported from `beaconbox` and documented, but
  `from beaconbox.models import *` silently omitted it. A test now pins every public type into
  its module's `__all__` and into the package root, so this cannot recur — nothing had been
  checking, which is why it went unnoticed through a release.

### Note

- `DeliveryStatus` gained a field before `raw`. It is a response model built by `from_api`, so
  this affects only code constructing one positionally by hand.

## [0.1.0]

### Added

- `BeaconBox` and `AsyncBeaconBox` clients, with the same 18 operations and identical signatures.
- Typed models for every response, with `py.typed`. Unknown fields remain available via `.raw`.
- Cursor pagination via `messages.iterate()`, and `async for` on the async client.
- Webhook signature verification via `beaconbox.webhooks.verify()`.
- Automatic `Idempotency-Key` on every write, reused across the SDK's own retries. Override per
  call with `idempotency_key=`.
- Retries with exponential backoff and jitter on connection errors, 429 and 5xx. Configure with
  `RetryPolicy`; `max_retries=0` disables them.
- `Retry-After` honoured up to `RetryPolicy.max_retry_after` (30s). Beyond it, `RateLimitError` is
  raised with `retry_after` set rather than retrying.
- Skipped SMS and WhatsApp channels are reported on the result; they do not raise.
- Configurable `base_url`, `timeout`, `retry_policy`, `verify`, `max_connections` and
  `user_agent_suffix`. An `httpx` client can be injected via `http_client=` instead.
- Opt-in logging on the `beaconbox` logger. Silent by default; the SDK configures nothing.
- Plain `http` is refused except on localhost.
- Redirects are not followed.
- API keys, newly minted keys and webhook endpoint secrets are excluded from `repr()`.
- Log records carry route templates (`/recipients/{email}/sms`), never interpolated paths.

### Requirements

- Python 3.10+
- [httpx](https://www.python-httpx.org) `>=0.28.1,<1`

[0.2.0]: https://github.com/beaconbox-eu/beaconbox-python/releases/tag/v0.2.0
[0.1.0]: https://github.com/beaconbox-eu/beaconbox-python/releases/tag/v0.1.0
