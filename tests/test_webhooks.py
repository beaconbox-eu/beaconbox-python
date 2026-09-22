"""Webhook verification, which is the one place in this SDK where a bug is a security hole.

The signatures in these tests are computed the way the API computes them, from the API's own
source: HMAC-SHA256 over `"<timestamp>.<raw body>"`. If this file and
``services/webhooks_out.sign`` ever disagree, one of them is wrong and every merchant integration
finds out at the same time.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from beaconbox import WebhookEventType, WebhookVerificationError, webhooks

SECRET = "whsec_" + "a" * 64
NOW = 1_755_680_000
BODY = (
    b'{"id":"evt_1","type":"message.bounced",'
    b'"occurred_at":"2026-08-20T09:20:00Z","data":{"message_id":"m_1"}}'
)


def sign(body: bytes = BODY, secret: str = SECRET, timestamp: int = NOW) -> str:
    digest = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


class TestAccepts:
    def test_a_genuine_delivery(self) -> None:
        event = webhooks.verify(BODY, sign(), SECRET, now=NOW)

        assert event.id == "evt_1"
        assert event.type is WebhookEventType.MESSAGE_BOUNCED
        assert event.data == {"message_id": "m_1"}

    def test_a_str_body_as_well_as_bytes(self) -> None:
        """Some frameworks hand back a str. Encoding it here beats a caller doing it wrong."""
        event = webhooks.verify(BODY.decode(), sign(), SECRET, now=NOW)
        assert event.id == "evt_1"

    def test_a_delivery_inside_the_tolerance_window(self) -> None:
        webhooks.verify(BODY, sign(timestamp=NOW - 299), SECRET, now=NOW)

    def test_clock_skew_in_either_direction(self) -> None:
        """A receiver's clock can be ahead of ours as easily as behind."""
        webhooks.verify(BODY, sign(timestamp=NOW + 100), SECRET, now=NOW)

    def test_an_unknown_event_type_still_verifies(self) -> None:
        """A type added after this SDK shipped is a delivery to handle, not a forgery."""
        body = (
            b'{"id":"evt_2","type":"parcel.collected",'
            b'"occurred_at":"2026-08-20T09:20:00Z","data":{}}'
        )
        event = webhooks.verify(body, sign(body), SECRET, now=NOW)

        assert event.type == "parcel.collected"

    def test_extra_signature_pairs_are_tolerated(self) -> None:
        """So that a future `v2=` scheme does not make every deployed verifier reject the
        delivery outright."""
        header = sign() + ",v2=notyetimplemented"
        webhooks.verify(BODY, header, SECRET, now=NOW)


class TestRejects:
    def test_a_tampered_body(self) -> None:
        tampered = BODY.replace(b"m_1", b"m_2")

        with pytest.raises(WebhookVerificationError, match="does not match"):
            webhooks.verify(tampered, sign(), SECRET, now=NOW)

    def test_the_wrong_secret(self) -> None:
        with pytest.raises(WebhookVerificationError, match="does not match"):
            webhooks.verify(BODY, sign(secret="whsec_" + "b" * 64), SECRET, now=NOW)

    def test_a_replayed_delivery(self) -> None:
        """The timestamp is inside the signed string, so a captured delivery cannot be re-sent
        later with a fresh one."""
        with pytest.raises(WebhookVerificationError, match="tolerance"):
            webhooks.verify(BODY, sign(timestamp=NOW - 3600), SECRET, now=NOW)

    def test_a_moved_timestamp_breaks_the_signature(self) -> None:
        """Proves the timestamp is signed rather than merely sent alongside."""
        header = sign().replace(f"t={NOW}", f"t={NOW - 10}")

        with pytest.raises(WebhookVerificationError, match="does not match"):
            webhooks.verify(BODY, header, SECRET, now=NOW - 10)

    @pytest.mark.parametrize(
        "header",
        ["", "garbage", "t=,v1=", "v1=abc", f"t={NOW}", "t=notanumber,v1=abc", "t=0,v1=abc"],
    )
    def test_a_malformed_header(self, header: str) -> None:
        with pytest.raises(WebhookVerificationError):
            webhooks.verify(BODY, header, SECRET, now=NOW)

    def test_a_body_that_is_not_json(self) -> None:
        body = b"not json at all"

        with pytest.raises(WebhookVerificationError, match="not JSON"):
            webhooks.verify(body, sign(body), SECRET, now=NOW)

    def test_a_json_body_that_is_not_an_object(self) -> None:
        body = b"[1,2,3]"

        with pytest.raises(WebhookVerificationError, match="not a JSON object"):
            webhooks.verify(body, sign(body), SECRET, now=NOW)


def test_re_encoding_the_body_breaks_verification() -> None:
    """The single most common way to get this wrong, demonstrated.

    Round-tripping through `json.loads`/`json.dumps` changes the bytes over separators and key
    order. It passes in development against a payload that happens to survive it, and fails in
    production against one that does not. Pass the raw body.
    """
    re_encoded = json.dumps(json.loads(BODY)).encode()
    assert re_encoded != BODY

    with pytest.raises(WebhookVerificationError):
        webhooks.verify(re_encoded, sign(BODY), SECRET, now=NOW)


def test_comparison_is_constant_time() -> None:
    """A `==` on the hex digest leaks, through timing, how much of a guess was right.

    Asserted structurally rather than by measuring: a timing assertion in a test suite is a
    flake on a loaded CI runner.
    """
    import inspect

    source = inspect.getsource(webhooks.verify)
    assert "hmac.compare_digest" in source
