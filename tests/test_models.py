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
    MessageKind,
    MessagePush,
    MessagePushResult,
    MessageStatus,
    RecipientStatus,
    SkipReason,
    SmsOutcome,
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
                "name": "orders",
                "key": "bbx_live_theactualsecret",
                "masked": "bbx_live_••••••••cret",
                "created": "2026-08-20T00:00:00Z",
            }
        )

        assert "theactualsecret" not in repr(key)
        assert key.key == "bbx_live_theactualsecret"

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
