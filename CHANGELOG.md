# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.0] — 2026-09-29

### Security

- `webhooks.verify()` raises `WebhookVerificationError` for a signature containing a non-ASCII
  character, rather than letting a `TypeError` escape. `hmac.compare_digest` refuses to compare
  `str` values that are not ASCII, and the `v1=` value is written by whoever POSTs to the endpoint,
  so `v1=é` reached the documented `except WebhookVerificationError` handler as an unhandled
  exception: an unauthenticated caller could turn the intended 400 into a 500 and a stack trace.
  The comparison is now over the decoded bytes, and a `v1=` that is not hex simply does not match.
- An API key containing a control character, a newline or a non-ASCII character is refused at
  construction. Surrounding whitespace is still stripped, because a key read from a file keeps its
  trailing newline; what is refused is a `\r\n` *inside* the value, which would reach the
  `Authorization` header. The message names the problem without quoting the key.
- A `base_url` carrying a query string or a fragment is refused. It did not fail, it went
  somewhere else: `https://host?x=1` silently produced `https://host?x=1/api/v1/credits`, with the
  whole API path swallowed into a query value. The check is on the `?` and `#` themselves rather
  than on `urlsplit`'s components, because those are the empty string for a bare trailing delimiter
  — which is falsy, and breaks the URL identically. A path prefix is still allowed, for a gateway
  that mounts BeaconBox under one.

### Fixed

- `user_agent_suffix` is made header-safe instead of reaching the header verbatim. httpx keeps the
  value as given and h11 then refuses to serialise the request, raising `httpx.LocalProtocolError` —
  an `httpx.HTTPError`, which the transport catches and *retries*. So a `\r\n` in a caller's own
  configuration spent the whole retry budget and was then reported as "the request did not complete:
  retry with the same idempotency key". Sanitised rather than refused, because the suffix is a label
  a human reads, and byte-identical to the PHP SDK's handling of the same input.
- `keys.create()` returns `id`, so the key you just minted can be revoked in one call. The only
  other route to it was to list the keys and match on `name` — and a name carries no unique
  constraint and defaults to `"Untitled key"`, so two unnamed keys are indistinguishable and the
  match resolves to whichever the ordering happens to put first. That is a wrong-key revocation on
  the one path that exists to answer a leak. Needs the matching API change; `id` is in the `repr`,
  since it is the handle for revoking and not the secret.
- Response parsing no longer raises on an explicit JSON `null` in a field the API declares
  required. `int(None)` raised `TypeError` — so a null `credits` crashed a push that had already
  been charged for — and `str(None)` produced the four-character string `"None"`, so
  `message.id` could come back as `"None"` and be passed to the next call. Both now fall back to a
  zero value, which is what this module's stated tolerance always promised.
- An integral `float` in a numeric field is read as the integer. JSON has one number type, so a
  hop that re-serialises `1` as `1.0` no longer reports zero credits. A `bool` is not read as a
  number: `True` being an `int` is a fact about Python, not about the API.
- An optional string field that arrives wrongly typed reads as absent rather than being handed
  back as an `int` a caller would compare against `SkipReason` and silently never match.
- A boolean field is read strictly rather than through Python's truthiness. `"queued": "yes"` from
  a rewriting proxy read as `True` here while the PHP SDK, whose parser tests `is_bool`, read the
  same payload as `False` — so two workers in two languages disagreed about whether a message had
  been sent. Both now agree, on the stricter reading.
- A non-string entry in `event_types` is dropped rather than stringified.
- `enums.coerce` returns only an enum member or a `str`, which is what every field built through it
  is annotated as. It returned its argument unchanged, so a JSON `null` in an enum field produced
  `None` and a number produced an `int` — values the `RecipientStatus | str` annotation said could
  not be there, handed to a caller who would compare them against a member and never match. A
  non-string now reads as `""`, matching the PHP SDK, and the declared return type is what holds it
  there: `-> Any` is why mypy could not.
- `messages.iterate()` refuses a `page_size` below 1 on both clients. The server clamps `limit`
  into its own range, so this was not an error there — it quietly became one message per request.

### Changed

- The repeated-cursor guard shared by the sync and async `iterate()` is one private object,
  `_PageWalk`, rather than the same check and the same sentence written out twice. Its method is
  `advance()` rather than `next()`, which on an object driven by a `while` loop invited a reader to
  assume it was an iterator.
- `messages.push()` documents `sms_if_whatsapp_fails`, which was the only parameter its docstring
  had omitted, and its `:param:` order now matches the signature.

### Tests

- Three properties the prose claimed and nothing checked: every `:param:` names a real parameter,
  `push` documents its parameters in signature order, and **the async client's signatures match the
  sync client's** — the package docstring says "identical signatures" and the async module says
  "The signatures match exactly". Each was confirmed to fail against a deliberate mutation before
  being kept.

### Note

- `NewApiKey` gained `id` as its **first** field. It is a response model built by `from_api`, so this
  affects only code constructing one positionally by hand.

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

[0.3.0]: https://github.com/beaconbox-eu/beaconbox-python/releases/tag/v0.3.0
[0.2.0]: https://github.com/beaconbox-eu/beaconbox-python/releases/tag/v0.2.0
[0.1.0]: https://github.com/beaconbox-eu/beaconbox-python/releases/tag/v0.1.0
