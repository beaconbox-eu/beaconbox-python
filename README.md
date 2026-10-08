# beaconbox

Official Python SDK for [BeaconBox](https://beaconbox.eu), order updates your customers actually
receive.

```bash
pip install beaconbox
```

Python 3.10+. One runtime dependency, [httpx](https://www.python-httpx.org), which is also the
only HTTP library where the sync and the async client share an API, so this SDK's logic is
written once rather than twice.

Full documentation, including the API reference and the guides this README summarises, is at
[docs.beaconbox.eu](https://docs.beaconbox.eu/develop).

## Push an update

```python
from beaconbox import BeaconBox, OrderStatus

client = BeaconBox()  # reads BEACONBOX_API_KEY

result = client.messages.push(
    recipient_email="buyer@example.com",
    subject="Your order has shipped",
    body="Tracking XY123456789EE. Estimated delivery Thursday.",
    kind="updateable",
    reference="#A-10294",
    order_status=OrderStatus.SHIPPED,
)

print(result.id)  # 3xg39cvt8a46
```

The customer gets an email whose link opens their inbox already signed in. No password, no
account to create.

Re-push the same `subject` with `kind="updateable"` to overwrite it in place, quietly. Add
`notify=True` to force a nudge on an update, or `obsoletes=[...]` to grey out messages this one
replaces (at most 100 ids, each at most 64 characters).

Pass `reference` (your order number) and `order_status` (an `OrderStatus` such as
`OrderStatus.SHIPPED`, or its string value) and BeaconBox writes the WhatsApp and SMS wording for
you, for example "Your order #A-10294 from PhonicBloom has shipped." A WhatsApp nudge needs both:
missing either, WhatsApp is skipped with `template_not_sendable` and nothing is charged.

A request body may be at most 2 MiB, or 8 MiB for a batch. A larger one is refused with
`InvalidRequestError` (HTTP 413, `request.too_large`) before it is read.

## Async

Same client, same signatures, awaited. Nothing blocks the event loop, including retry backoff.

```python
from beaconbox import AsyncBeaconBox

async with AsyncBeaconBox() as client:
    result = await client.messages.push(
        recipient_email="buyer@example.com",
        subject="Your order has shipped",
        body="Tracking XY123456789EE.",
    )

    async for message in client.messages.iterate(recipient_email="buyer@example.com"):
        print(message.id, message.delivery.opened)
```

## Two things to know before anything else

### 1. Read the response, not the status code

A push returns **201 even when the SMS or WhatsApp message was not sent.** The update is already
in the customer's inbox and the email nudge has gone, so a paid channel that could not send
reports a reason and the request still succeeded.

```python
from beaconbox import SkipReason

result = client.messages.push(
    recipient_email="buyer@example.com",
    recipient_phone="+37255550134",
    subject="Your order has shipped",
    body="Tracking XY123456789EE.",
    reference="#A-10294",
    order_status=OrderStatus.SHIPPED,
    channels=["sms"],
)

if result.sms and result.sms.skipped_reason == SkipReason.INSUFFICIENT_CREDIT:
    # Top up, then: client.messages.resend_sms(result.id)
    ...
elif result.sms and result.sms.skipped_reason in (
    SkipReason.SMS_DISABLED,  # your SMS mode is off
    SkipReason.SMS_NOT_ENABLED,  # BeaconBox has not switched SMS on for you yet
):
    # The email went. Naming "sms" above did not, and cannot, turn SMS on.
    ...
```

**This SDK does not raise on a skip**, deliberately. Treating one as an error is what invites a
retry, and a retry of a push that already succeeded is a second message to a real person.

#### What `channels` means

`channels` is a request that can only **narrow** what your account settings allow. It never
switches a channel on:

- Leave it unset and the push follows your account settings.
- Leave a channel out of the list and this push does not use it (`disabled_by_request`).
- Name a channel whose mode is `off` and nothing is sent on it (`sms_disabled`,
  `whatsapp_disabled`). `off` is final.
- Name a channel BeaconBox has not yet enabled for your business and nothing is sent on it
  (`sms_not_enabled`, `whatsapp_not_enabled`). Ask for it from the dashboard.
- A channel in `on_request` mode sends only on a push that names it.

Naming a channel never overrides a country restriction or a recipient who opted out, and on
WhatsApp never a recipient who did not opt in. A push that does not mention a channel your
account has off, or sends only on request, gets no block for it at all (`result.sms is None`).

The **email** side answers in two halves, because the decision is made twice.

`result.email` is the verdict as known at push time, so the stable, recipient-shaped reasons reach
you in the response to the call you just made:

```python
if result.email and not result.email.sending:
    # Reliable: no email will be sent, and skipped_reason says why.
    print(result.email.skipped_reason)  # plan_lapsed, unsubscribed, suppressed, ...
```

**`sending=False` is reliable; `sending=True` is an expectation, not a promise.** At push time we
can see a suppressed address or a lapsed plan; we cannot see the day's cap filling up between now
and the send, or a pause applied in between. So for what actually happened, read the message back:

```python
from beaconbox import EmailSkipReason

message = client.messages.get(result.id)

if message.delivery.not_sent:
    # No email was ever attempted, and none will be. Stop polling for delivery.
    if message.delivery.not_sent.reason == EmailSkipReason.PLAN_LAPSED:
        ...  # the subscription ran out; paying resumes sending

if message.delivery.failed:
    # An email was attempted and BeaconBox gave up: esp_refused, retries_exhausted
    # or outcome_unknown (the answer was lost; it may still arrive, which clears `failed`,
    # and is never resent).
    print(message.delivery.failed.reason, message.delivery.failed.at)
```

`not_sent` is what tells `delivery.delivered is False` apart from itself. On its own that flag
covers both *on its way* and *never attempted*, which leaves a loop waiting for delivery nothing
to stop on. It is `None` in the ordinary case, and a later successful send withdraws it.
`failed` is its counterpart at the other end of the send path: the email was tried, and the
provider refused it, every retry failed, or (`outcome_unknown`) the provider's answer was lost,
in which case it may still arrive: a later delivery clears `failed` (back to `None`) and shows as
delivered, whatever its timestamp. Either one means stop waiting, unless an `outcome_unknown`
delivery still matters to you.

`daily_cap_reached` needs a resend: the nudge is not retried when the cap resets, so push again
the next day if the update still matters.

Compare `reason` as a string and treat an unrecognised value as "not sent": the server's list
grows whenever a refusal is added to the send path, which is why this field is not typed as the
enum.

### 2. Idempotency is handled for you, and you can do better

Every `POST` carries an `Idempotency-Key`. This SDK generates one per call and **reuses it across
its own retries**, so a connection that dies with the answer in flight cannot become a duplicate
email and a duplicate charged SMS.

`PUT` and `DELETE` do not carry one and do not need one: setting a recipient's number twice leaves
one number. Sending a message twice sends two messages and charges two credits, which is the whole
reason the header is mandatory on a push.

Pass your own whenever you have a natural key:

```python
client.messages.push(..., idempotency_key="order-4711-shipped")
```

Then a retry from *anywhere*, your queue, a cron, a human clicking twice, collapses onto the same
key rather than only the retries this SDK makes internally.

A key is at most 255 characters, and a **batch** key at most 252: each item's own key is the
batch key with `#<index>` appended. A longer one is refused with `InvalidRequestError` before
anything runs.

## Everything else

```python
from beaconbox import MessagePush

# Delivery status
client.messages.get("3xg39cvt8a46")

# Every message, following cursors. A generator, so a year of history is not held in memory
for message in client.messages.iterate(recipient_email="buyer@example.com"):
    ...

# Take one back: withdrawn for the recipient, any queued nudge called off, no credit spent
client.messages.retract("3xg39cvt8a46")

# Up to 100 pushes. Read result.failed, not the status code. A ConflictError
# (idempotency.request_in_progress) means the same batch is still running: retry it with the same key
# later. The SDK does not retry it for you, and items already sent are replayed rather than resent
result = client.messages.push_batch(
    [
        MessagePush(recipient_email="a@example.com", subject="Shipped", body="..."),
        MessagePush(recipient_email="b@example.com", subject="Shipped", body="..."),
    ]
)
for item in result.failures:
    print(item.index, item.error_code)

# Contact details. Reads are masked: a leaked key must not dump a phone book
client.recipients.sms("buyer@example.com").sms_consent_source  # push, api, admin or inbox
client.recipients.set_phone("buyer@example.com", "+37255550134")
client.recipients.clear_phone("buyer@example.com")

# The prepaid balance. One balance, shared by SMS and WhatsApp
client.credits.balance()

# Keys and webhook endpoints
client.keys.create("orders service")
endpoint = client.webhook_endpoints.create("https://example.com/hooks")  # empty = every event

# Send one sample ping and see what came back. Always 200, whatever your endpoint answers:
# the failure being reported belongs to the next hop, so read `delivered`, not the status.
result = client.webhook_endpoints.test(endpoint.id)
if not result.delivered:
    print(result.status_code, result.error, f"{result.duration_ms}ms")
```

### Pay only for the customers the email did not reach

`escalate_if_unread_after_minutes` holds the paid channel back. The SMS is sent only if the
recipient still has not opened the message after that long, and if they open it first, nothing is
sent and nothing is charged.

- **The minutes count from the email**: from `send_at` when the message is scheduled, otherwise
  from the push. If the email is still queued at the deadline, the deadline moves to that many
  minutes after it goes.
- **An email that bounced, failed or was skipped counts as unread**, so the paid channel is still
  sent, subject to its own consent and settings. That is the case the paid channel exists for: the
  email did not reach them. Only a push that queued no nudge at all (`notify=False`, or a quiet
  in-place update) never escalates.
- A channel in `on_request` mode that the push named still sends at the deadline.
- `result.sms.escalates_at` (or `result.whatsapp.escalates_at`) says when the deadline falls.

```python
client.messages.push(
    recipient_email="buyer@example.com",
    recipient_phone="+37255550134",
    subject="Action needed on your order",
    body="We could not process your payment.",
    channels=["sms"],
    escalate_if_unread_after_minutes=120,
)
```

### Erasing a recipient

`client.recipients.erase(email)` is the API half of an Article 17 request made to you, and it
is **not undoable**. It deletes the messages you sent them, their delivery events, their SMS and
WhatsApp send records, their WhatsApp replies and their recipient record (phone number, consent,
unsubscribe state), scrubs them from webhook deliveries still queued for you, and cancels sends
still waiting to go out.

It **keeps** email suppressions for the address (an unsubscribe is kept as one) and SMS or WhatsApp
opt-outs for their number, on purpose: those are what stop them being contacted again by a later
push, and an opt-out erased on request stops being one. Erasing does not undo an unsubscribe: a
later push to the address finds them still unsubscribed. The report counts both. A repeat erasure,
or one of an address you never messaged, answers with zeros; retry a timed-out call with the same
`idempotency_key` to get the original counts back.

`client.recipients.erase_whatsapp(email)` removes only their WhatsApp history and withdraws
WhatsApp consent.

Both take the recipient's email address and nothing else. A blank value, or one that is not an
address, raises `ValueError` before anything is sent (the API refuses it with a 422 anyway), so a
slip in the value fails at your line.

```python
report = client.recipients.erase("buyer@example.com")
print(report.messages_deleted, report.suppressions_kept, report.stops_kept)

receipt = client.recipients.erase_whatsapp("buyer@example.com")

# The half only you can finish: erased replies whose words may already be in *your* mailboxes.
yours = receipt.replies_a_forward_email_may_have_carried
```

## Verifying webhooks

```python
import os
from beaconbox import WebhookVerificationError, WebhookEventType, webhooks


@app.post("/hooks/beaconbox")
async def hook(request):
    try:
        event = webhooks.verify(
            await request.body(),  # the RAW body, byte for byte
            request.headers["X-BeaconBox-Signature"],
            os.environ["BEACONBOX_WEBHOOK_SECRET"],
        )
    except WebhookVerificationError:
        return Response(status_code=400)

    if event.type == WebhookEventType.MESSAGE_BOUNCED:
        ...
    return {"ok": True}
```

**Pass the raw body.** Decoding to a dict and re-encoding changes the bytes over key order and
whitespace, and the signature stops matching, in production, on a payload shaped slightly
differently from the one you tested with. The helper also checks the timestamp (which is signed,
so a captured delivery cannot be replayed) and compares in constant time.

Deliveries are **retried**, so the same `event.id` can arrive twice. Deduplicate on it.

| Event | `event.data` |
| --- | --- |
| `message.delivered`, `message.opened`, `message.bounced`, `message.complained` | `message_id`, `recipient_email`, `subject`, `channel` (`"email"`) |
| `message.not_sent` | the same, plus `reason` (an `EmailSkipReason` value): no email was attempted |
| `message.failed` | the same, plus `reason` (`esp_refused`, `retries_exhausted` or `outcome_unknown`) when BeaconBox gave up on the send: an email was attempted and will not be retried. `outcome_unknown` means the provider's answer was lost, so it may still arrive: a `message.delivered` for the message supersedes it, in whichever order the two arrive |
| `sms.delivered`, `sms.failed`, `sms.rejected`, and the same three for `whatsapp.*` | `message_id`, `recipient_phone` (masked), `country_code`, `credits_charged`, `channel`, `error_code`. Sent on the carrier's (or Meta's) report, and also when BeaconBox ends the send itself: refused at submit (`rejected`), an outcome never learned (`failed`, `*.submit_outcome_unknown`), or a send given up on after its retries (`failed`) |

Two event types are worth wiring up before you need them. `MESSAGE_NOT_SENT` carries a `reason`
and tells you an email was never attempted, the machine-readable half of the section above.
`PLAN_LAPSED` means email sending has **stopped** for your account, and every nudge from then on
produces a `MESSAGE_NOT_SENT` with reason `plan_lapsed` until the subscription is paid; it is the
one to page on, and there is no way to learn it from your own side.

## Errors

Everything inherits from `beaconbox.BeaconBoxError`.

| Exception | When |
| --- | --- |
| `AuthenticationError` | 401, key missing, malformed or revoked |
| `PermissionDeniedError` | 403, for example `plan.read_only` |
| `InvalidRequestError` | 422, a malformed field, or a reused idempotency key with a different body. Also 413, a body over the size limit |
| `ResourceMissingError` | 404, no such id. Also what another business's id looks like, deliberately |
| `ConflictError` | 409, already sent, still queued, or an identical request still in flight |
| `RateLimitError` | 429, after the SDK has already retried |
| `ServerError` | 5xx, after the SDK has already retried |
| `APIConnectionError` | no answer at all. **Not** proof the work did not happen |
| `WebhookVerificationError` | a delivery could not be proven to be ours |

Branch on `error.error_code` (a stable dotted string such as `message.not_found`), not on the
message text. Some worth knowing:

| `error_code` | Status | Means |
| --- | --- | --- |
| `sms.already_sent`, `whatsapp.already_sent` | 409 | a resend while that channel's send is still queued, or after it went |
| `whatsapp.escalated_to_sms` | 409 | a WhatsApp resend after `sms_if_whatsapp_fails` already texted them |
| `sms.may_have_sent` | 409 | an SMS resend after an earlier text ended without the carrier's answer |
| `whatsapp.may_have_sent` | 409 | a WhatsApp resend after an earlier message ended without Meta's answer |
| `sms.phone_already_in_use` | 409 | the number belongs to another of your recipients |
| `idempotency.request_in_progress` | 409 | the same key is still running; wait and retry with it |
| `request.conflict` | 409 | two requests raced to write the same thing; a retry sees the winner |
| `webhook.test_rate_limited` | 429 | `webhook_endpoints.test()` pressed too often |
| `common.validation_failed` | 422 | a field is malformed or over a limit, for example more than 100 `obsoletes` |
| `sms.phone_invalid` | 422 | `recipient_phone` or `recipients.set_phone()` got a number that does not parse or cannot be a recipient's phone: premium-rate, toll-free, shared-cost, voicemail, service, or a satellite or international code (+800, 808, 870, 878, 881, 882, 883, 888, 979). A number in those ranges stored earlier is skipped as `country_not_allowed` |
| `request.too_large` | 413 | the body is over 2 MiB (8 MiB for a batch); refused before it is read |
| `request.unstorable_input` | 422 | text holds a NUL character or a lone surrogate, which cannot be stored; the idempotency key is not spent |
| `plan.read_only` | 403 | `keys.create()` while your plan has lapsed; pay, and it works again |

A paid send that fails after the push reports its own code on the `sms.failed` or `whatsapp.failed`
webhook as `data["error_code"]`. `sms.submit_outcome_unknown` and `whatsapp.submit_outcome_unknown`
mean BeaconBox called the carrier (or Meta) and never learned the answer: the message may have
arrived, so no second copy is sent and the charge stands. A WhatsApp one still falls back to SMS
when `sms_if_whatsapp_fails` was set.

**Every response carries an `X-Request-Id`.** It is on every exception as `error.request_id` and
in its message. Quote it to support: it is the one value that finds your request in our logs.

## Configuration

```python
from beaconbox import BeaconBox, RetryPolicy

client = BeaconBox(
    api_key="bbx_live_...",  # or $BEACONBOX_API_KEY
    base_url="https://api.beaconbox.eu",  # or $BEACONBOX_BASE_URL
    timeout=30.0,
    retry_policy=RetryPolicy(max_retries=2),
    max_connections=20,
    user_agent_suffix="acme-orders/2.1",
)
```

Reuse one client: it holds a connection pool, and it is safe to share between threads. Close it,
or use it as a context manager.

Inside a job runner that already retries, pass `RetryPolicy(max_retries=0)` so the two schedules
do not multiply.

### Backpressure

A connection failure, a 429 and a 5xx are retried; a 4xx is not, because sending the same wrong
request again asks the same question. Backoff is exponential with full jitter, since the failure
being absorbed is synchronised across every worker you run.

**A `Retry-After` is honoured in full, not shortened to the backoff cap.** It is the server's own
answer to when it will be ready, and retrying earlier only earns a second 429. Jitter is added *on
top* of it rather than sampled from within it, because the herd is at its worst here: every worker
that hit the same 429 was handed the same number.

If the server asks for longer than `RetryPolicy.max_retry_after` (30s by default), the SDK **stops
rather than retrying early** and raises `RateLimitError` with `retry_after` set. Blocking for the
cap and being refused anyway helps nobody; the number is what you need to schedule a real retry.

### Concurrency

`max_connections` is a ceiling, and exceeding it does not look like one: the overflow queues for a
free connection, and a queue wait that outlives the pool timeout surfaces as `APIConnectionError`
rather than as anything mentioning a pool. If you fan out more concurrent calls than the ceiling, and
`asyncio.gather` over a few hundred pushes makes that easy, raise it to match.

## Logging

Standard library `logging`, under the `beaconbox` logger, and **silent until you ask**. The SDK
attaches a `NullHandler` and configures nothing else: no `basicConfig`, no handlers, no level on
the root logger.

```python
import logging

logging.getLogger("beaconbox").setLevel(logging.DEBUG)
```

| Level | What |
| --- | --- |
| `DEBUG` | every request and response, with status and elapsed time |
| `WARNING` | a retry (with reason and backoff), and giving up after the last one |

Nothing is emitted at `INFO` or above in normal operation, so a `WARNING` from this SDK always
means something went wrong.

Each record carries a `beaconbox` attribute with structured fields (`route`, `attempt`,
`status_code`, `elapsed_ms`, `idempotency_key`, `request_id`) for a JSON formatter. `request_id`
is the response's `X-Request-Id`, the value to quote to support.

**Nothing sensitive is ever logged.** Not the API key or any header, not request or response
bodies, not the query string, and not the interpolated URL path. A route template is logged
instead, so `/recipients/buyer@example.com/sms` appears as `/recipients/{email}/sms`. The fields
are an allow-list rather than a redaction pass, because redaction is a list of things somebody
remembered to hide and the field added next year is not on it.

### Security defaults you cannot accidentally lose

- **Plain `http` is refused** for anything but localhost, so a misconfigured `base_url` cannot put
  your API key on the wire in clear.
- **Redirects are never followed.** httpx would re-send the `Authorization` header to wherever a
  redirect points.
- **An API key that cannot go in a header is refused at construction**: a control character, a
  newline or a non-ASCII character. A `\r\n` inside the key would reach the `Authorization`
  header. Surrounding whitespace is stripped, because a key read from a file keeps its trailing
  newline.
- **A `base_url` with a query string or a fragment is refused**, because it would not fail, it would
  go somewhere else. A path prefix is fine, for a gateway that mounts BeaconBox under one.
- The key is never in a `repr`, and `NewApiKey.__repr__` redacts the minted key. Note that
  `dataclasses.asdict` and `.raw` still carry it: read the attribute, store it, drop the object.
- Webhook signatures are compared in constant time, over a signed timestamp, and a malformed or
  hostile signature value is a `WebhookVerificationError` rather than an unhandled exception, so
  the documented `except WebhookVerificationError` really does answer 400 to everything.

Need a corporate CA bundle? Pass `verify=` a path or an `ssl.SSLContext`. Need a proxy or custom
instrumentation? Pass your own `http_client=httpx.Client(...)`, and then you own closing it.

`timeout` and `verify` are **refused** alongside `http_client`, rather than silently ignored: they
are settings on the client you supplied, and accepting them while doing nothing is how somebody
discovers during an incident that the timeout they set was never applied.

## Development

```bash
uv sync --dev     # `-e '.[dev]'` installs nothing: dev lives in [dependency-groups], not an extra
uv run pytest     # hermetic, no network
uv run mypy src tests
uv run ruff check .
```

The live suite runs against a real BeaconBox. See [`tests/live/README.md`](tests/live/README.md).

## Licence

MIT. See [LICENSE](LICENSE).

BeaconBox is a product of BloomHarbor OÜ, a company registered in Estonia.
