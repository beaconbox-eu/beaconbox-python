"""Parsing: what a model does with a payload that is fine, and with one that is not.

The forward-compatibility tests here are the load-bearing ones. An SDK that raises on a field it
has not heard of turns an additive API change into an outage in a merchant's job runner, months
after the SDK was last touched.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest

from beaconbox import (
    DeliveryStatus,
    EmailSkipReason,
    MessageKind,
    MessagePush,
    MessagePushResult,
    MessageStatus,
    RecipientSms,
    RecipientStatus,
    SkipReason,
    SmsOutcome,
    WebhookEndpoint,
    WebhookEvent,
)
from beaconbox.models import _parse_datetime
from tests.conftest import MESSAGE_VIEW, PUSH_RESULT


class TestDatetimes:
    def test_parses_a_trailing_z(self) -> None:
        """`fromisoformat` did not accept `Z` until 3.11, and this SDK supports 3.10."""
        parsed = _parse_datetime("2026-08-20T09:15:00Z")
        assert parsed == datetime(2026, 8, 20, 9, 15, tzinfo=timezone.utc)

    def test_parses_an_explicit_offset(self) -> None:
        parsed = _parse_datetime("2026-08-20T12:15:00+03:00")
        assert parsed is not None
        assert parsed.utcoffset() == timedelta(hours=3)

    def test_a_naive_timestamp_is_stamped_utc(self) -> None:
        """A naive datetime compared against an aware one raises at some later, less obvious
        call site."""
        parsed = _parse_datetime("2026-08-20T09:15:00")
        assert parsed is not None
        assert parsed.tzinfo is timezone.utc

    @pytest.mark.parametrize("value", [None, "", "not a date", 12345, {}])
    def test_anything_unparseable_is_none_rather_than_an_exception(self, value: object) -> None:
        assert _parse_datetime(value) is None

    def test_a_required_timestamp_that_is_missing_does_not_crash_the_parse(self) -> None:
        result = MessagePushResult.from_api({**PUSH_RESULT, "created_at": None})
        assert result.created_at.year == 1970


class TestForwardCompatibility:
    def test_an_unknown_enum_value_falls_back_to_the_string(self) -> None:
        """A `MessageStatus` shipped next quarter must not become a crash loop."""
        result = MessagePushResult.from_api({**PUSH_RESULT, "status": "quarantined"})

        assert result.status == "quarantined"
        assert not isinstance(result.status, MessageStatus)

    def test_a_known_enum_value_becomes_the_member(self) -> None:
        result = MessagePushResult.from_api(PUSH_RESULT)

        assert result.kind is MessageKind.UPDATEABLE
        assert result.status is MessageStatus.ACTIVE

    def test_an_unknown_field_is_ignored_and_kept_in_raw(self) -> None:
        result = MessagePushResult.from_api({**PUSH_RESULT, "postage_class": "tracked"})

        assert result.raw["postage_class"] == "tracked"

    def test_missing_optional_blocks_are_none_not_errors(self) -> None:
        minimal = {
            "id": "m_1",
            "subject": "s",
            "kind": "one_off",
            "status": "active",
            "created": True,
            "nudged": False,
            "created_at": "2026-08-20T09:00:00Z",
            "updated_at": "2026-08-20T09:00:00Z",
        }
        result = MessagePushResult.from_api(minimal)

        assert result.sms is None
        assert result.whatsapp is None
        assert result.whatsapp_opt_in is None

    def test_a_null_in_a_required_field_does_not_raise(self) -> None:
        """The tolerance this module promises, on the shape that used to break it.

        ``int(None)`` and ``str(None)`` were the parser, so an explicit JSON ``null`` in a numeric
        field raised ``TypeError`` out of a push that had already been charged for, and a null in a
        string field produced the four-character string ``"None"``.
        """
        result = MessagePushResult.from_api(
            {**PUSH_RESULT, "id": None, "subject": None, "sms_units": None, "reference": None}
        )

        assert result.id == ""
        assert result.subject == ""
        assert result.sms_units == 0
        assert result.reference is None

    def test_a_null_credit_count_does_not_raise(self) -> None:
        outcome = SmsOutcome.from_api({"queued": False, "credits": None})

        assert outcome.credits == 0

    def test_an_integral_float_is_read_as_the_integer(self) -> None:
        """JSON has one number type, and a hop that re-serialises ``1`` as ``1.0`` must not turn
        one credit into zero."""
        assert SmsOutcome.from_api({"queued": True, "credits": 1.0}).credits == 1

    def test_a_boolean_is_not_read_as_a_number(self) -> None:
        """``True`` is an ``int`` in Python, which is a fact about Python and not about the API."""
        assert SmsOutcome.from_api({"queued": True, "credits": True}).credits == 0

    def test_a_wrongly_typed_optional_string_reads_as_absent(self) -> None:
        """Rather than handing back an int a caller would compare against ``SkipReason``."""
        assert (
            SmsOutcome.from_api({"queued": False, "credits": 1, "skipped_reason": 7}).skipped_reason
            is None
        )

    @pytest.mark.parametrize("value", [None, 7, True, [], {}])
    def test_an_enum_field_is_only_ever_a_member_or_a_string(self, value: object) -> None:
        """`coerce` returned `value` unchanged, so a null enum field produced `None` and a number
        produced an `int` — values the `RecipientStatus | str` annotation said could not be there.

        The empty string rather than a `repr`: `""` is obviously absent, where `"None"` or `"7"`
        looks like something the server sent. The PHP SDK has always read these as `""`.
        """
        parsed = RecipientSms.from_api({"recipient_email": "a@b.c", "sms_status": value})

        assert parsed.sms_status == ""
        assert isinstance(parsed.sms_status, str)

    def test_a_known_enum_value_still_becomes_the_member(self) -> None:
        parsed = RecipientSms.from_api({"recipient_email": "a@b.c", "sms_status": "stopped"})

        assert parsed.sms_status is RecipientStatus.STOPPED

    def test_a_boolean_field_is_not_pythons_truthiness(self) -> None:
        """The PHP SDK reads `"queued": "yes"` as false, because its parser tests `is_bool`.

        `bool(value)` here read it as true, so two workers in two languages disagreed about whether
        a message had been sent, from the same payload. Strict is the safer reading of the pair: a
        non-boolean in a boolean field is a corrupt response, not a claim the API made.
        """
        assert SmsOutcome.from_api({"queued": "yes", "credits": 1}).queued is False
        assert SmsOutcome.from_api({"queued": None, "credits": 1}).queued is False
        assert SmsOutcome.from_api({"queued": 1, "credits": 1}).queued is False
        assert SmsOutcome.from_api({"queued": True, "credits": 1}).queued is True

    def test_a_non_string_event_type_is_dropped_rather_than_stringified(self) -> None:
        endpoint = WebhookEndpoint.from_api(
            {"id": "wh_1", "event_types": ["message.opened", 7, None, "sms.failed"]}
        )

        assert endpoint.event_types == ("message.opened", "sms.failed")


class TestOutcomes:
    def test_a_skip_carries_its_reason(self) -> None:
        outcome = SmsOutcome.from_api(
            {"queued": False, "credits": 1, "skipped_reason": "insufficient_credit"}
        )

        assert outcome.queued is False
        assert outcome.skipped_reason == SkipReason.INSUFFICIENT_CREDIT

    def test_an_armed_escalation_is_neither_queued_nor_skipped(self) -> None:
        """Nothing was refused and nothing has been charged. The send is armed for later."""
        outcome = SmsOutcome.from_api(
            {"queued": False, "credits": 1, "escalates_at": "2026-08-20T11:15:00Z"}
        )

        assert outcome.queued is False
        assert outcome.skipped_reason is None
        assert outcome.escalates_at == datetime(2026, 8, 20, 11, 15, tzinfo=timezone.utc)

    def test_opt_in_outcome_reports_status_after_the_push(self) -> None:
        result = MessagePushResult.from_api(
            {
                **PUSH_RESULT,
                "whatsapp_opt_in": {
                    "recorded": False,
                    "status": "stopped",
                    "skipped_reason": "opted_out",
                },
            }
        )

        assert result.whatsapp_opt_in is not None
        assert result.whatsapp_opt_in.status is RecipientStatus.STOPPED
        assert result.whatsapp_opt_in.recorded is False


class TestImmutability:
    def test_a_result_cannot_be_edited(self) -> None:
        """A response is a fact about a moment. A mutated one claims to be an API answer and is
        not, and that object then gets logged."""
        result = MessagePushResult.from_api(PUSH_RESULT)

        with pytest.raises(FrozenInstanceError):
            result.id = "m_other"  # type: ignore[misc]

    def test_raw_is_excluded_from_equality_and_repr(self) -> None:
        """Otherwise every model's repr is the whole payload twice over."""
        one = MessagePushResult.from_api(PUSH_RESULT)
        two = MessagePushResult.from_api({**PUSH_RESULT, "an_added_field": 1})

        assert one == two
        assert "an_added_field" not in repr(two)


class TestMessagePushPayload:
    def test_only_the_required_fields_by_default(self) -> None:
        payload = MessagePush(recipient_email="a@b.c", subject="s", body="b").to_payload()

        assert payload == {
            "recipient_email": "a@b.c",
            "subject": "s",
            "body": "b",
            "kind": "one_off",
        }

    def test_send_at_is_serialized(self) -> None:
        when = datetime(2026, 8, 21, 8, 0, tzinfo=timezone.utc)
        payload = MessagePush(
            recipient_email="a@b.c", subject="s", body="b", send_at=when
        ).to_payload()

        assert payload["send_at"] == "2026-08-21T08:00:00+00:00"

    def test_a_naive_send_at_is_refused_here_rather_than_at_the_api(self) -> None:
        """The failure it otherwise produces is a nudge delivered at the wrong hour in the
        recipient's day, which nobody notices as a bug."""
        message = MessagePush(
            recipient_email="a@b.c",
            subject="s",
            body="b",
            send_at=datetime(2026, 8, 21, 8, 0),
        )

        with pytest.raises(ValueError, match="timezone-aware"):
            message.to_payload()

    def test_empty_obsoletes_is_omitted(self) -> None:
        payload = MessagePush(recipient_email="a@b.c", subject="s", body="b").to_payload()
        assert "obsoletes" not in payload

    def test_escalation_and_fallback_pass_through(self) -> None:
        payload = MessagePush(
            recipient_email="a@b.c",
            subject="s",
            body="b",
            escalate_if_unread_after_minutes=120,
            sms_if_whatsapp_fails=True,
        ).to_payload()

        assert payload["escalate_if_unread_after_minutes"] == 120
        assert payload["sms_if_whatsapp_fails"] is True


class TestNested:
    def test_a_message_parses_its_whole_delivery_tree(self) -> None:
        from beaconbox import Message

        message = Message.from_api(MESSAGE_VIEW)

        assert message.delivery.delivered is True
        assert message.delivery.opened is False
        assert message.delivery.sms is not None
        assert message.delivery.sms.status == "delivered"
        assert message.delivery.sms.submitted_at is not None

    def test_a_webhook_event_keeps_its_data_untouched(self) -> None:
        event = WebhookEvent.from_api(
            {
                "id": "evt_1",
                "type": "message.bounced",
                "occurred_at": "2026-08-20T09:20:00Z",
                "data": {"message_id": "m_1", "reason": "mailbox full"},
            }
        )

        assert event.type == "message.bounced"
        assert event.data["reason"] == "mailbox full"


class TestSecrets:
    def test_a_minted_key_is_redacted_in_its_repr(self) -> None:
        """A `print(key)` in a debugging session is how a live credential reaches a log
        aggregator."""
        from beaconbox import NewApiKey

        key = NewApiKey.from_api(
            {
                "id": "k_7Hq2",
                "name": "orders",
                "key": "bbx_live_theactualsecret",
                "masked": "bbx_live_••••••••cret",
                "created": "2026-08-20T00:00:00Z",
            }
        )

        assert "theactualsecret" not in repr(key)
        assert key.key == "bbx_live_theactualsecret"
        # The id is in the repr: it is the handle for revoking, and it is not the secret.
        assert "k_7Hq2" in repr(key)

    def test_a_new_endpoints_secret_is_redacted_in_its_repr(self) -> None:
        from beaconbox import NewWebhookEndpoint

        endpoint = NewWebhookEndpoint.from_api(
            {
                "id": "we_1",
                "url": "https://example.com/hooks",
                "description": "",
                "secret": "whsec_theactualsecret",
                "event_types": [],
                "disabled": False,
                "consecutive_failures": 0,
                "last_success_at": None,
                "last_error": None,
                "created_at": "2026-08-20T00:00:00Z",
            }
        )

        assert "theactualsecret" not in repr(endpoint)
        assert endpoint.secret == "whsec_theactualsecret"


class TestNotSent:
    """``delivery.not_sent`` — the field that tells a caller when to stop polling.

    Before it existed, ``delivered is False`` meant both *on its way* and *never attempted, and
    never will be*, so an integration waiting for a delivery that the server had already declined
    to attempt waited forever.
    """

    def test_absent_is_none_rather_than_a_missing_key(self) -> None:
        delivery = DeliveryStatus.from_api({"delivered": False, "opened": False, "bounced": False})
        assert delivery.not_sent is None

    def test_a_refusal_carries_its_reason_and_time(self) -> None:
        delivery = DeliveryStatus.from_api(
            {
                "delivered": False,
                "opened": False,
                "bounced": False,
                "not_sent": {"reason": "plan_lapsed", "at": "2026-09-22T10:00:00Z"},
            }
        )
        assert delivery.not_sent is not None
        assert delivery.not_sent.reason == EmailSkipReason.PLAN_LAPSED
        assert delivery.not_sent.at == datetime(2026, 9, 22, 10, 0, tzinfo=timezone.utc)

    def test_an_unknown_reason_parses_rather_than_raising(self) -> None:
        """The load-bearing one, and why ``reason`` is a ``str`` and not the enum.

        The server's list grows whenever a refusal is added to the send path. An SDK that raised
        on a value it had not heard of would turn an additive server change into an outage in a
        merchant's job runner — months after this SDK was last touched, and for a message it was
        being told about precisely because something needed attention.
        """
        delivery = DeliveryStatus.from_api(
            {
                "delivered": False,
                "opened": False,
                "bounced": False,
                "not_sent": {"reason": "some_future_reason", "at": None},
            }
        )
        assert delivery.not_sent is not None
        assert delivery.not_sent.reason == "some_future_reason"
        assert delivery.not_sent.at is None

    def test_the_enum_carries_the_wire_value(self) -> None:
        # ``EmailSkipReason`` is documentation and a comparison target, never the parsed type —
        # which is why the comparison that matters is the one in the test above, against a
        # ``reason`` that is typed ``str``. Asserting the literal directly against the member is
        # what mypy's ``--strict-equality`` rejects as non-overlapping, and it is right to: the
        # member is what a caller compares *to*, not what they compare.
        assert EmailSkipReason.DAILY_CAP_REACHED.value == "daily_cap_reached"
