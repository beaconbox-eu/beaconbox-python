# Live tests (Python)

These run the SDK against a **real** BeaconBox. They are skipped unless `BEACONBOX_LIVE_URL` and
`BEACONBOX_API_KEY` are set, so plain `pytest` stays hermetic and fast.

## Running them

From `sdk/`:

```bash
mise run //deployment/local:up   # once, if the stack is not already running
mise run //sdk:live-python       # seeds a fresh business and runs this suite
```

`mise run //sdk:live` runs this suite and the PHP one against the **same** seed, which is the better command
when you have changed something both SDKs share.

## Running them by hand

```bash
export BEACONBOX_LIVE_URL=https://api.beaconbox.localhost
export BEACONBOX_API_KEY=$(../../scripts/seed.sh | python -c 'import json,sys; print(json.load(sys.stdin)["api_key"])')
export BEACONBOX_LIVE_CA_BUNDLE="$(mkcert -CAROOT)/rootCA.pem"
pytest tests/live -q
```

`BEACONBOX_LIVE_CA_BUNDLE` is needed against the local stack and not against a public deployment.
httpx verifies TLS against certifi rather than the system keychain, so the mkcert certificate the
local stack serves is correctly rejected until the CA is supplied. Passing it exercises the same
`verify=` option a merchant behind a TLS-inspecting corporate proxy needs, which is why the suite
does that rather than turning verification off and leaving the SDK's security default untested.

`BEACONBOX_LIVE_RECIPIENT` overrides the recipient address, and defaults to
`live-sdk@example.com`.

## What they are for

Every hermetic test asserts against a payload this repository wrote. If the SDK and the API
disagree about a field name, both sides of a unit test agree with each other and are wrong
together. That is how `push_batch` can pass a full unit suite and 422 on every real call.

So the assertions here are deliberately shallow. What matters is that the server accepted the
request and the SDK understood the answer. The exceptions are the three behaviours no mock can
prove:

- an `updateable` push with a repeated subject updates in place instead of creating a second
  message;
- two pushes under one idempotency key are one message in the real store;
- a key minted through `keys.create()` actually authenticates.

## Notes

- **They write.** Each run pushes real messages and mints and revokes real keys against a
  throwaway seeded business. Do not point them at production.
- Subjects are uniquified per run, because an `updateable` push matches on subject and a reused one
  would update the previous run's message.
- Keys and webhook endpoints created here are cleaned up in `finally` blocks.
