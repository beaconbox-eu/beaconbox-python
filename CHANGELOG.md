# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.5.0] - 2026-10-07

### Added

- `order_status` on `messages.push()` (both clients) and on `MessagePush`, with `OrderStatus`
  listing the known values (`confirmed`, `shipped`, `ready_for_pickup`, `delivered`, `cancelled`,
  `refunded`, `delayed`). It says what happened to the order, and BeaconBox picks the WhatsApp and
  SMS wording from it. A WhatsApp nudge is sent only when a push carries both `reference` and
  `order_status`; otherwise WhatsApp is skipped with `template_not_sendable` and nothing is
  charged. A plain string is accepted too, so a status the API adds later can be sent before an
  SDK release. `MessagePushResult.order_status` echoes it back, and an unknown value parses as a
  plain string.
- Error codes `sms.may_have_sent` and `whatsapp.may_have_sent` (409): a resend is refused while an
  earlier send of that message ended without the provider's answer, because it may have arrived.
  A later receipt that settles it lifts the refusal. Send records also gain the outcome codes
  `sms.never_submitted` and `whatsapp.never_submitted`: the send never reached the provider and
  its credits were returned.
- `keys.create(..., detach=True)` on both clients: the API rotation path. A key minted with a key
  is now revoked when that key is revoked (and so is anything minted below it without `detach`). A
  detached key has no parent and keeps working after you revoke the key that minted it: mint it,
  move to it, then revoke the old one. A leaked key can mint one too, so after a leak check
  `minted_by` (below) for keys the leaked key created and revoke them as well.
- `ApiKey.minted_by`: the id of the key that created a listed key through the API, set even for a
  detached one, and `None` for a key created in the dashboard. After a leak, a key still listed
  with the leaked key's id there was created detached with it and keeps working until revoked.
- `recipients.erase(email)` on both clients, returning a `RecipientErasureReport`. The API half of
  an Article 17 request: it deletes the messages you sent the recipient, their delivery events,
  their SMS and WhatsApp send records, their WhatsApp replies and their recipient record, scrubs
  them from webhook deliveries still queued for you and cancels sends still waiting to go out. It
  keeps email suppressions and SMS or WhatsApp opt-outs on purpose, because those are what stop
  the person being contacted again, and the report counts both (`suppressions_kept`,
  `stops_kept`). Not undoable. It is a `POST` and carries an `Idempotency-Key` like every other:
  a repeat erasure reports zeros, so retrying with the same key is how a timed-out call gets its
  original counts back.
- `delivery.failed` on a message, a `Failed` with `reason` and `at`: the email was attempted and
  will never arrive, because the provider refused it or every retry failed. The counterpart of
  `delivery.not_sent`, and like it a reason to stop polling for delivery. `EmailFailureReason`
  lists the known values (`esp_refused`, `retries_exhausted`); `reason` stays a plain string so a
  new value cannot raise.
- `SkipReason.SMS_NOT_ENABLED`, `SkipReason.SMS_WARMUP_CAP_REACHED` and
  `SkipReason.WHATSAPP_NOT_ENABLED`, and `EmailSkipReason.BUSINESS_INACTIVE`. Unknown reasons still
  parse as plain strings.
- The response's `X-Request-Id` is logged as `request_id` on the debug response record and on
  the retry warning, and is now part of every `APIError` message as well as its `request_id`
  attribute. Quote it to support.
- `MessagePushResult.recipient_email` (so also on each batch item's `result`): the address the
  message was stored for, as the API normalised it. `None` only on an idempotent replay of a
  response recorded before the API sent it.
- `RecipientSms.sms_consent_at` and `RecipientSms.sms_consent_source` (a `SmsConsentSource`:
  `push`, `api`, `admin` or `inbox`): when and through which surface texting this recipient first
  began. An unknown source parses as a plain string.
- `EmailFailureReason.OUTCOME_UNKNOWN` (`outcome_unknown`): the email provider may have taken the
  message and its answer was lost. Reported as failed and never resent, but it may still arrive.
- Documented error codes: `request.too_large` (413, a body over 2 MiB or 8 MiB for a batch) and
  `request.unstorable_input` (422, a NUL character or lone surrogate in text), both raised as
  `InvalidRequestError`; `plan.read_only` (403) on `keys.create()` while the plan has lapsed, as
  `PermissionDeniedError`. The `sms.failed` and `whatsapp.failed` webhooks document
  `sms.submit_outcome_unknown` and `whatsapp.submit_outcome_unknown` in `data["error_code"]`.

### Changed

- **WhatsApp now needs `reference` and `order_status` on the push.** The generic WhatsApp templates
  are retired, because Meta classed them as marketing; each order status now has its own template. A
  push without both, or whose reference or business name a nudge cannot show, skips WhatsApp with
  `template_not_sendable` and is not charged. With `sms_if_whatsapp_fails`, the text goes instead,
  once, as it does for a number not on WhatsApp.
- **The WhatsApp and SMS nudges show `reference` only when it passes a display rule**: an optional
  leading `#`, then a letter or digit, then only letters, digits and `_ / # -`, with at most 32
  characters and at most two dots, each directly before a digit (`INV.2026` is shown, `shop.com`
  is not). A reference the API accepts that fails the rule is stored and echoed but never shown,
  and such a push sends no WhatsApp message.
- `WebhookEventType.PLAN_ALLOWANCE_EXCEEDED` is documented as fair use: sending continues and
  email past the allowance is not charged. Its payload no longer carries `overage_cents`.
- `whatsapp.rejected` is documented as also firing, with `error_code` `whatsapp.submit_refused`
  and the credit refunded, for a send Meta refused on every attempt. The API used to send
  `whatsapp.failed` for it; it now matches `sms.rejected`.
- `recipients.erase()` and `recipients.erase_whatsapp()` raise `ValueError` before sending
  anything when the email is blank or not an address, which the API now refuses with a 422. The
  value is trimmed before it is sent.
- `escalate_if_unread_after_minutes` is documented as the API now behaves: it counts from
  `send_at` when the message is scheduled, waits while the email is still queued, and still
  escalates when the email bounced, failed or was skipped. Only a push that queued no nudge never
  escalates, and an `on_request` channel the push named still sends at the deadline.
- `channels` on a push is documented as what the API now enforces: a request that can only
  narrow what your account settings allow, never switch a channel on. A channel in `off` mode
  stays off (`sms_disabled`, `whatsapp_disabled`), one BeaconBox has not enabled for your business
  never sends (`sms_not_enabled`, `whatsapp_not_enabled`), and one in `on_request` mode sends only
  on a push that names it. A push still answers 201 and reports the skip; nothing raises.
- `EmailSkipReason.DAILY_CAP_REACHED` is documented as requiring a resend: the nudge is not retried
  when the cap resets.
- `WebhookEventType.MESSAGE_FAILED` documents its payload: `message_id`, `recipient_email`,
  `subject`, `channel`, and `reason` when BeaconBox gave up on the send.
- Docstrings and the README state the API's push limits: `obsoletes` takes at most 100 ids of at
  most 64 characters, and a batch `idempotency_key` at most 252 characters (each item's key
  appends `#<index>`). Both are refused with `InvalidRequestError`. The SDK does not check them
  itself.
- The resend docstrings name the 409 codes (`sms.already_sent`, `whatsapp.already_sent`,
  `whatsapp.escalated_to_sms`), all raised as `ConflictError`, and the README lists the other new
  codes a public call can answer: `request.conflict` (409) and `webhook.test_rate_limited` (429).
- `messages.push_batch()` documents its one whole-batch refusal: a 409
  `idempotency.request_in_progress` while the same key is still running (or took the batch over),
  raised as `ConflictError` and not retried by the SDK. Retry the whole batch later with the same
  key.
- `recipients.erase()` documents that an email unsubscribe is kept as a suppression, so erasing
  does not undo it.
- `EmailFailureReason.OUTCOME_UNKNOWN` and `delivery.failed` document that a later delivery clears
  `failed` and shows as delivered, whatever its timestamp, so it is not a reason to stop polling.
- `recipients.set_phone()` and a push's `recipient_phone` document `sms.phone_invalid` (422) for
  premium-rate, toll-free, shared-cost, voicemail, service, satellite and international numbers; one
  already stored is skipped as `country_not_allowed`.
- The `sms.failed`, `sms.rejected`, `whatsapp.failed` and `whatsapp.rejected` webhooks document that
  they also fire when BeaconBox ends a send itself: refused at submit, outcome unknown, or given up
  on.
- An `APIError` message now ends `(HTTP 409, request <id>)` when the response carried a request
  id, rather than `(HTTP 409)`. Code matching on the message text should match on `error_code`.
- `DeliveryStatus` gained `failed` before `raw`. It is a response model built by `from_api`, so
  this affects only code constructing one positionally by hand.

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

[0.5.0]: https://github.com/beaconbox-eu/beaconbox-python/releases/tag/v0.5.0
[0.4.0]: https://github.com/beaconbox-eu/beaconbox-python/releases/tag/v0.4.0
[0.3.0]: https://github.com/beaconbox-eu/beaconbox-python/releases/tag/v0.3.0
[0.2.0]: https://github.com/beaconbox-eu/beaconbox-python/releases/tag/v0.2.0
[0.1.0]: https://github.com/beaconbox-eu/beaconbox-python/releases/tag/v0.1.0
