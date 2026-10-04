# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.4.0] - 2026-10-04

### Added

- `MessagePushResult.email`, an `EmailOutcome` carrying whether the nudge is expected to send and
  why not if it is not. Its own type beside `sms` and `whatsapp`, because suppression is per
  channel: a recipient who texted STOP has suppressed SMS and nothing else, a hard bounce
  suppresses email and nothing else, and one merged flag could not say which. `sending=False` is
  reliable; `sending=True` is the verdict as known at push time and cannot see a daily cap filling
  up before the send, so `Message.delivery` and the `message.not_sent` webhook remain
  authoritative.
- `webhook_endpoints.test(public_id)`. Send a sample `ping` to one of your endpoints and read what
  came back as a `WebhookTestResult`. It never raises for a failure at *your* end, because that
  failure belongs to your server rather than to the API call: read `delivered`. The event type is
  `ping` rather than a real one, so a test cannot be mistaken for a genuine delivery by a handler
  that acts on them, and it is signed exactly like a real delivery, so it also proves your
  signature verification. It does not touch the endpoint's health counters.

### Security

- `webhooks.verify()` raises `WebhookVerificationError` for a signature header whose `t=` is longer
  than twenty digits, rather than letting a `ValueError` escape. `int()` refuses a string of more
  than `sys.get_int_max_str_digits()` digits (4300 by default), and the header is written by
  whoever POSTs to your endpoint, so a long run of digits reached the documented
  `except WebhookVerificationError` handler as an unhandled exception: an unauthenticated caller
  could turn the intended 400 into a 500 and a stack trace.

### Fixed

- `WebhookTestResult.status_code` reads an unusable value as `None` instead of raising or
  inventing a status. A JSON body may legitimately carry `NaN` or `Infinity`, and converting
  either to an integer raises, so a malformed response could escape the parse layer as a
  `ValueError` or `OverflowError` rather than being tolerated the way every other field is. A
  boolean no longer reads as `1`, and a non-integral float is no longer truncated into a
  plausible-looking status.
- `Retry-After` is read as delta-seconds and nothing else. `float()` also accepts `1e3`, `+5`,
  `.5`, `inf` and non-ASCII digits, so a malformed or hostile header could choose how long a
  caller blocked, up to a wait no policy could bound. A fractional value such as `1.5` is still
  honoured exactly.
- `webhooks.verify()` requires the signature header's `t=` to be ASCII digits. `int()` also
  accepts a leading sign, surrounding whitespace, underscore separators and other scripts'
  digits, each of which was a second spelling of a timestamp that has one canonical form and is
  part of the signed string.
- A number outside signed 64-bit range reads as absent rather than as a value. Python's integers
  are unbounded, so `{"credits": 1e300}` read as a 300-digit number and travelled on into a
  caller's own arithmetic, formatting and database. No count this API reports can exceed an
  `int64`, so one that does is a corrupt response rather than a large answer.
- `retry_after` on a `RateLimitError` is always a finite number. `float()` does not raise on a long
  run of digits, it returns `inf`, and an `inf` here is a value a caller may hand to `sleep()`.

### Changed

- `WebhookVerificationError` for a signature mismatch now names the likely cause. "Signature does
  not match" reads as a key problem and almost never is: the usual cause is verifying a
  re-serialized body instead of the raw request bytes, which looks identical and hashes
  differently. The message says so before it mentions the secret, so nobody rotates a working one.

## [0.3.0] - 2026-09-29

### Security

- `webhooks.verify()` raises `WebhookVerificationError` for a signature containing a non-ASCII
  character, rather than letting a `TypeError` escape. The `v1=` value is written by whoever POSTs
  to your endpoint, and `hmac.compare_digest` refuses to compare non-ASCII `str` values, so such a
  signature reached the documented `except WebhookVerificationError` handler as an unhandled
  exception. The comparison is now over the decoded bytes, and a `v1=` that is not hex simply does
  not match.
- An API key containing a control character, a newline or a non-ASCII character is refused at
  construction, since it would otherwise reach the `Authorization` header verbatim. Surrounding
  whitespace is still stripped, because a key read from a file keeps its trailing newline. The
  message names the problem without quoting the key.
- A `base_url` carrying a query string or a fragment is refused. Such a URL does not fail, it goes
  somewhere else: `https://host?x=1` produced `https://host?x=1/api/v1/credits`, with the whole
  API path swallowed into a query value. A path prefix is still allowed, for a gateway that mounts
  BeaconBox under one.

### Fixed

- `user_agent_suffix` is made header-safe rather than reaching the header verbatim, where a stray
  newline became a transport error that spent the whole retry budget before reporting itself as a
  request that did not complete. Sanitised rather than refused, because the suffix is a label a
  human reads.
- `keys.create()` returns `id`, so the key you just minted can be revoked in one call. The only
  other route to it was to list the keys and match on `name`, which carries no unique constraint
  and defaults to `"Untitled key"`, so two unnamed keys cannot be told apart. `id` is in the
  `repr`, since it is the handle for revoking and not the secret.
- Response parsing no longer raises on an explicit JSON `null` in a field the API declares
  required. A null numeric field raised `TypeError`, so a null `credits` crashed a push that had
  already been charged for, and a null string field produced the four-character string `"None"`,
  so `message.id` could be passed on as `"None"`. Both now fall back to a zero value, which is
  what this module's stated tolerance always promised.
- An integral `float` in a numeric field is read as the integer. JSON has one number type, so a
  hop that re-serialises `1` as `1.0` no longer reports zero credits. A `bool` is not read as a
  number: `True` being an `int` is a fact about Python, not about the API.
- An optional string field that arrives wrongly typed reads as absent, rather than being handed
  back as an `int` a caller would compare against `SkipReason` and silently never match.
- A boolean field is read strictly rather than through Python's truthiness, so `"queued": "yes"`
  from a rewriting proxy no longer reads as `True`.
- A non-string entry in `event_types` is dropped rather than stringified.
- `enums.coerce` returns only an enum member or a `str`, which is what every field built through
  it is annotated as. A non-string now reads as `""`.
- `messages.iterate()` refuses a `page_size` below 1 on both clients. The server clamps `limit`
  into its own range, so this was not an error there; it quietly became one message per request.

### Changed

- The repeated-cursor guard shared by the sync and async `iterate()` is one private object rather
  than the same check written out twice.
- `messages.push()` documents `sms_if_whatsapp_fails`, which was the only parameter its docstring
  had omitted, and its `:param:` order now matches the signature.

### Note

- `NewApiKey` gained `id` as its **first** field. It is a response model built by `from_api`, so
  this affects only code constructing one positionally by hand.

## [0.2.0] - 2026-09-22

### Added

- `delivery.not_sent` on a message: why no email was ever sent, and when the decision was made.
  Present only while that decision stands, since a later successful send withdraws it. It is what
  tells a `delivered` of `False` apart from *never attempted, and never will be*, so a loop
  polling for delivery now has something to stop on. `EmailSkipReason` lists the known values;
  `reason` stays a plain string, because the server's list grows and an unknown value must not
  raise.
- `message.not_sent` webhook event, carrying the same `reason`. Deliberately not folded into
  `message.failed`: nothing was attempted and nothing bounced.
- `plan.past_due`, `plan.lapsed` and `plan.allowance_exceeded` webhook events. The second is the
  one to alert on. It means email sending has stopped, and every nudge from then on produces a
  `message.not_sent` with `reason: "plan_lapsed"` until the subscription is paid.

### Fixed

- `MessagePush` and the two types above are listed in their own modules' `__all__`. `MessagePush`
  was re-exported from `beaconbox` and documented, but `from beaconbox.models import *` omitted
  it. A test now pins every public type into its module's `__all__` and into the package root.

### Note

- `DeliveryStatus` gained a field before `raw`. It is a response model built by `from_api`, so
  this affects only code constructing one positionally by hand.

## [0.1.0]

### Added

- `BeaconBox` and `AsyncBeaconBox` clients, with the same operations and identical signatures.
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

[0.4.0]: https://github.com/beaconbox-eu/beaconbox-python/releases/tag/v0.4.0
[0.3.0]: https://github.com/beaconbox-eu/beaconbox-python/releases/tag/v0.3.0
[0.2.0]: https://github.com/beaconbox-eu/beaconbox-python/releases/tag/v0.2.0
[0.1.0]: https://github.com/beaconbox-eu/beaconbox-python/releases/tag/v0.1.0
