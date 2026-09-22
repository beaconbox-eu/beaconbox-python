# beaconbox

Official Python SDK for [BeaconBox](https://beaconbox.eu), order updates your customers actually
receive.

```bash
pip install beaconbox
```

Python 3.10+. One runtime dependency, [httpx](https://www.python-httpx.org), which is also the
only HTTP library where the sync and the async client share an API, so this SDK's logic is
written once rather than twice.

## Push an update

```python
from beaconbox import BeaconBox

client = BeaconBox()  # reads BEACONBOX_API_KEY

result = client.messages.push(
    recipient_email="buyer@example.com",
    subject="Your order has shipped",
    body="Tracking XY123456789EE. Estimated delivery Thursday.",
    kind="updateable",
)

print(result.id)  # 3xg39cvt8a46
```

The customer gets an email whose link opens their inbox already signed in. No password, no
account to create.

Re-push the same `subject` with `kind="updateable"` to overwrite it in place, quietly. Add
`notify=True` to force a nudge on an update, or `obsoletes=[...]` to grey out messages this one
replaces.

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
    channels=["sms"],
)

if result.sms and result.sms.skipped_reason == SkipReason.INSUFFICIENT_CREDIT:
    # Top up, then: client.messages.resend_sms(result.id)
    ...
```

**This SDK does not raise on a skip**, deliberately. Treating one as an error is what invites a
retry, and a retry of a push that already succeeded is a second message to a real person.

### 2. Idempotency is handled for you, and you can do better

Every write carries an `Idempotency-Key`. This SDK generates one per call and **reuses it across
its own retries**, so a connection that dies with the answer in flight cannot become a duplicate
email and a duplicate charged SMS.

Pass your own whenever you have a natural key:

```python
client.messages.push(..., idempotency_key="order-4711-shipped")
```

Then a retry from *anywhere*, your queue, a cron, a human clicking twice, collapses onto the same
key rather than only the retries this SDK makes internally.

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

# Up to 100 pushes. Always 200: read result.failed, not the status code
result = client.messages.push_batch(
    [
        MessagePush(recipient_email="a@example.com", subject="Shipped", body="..."),
        MessagePush(recipient_email="b@example.com", subject="Shipped", body="..."),
    ]
)
for item in result.failures:
    print(item.index, item.error_code)

# Contact details. Reads are masked: a leaked key must not dump a phone book
client.recipients.set_phone("buyer@example.com", "+37255550134")
client.recipients.clear_phone("buyer@example.com")

# The prepaid balance. One balance, shared by SMS and WhatsApp
client.credits.balance()

# Keys and webhook endpoints
client.keys.create("orders service")
client.webhook_endpoints.create("https://example.com/hooks")  # empty list means every event
```

### Pay only for the customers the email did not reach

`escalate_if_unread_after_minutes` holds the paid channel back. The SMS is sent only if the
recipient still has not opened the message after that long, and if they open it first, nothing is
sent and nothing is charged.

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

### Erasing a recipient's WhatsApp history

```python
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

## Errors

Everything inherits from `beaconbox.BeaconBoxError`.

| Exception | When |
| --- | --- |
| `AuthenticationError` | 401, key missing, malformed or revoked |
| `PermissionDeniedError` | 403 |
| `InvalidRequestError` | 422, a malformed field, or a reused idempotency key with a different body |
| `ResourceMissingError` | 404, no such id. Also what another business's id looks like, deliberately |
| `ConflictError` | 409, already sent, or an identical request still in flight |
| `RateLimitError` | 429, after the SDK has already retried |
| `ServerError` | 5xx, after the SDK has already retried |
| `APIConnectionError` | no answer at all. **Not** proof the work did not happen |
| `WebhookVerificationError` | a delivery could not be proven to be ours |

Branch on `error.error_code` (a stable dotted string such as `message.not_found`), not on the
message text.

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
top* of it rather than sampled from within it — the herd is at its worst here, because every worker
that hit the same 429 was handed the same number.

If the server asks for longer than `RetryPolicy.max_retry_after` (30s by default), the SDK **stops
rather than retrying early** and raises `RateLimitError` with `retry_after` set. Blocking for the
cap and being refused anyway helps nobody; the number is what you need to schedule a real retry.

### Concurrency

`max_connections` is a ceiling, and exceeding it does not look like one: the overflow queues for a
free connection, and a queue wait that outlives the pool timeout surfaces as `APIConnectionError`
rather than as anything mentioning a pool. If you fan out more concurrent calls than the ceiling —
`asyncio.gather` over a few hundred pushes makes that easy — raise it to match.

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
`status_code`, `elapsed_ms`, `idempotency_key`) for a JSON formatter.

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
- The key is never in a `repr`, and `NewApiKey.__repr__` redacts the minted key.
- Webhook signatures are compared in constant time, over a signed timestamp.

Need a corporate CA bundle? Pass `verify=` a path or an `ssl.SSLContext`. Need a proxy or custom
instrumentation? Pass your own `http_client=httpx.Client(...)`, and then you own closing it.

`timeout` and `verify` are **refused** alongside `http_client`, rather than silently ignored: they
are settings on the client you supplied, and accepting them while doing nothing is how somebody
discovers during an incident that the timeout they set was never applied.

## Development

```bash
uv venv && uv pip install -e '.[dev]'
pytest            # hermetic, no network
mypy src tests
ruff check .
```

The live suite runs against a real BeaconBox. See [`tests/live/README.md`](tests/live/README.md).

## Licence

MIT. See [LICENSE](LICENSE).

BeaconBox is a product of BloomHarbor OÜ, a company registered in Estonia.
