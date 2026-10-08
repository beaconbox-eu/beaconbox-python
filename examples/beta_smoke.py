"""Drive the API the way a merchant's integration does, and report what broke.

Not a unit test — those run offline against a fake transport. This exercises the paths a real
integration takes against a real deployment, in the order a real integration takes them, because
the failures worth finding before beta are the ones that only appear when the pieces are
connected: an idempotency replay that returns a *different* body, a batch that reports success
while half its items were rejected, a delivery status that never advances past SENT.

Read-only and additive by default. The two steps that leave permanent state — a suppression from
a hard bounce, and an unsubscribe — are behind flags, because both are irreversible per address
and a suppressed address silently changes the result of every later run.

    export BEACONBOX_API_KEY=bbx_live_...
    uv run python examples/beta_smoke.py --to test@bloomharbor.eu
    uv run python examples/beta_smoke.py --to test@bloomharbor.eu --bounce
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import uuid

from beaconbox import (
    BeaconBox,
    InvalidRequestError,
    MessageKind,
    MessagePush,
    OrderStatus,
)

# Long enough for a push to be sent and the provider's delivery webhook to make the round trip.
# A delivery event is not synchronous with the push, so a status read straight after a push
# legitimately shows SENT — polling is the only honest way to assert on it.
_DELIVERY_TIMEOUT_SECONDS = 120
_POLL_SECONDS = 5

_passed: list[str] = []
_failed: list[tuple[str, str]] = []


def check(name: str, condition: bool, detail: str = "") -> bool:
    (_passed if condition else _failed).append(name if condition else (name, detail))  # type: ignore[arg-type]
    print(
        f"  {'PASS' if condition else 'FAIL'}  {name}"
        + (f" — {detail}" if detail and not condition else "")
    )
    return condition


def await_delivery(client: BeaconBox, public_id: str) -> object:
    """Poll until the delivery event lands, or the timeout says it never will."""
    deadline = time.monotonic() + _DELIVERY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        message = client.messages.get(public_id)
        if message.delivery.delivered or message.delivery.bounced or message.delivery.not_sent:
            return message.delivery
        time.sleep(_POLL_SECONDS)
    return client.messages.get(public_id).delivery


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--to", required=True, help="A mailbox you control and can read.")
    parser.add_argument(
        "--bounce",
        action="store_true",
        help="Also push to a guaranteed-dead address and assert the bounce suppresses it. "
        "Permanent for that address.",
    )
    parser.add_argument(
        "--webhook-url",
        default="",
        help="Register this URL, send it a test event, then delete it. Needs a receiver you can "
        "watch — webhook.site or similar. Must be https and publicly resolvable.",
    )
    args = parser.parse_args()

    key = os.environ.get("BEACONBOX_API_KEY")
    if not key:
        print("BEACONBOX_API_KEY is unset.", file=sys.stderr)
        return 2
    client = BeaconBox(api_key=key)
    run = uuid.uuid4().hex[:8]
    print(f"\nRun {run} against {client.base_url}\n")

    # --- the ordinary path, three updates about one order -------------------------------
    print("Single pushes")
    first = client.messages.push(
        recipient_email=args.to,
        subject=f"Order #{run} is on its way",
        body="Your order shipped today. Tracking: XX000000000XX.",
        reference=f"#{run}",
        order_status=OrderStatus.SHIPPED,
    )
    check(
        "push accepted",
        first.created and first.nudged,
        f"created={first.created} nudged={first.nudged}",
    )
    # The per-channel block. A clean recipient must report `sending`; if this is False on the
    # very first push the address is already suppressed and every later assertion here is
    # measuring the wrong thing.
    check(
        "email block reports sending",
        first.email is not None and first.email.sending,
        f"email={first.email}",
    )

    # --- idempotency: the property the SDK's retry safety rests on ----------------------
    print("\nIdempotency")
    repeat_key = str(uuid.uuid4())
    one = client.messages.push(
        recipient_email=args.to, subject=f"Replay {run}", body="First.", idempotency_key=repeat_key
    )
    two = client.messages.push(
        # A DIFFERENT body under the SAME key. The stored response must come back unchanged —
        # if the second body were stored, every SDK retry would be a silent overwrite.
        recipient_email=args.to,
        subject=f"Replay {run}",
        body="Second — must never be stored.",
        idempotency_key=repeat_key,
    )
    check("replay returns the original id", one.id == two.id, f"{one.id} vs {two.id}")
    check("replay does not re-create", two.created is False, f"created={two.created}")

    # --- updateable: the same subject edits in place rather than stacking ---------------
    print("\nUpdateable messages")
    subject = f"Order #{run} status"
    opened = client.messages.push(
        recipient_email=args.to, subject=subject, body="Preparing.", kind=MessageKind.UPDATEABLE
    )
    revised = client.messages.push(
        recipient_email=args.to,
        subject=subject,
        body="Out for delivery.",
        kind=MessageKind.UPDATEABLE,
    )
    check("updateable matches on subject", opened.id == revised.id, f"{opened.id} vs {revised.id}")
    check("update does not re-create", revised.created is False, f"created={revised.created}")

    # --- batch: the separate code path, and the one that fails quietly -----------------
    print("\nBatch push")
    batch = client.messages.push_batch(
        [
            MessagePush(recipient_email=args.to, subject=f"Batch {run} item {n}", body=f"Item {n}.")
            for n in range(1, 4)
        ]
    )
    check("batch accepted all", not batch.failed, f"failed={batch.failed}")

    # --- reads: what a merchant's dashboard calls ---------------------------------------
    print("\nReads")
    fetched = client.messages.get(first.id)
    check("get returns the message", fetched.id == first.id)
    listed = list(client.messages.iterate())
    check(
        "iterate finds the push", any(m.id == first.id for m in listed), f"{len(listed)} message(s)"
    )
    balance = client.credits.balance()
    check("credit balance readable", balance is not None, repr(balance))

    # --- errors: the SDK must raise something catchable and specific --------------------
    print("\nError handling")
    try:
        client.messages.push(recipient_email="not-an-email", subject="x", body="y")
        check("invalid email rejected", False, "the API accepted a malformed address")
    except InvalidRequestError as exc:
        check("invalid email raises InvalidRequestError", True, str(exc))

    # --- delivery: the whole provider round trip, asserted rather than grepped ----------
    print("\nDelivery (polling)")
    delivery = await_delivery(client, first.id)
    check("delivery event correlated", bool(getattr(delivery, "delivered", False)), repr(delivery))

    # --- webhooks: the subsystem nothing else exercises ---------------------------------
    #
    # Registered, tested against a receiver we control, then removed. The test POST is the only
    # part of the outbound path that can be driven on demand — every other delivery waits on a
    # real event — so it is the only way to prove the signing, the URL vetting and the transport
    # before a merchant's integration depends on them.
    if args.webhook_url:
        print("\nWebhook endpoints")
        endpoint = client.webhook_endpoints.create(
            args.webhook_url, description=f"beta smoke {run}"
        )
        check("endpoint registered", bool(endpoint.id), f"id={endpoint.id}")
        check(
            "secret returned once",
            bool(endpoint.secret) and "\u2022" not in endpoint.secret,
            "the create response must carry the real secret, not a masked one",
        )
        try:
            result = client.webhook_endpoints.test(endpoint.id)
            check(
                "test event delivered",
                result.delivered,
                f"status={result.status_code} error={result.error}",
            )
            listed = {e.id: e for e in client.webhook_endpoints.list()}
            check("endpoint listed", endpoint.id in listed)
            check(
                "testing left health untouched",
                listed[endpoint.id].consecutive_failures == 0 and not listed[endpoint.id].disabled,
                f"failures={listed[endpoint.id].consecutive_failures}",
            )
        finally:
            # Always, even on a failed assertion: a registered endpoint keeps receiving real events
            # for this business, and leaving one pointed at a scratch URL would quietly send a
            # merchant's delivery outcomes somewhere nobody is reading.
            client.webhook_endpoints.delete(endpoint.id)

    # --- bounce: destructive, opt-in ----------------------------------------------------
    if args.bounce:
        print("\nBounce and suppression")
        domain = args.to.split("@", 1)[1]
        dead = f"definitely-no-such-mailbox-{run}@{domain}"
        bounced = client.messages.push(
            recipient_email=dead, subject=f"Bounce probe {run}", body="Expected to hard bounce."
        )
        state = await_delivery(client, bounced.id)
        check("hard bounce recorded", bool(getattr(state, "bounced", False)), repr(state))
        again = client.messages.push(
            recipient_email=dead, subject=f"Bounce probe {run} second", body="Must be suppressed."
        )
        after = client.messages.get(again.id)
        check(
            "suppressed on the next send",
            after.delivery.not_sent is not None,
            f"not_sent={after.delivery.not_sent}",
        )
        # Both surfaces must agree. The push-time block is advisory and the stored event is
        # authoritative, so a disagreement means the pre-check and the send gate have drifted
        # apart — the one failure mode of running the same rule in two places.
        check(
            "push-time block agreed with the stored outcome",
            again.email is not None and not again.email.sending,
            f"push said {again.email}, stored said {after.delivery.not_sent}",
        )

    print(f"\n{len(_passed)} passed, {len(_failed)} failed")
    for name, detail in _failed:
        print(f"  FAIL  {name} — {detail}")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
