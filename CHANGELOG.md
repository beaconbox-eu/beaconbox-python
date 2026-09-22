# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[0.1.0]: https://github.com/beaconbox-eu/beaconbox-python/releases/tag/v0.1.0
