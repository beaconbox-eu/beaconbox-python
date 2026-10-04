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

    @pytest.mark.parametrize("digits", [21, 4301, 100_000])
    def test_an_absurdly_long_timestamp_is_malformed_and_not_a_crash(self, digits: int) -> None:
        """``int()`` raises ``ValueError`` past ``sys.get_int_max_str_digits()`` (4300 by default).

        The value comes from a header anyone on the internet can write to a merchant's webhook
        route, so an unbounded digit run escaped this function as a ``ValueError``, missed the
        documented ``except WebhookVerificationError``, and turned the intended 400 into a 500 with
        a stack trace. The pattern is length-bounded precisely so the conversion cannot raise.
        """
        header = f"t={'9' * digits},v1={'a' * 64}"

        with pytest.raises(WebhookVerificationError, match="malformed"):
            webhooks.verify(BODY, header, SECRET, now=NOW)

    def test_the_longest_timestamp_it_accepts_is_still_handled(self) -> None:
        """The boundary itself: twenty digits parses, and is then simply far outside tolerance."""
        header = f"t={'9' * 20},v1={'a' * 64}"

        with pytest.raises(WebhookVerificationError, match="seconds off"):
            webhooks.verify(BODY, header, SECRET, now=NOW)

    @pytest.mark.parametrize(
        "timestamp",
        [f"+{NOW}", f" {NOW} ", "1_700_000_000", "\u0661\u0667" + "\u0660" * 8],  # Arabic-Indic
    )
    def test_a_timestamp_only_int_would_accept_is_malformed(self, timestamp: str) -> None:
        """``int()`` reads a leading sign, surrounding space, underscores and other scripts' digits.

        The timestamp is inside the signed string, so each of those is an alternative spelling of
        a header that has exactly one canonical form, and accepting them widens what counts as a
        delivery for no gain.
        """
        header = f"t={timestamp},v1={'a' * 64}"

        with pytest.raises(WebhookVerificationError, match="malformed"):
            webhooks.verify(BODY, header, SECRET, now=NOW)

    @pytest.mark.parametrize(
        "signature",
        [
            "\u00e9",  # a single non-ASCII character
            "caf\u00e9" * 16,  # non-ASCII at hex-digest length
            "\u0161" + "a" * 63,  # non-ASCII in the first position
            "zz" * 32,  # ASCII, right length, not hex
            "abc",  # an odd number of hex digits
            "a" * 4096,  # absurdly long
        ],
    )
    def test_a_hostile_signature_value_is_a_verification_error(self, signature: str) -> None:
        """Not a ``TypeError``, which is what ``hmac.compare_digest`` raises on a non-ASCII ``str``.

        Anyone on the internet can POST to a merchant's webhook route, so every one of these is
        reachable by an unauthenticated caller. The documented handler catches
        ``WebhookVerificationError`` and answers 400; a ``TypeError`` escaping this function made
        that a 500 with a stack trace instead, on input an attacker chooses.
        """
        header = f"t={NOW},v1={signature}"

        with pytest.raises(WebhookVerificationError, match="does not match"):
            webhooks.verify(BODY, header, SECRET, now=NOW)

    def test_an_uppercase_hex_signature_still_verifies(self) -> None:
        """Hex is hex. Rejecting the case a sender happened to choose would be a false negative."""
        header = sign().upper().replace("T=", "t=").replace("V1=", "v1=")

        assert webhooks.verify(BODY, header, SECRET, now=NOW).id == "evt_1"

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


class TestTheMismatchMessageIsActionable:
    """**The error a merchant actually hits, and the one most likely to be misread.**

    "Signature does not match" sounds like a key problem. The overwhelmingly common cause is not:
    the bytes being checked are not the bytes that were sent, because something between the wire
    and the check parsed and re-serialized the JSON. Found by using our own product — a body
    copied out of a webhook inspector's "copy as curl" failed exactly this way, and the first
    instinct was to go looking at the secret.
    """

    def test_a_reserialized_body_is_what_fails(self) -> None:
        """The failure reproduced from its real cause, not from a corrupted byte. The re-serialized
        body is *semantically identical* — same keys, same values, parses the same — and still
        cannot verify, which is precisely why it is so confusing in the wild."""
        reserialized = json.dumps(json.loads(BODY), indent=2).encode()
        assert json.loads(reserialized) == json.loads(BODY), "must differ only in formatting"

        with pytest.raises(WebhookVerificationError) as caught:
            webhooks.verify(reserialized, sign(), SECRET, now=NOW)

        assert "raw request bytes" in str(caught.value)

    def test_the_message_names_the_likely_cause_before_the_secret(self) -> None:
        """Ordering is the point: someone who reads "check your secret" first rotates a working
        secret before thinking about the body."""
        with pytest.raises(WebhookVerificationError) as caught:
            webhooks.verify(b'{"tampered":true}', sign(), SECRET, now=NOW)

        message = str(caught.value)
        assert message.index("raw request bytes") < message.index("secret")
