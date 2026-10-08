"""Every operation: the right verb, the right path, the right body.

These are the tests that catch a drift between the SDK and the API without a server. The batch
one earned its place: an earlier SDK sent the list under `messages` rather than `items`, which
unit tests that only asserted "a POST happened" were happy with and a real server answered 422.
"""

from __future__ import annotations

import inspect
import json
from typing import ClassVar
from urllib.parse import parse_qs, urlsplit

import pytest

from beaconbox import (
    BeaconBoxError,
    ConflictError,
    MessageKind,
    MessagePush,
    OrderStatus,
    ResourceMissingError,
    RetryPolicy,
    SkipReason,
    async_resources,
)
from tests.conftest import MESSAGE_VIEW, PUSH_RESULT, Reply, make_client


def query(url: object) -> dict[str, list[str]]:
    return parse_qs(urlsplit(str(url)).query)


class TestPush:
    def test_posts_the_required_fields(self) -> None:
        client, recorder = make_client(Reply(201, PUSH_RESULT))

        client.messages.push(
            recipient_email="buyer@example.com",
            subject="Your order has shipped",
            body="Tracking XY123456789EE.",
        )

        assert recorder.only.method == "POST"
        assert str(recorder.only.url).endswith("/api/v1/messages")
        assert recorder.body() == {
            "recipient_email": "buyer@example.com",
            "subject": "Your order has shipped",
            "body": "Tracking XY123456789EE.",
            "kind": "one_off",
        }

    def test_parses_the_email_block(self) -> None:
        """Per-channel, which is the whole reason it is its own block: a bounce suppression must
        never surface as an SMS reason, nor an SMS STOP as an email one."""
        client, _ = make_client(
            Reply(201, {**PUSH_RESULT, "email": {"sending": False, "skipped_reason": "suppressed"}})
        )

        result = client.messages.push(recipient_email="a@b.c", subject="s", body="b")

        assert result.email is not None
        assert result.email.sending is False
        assert result.email.skipped_reason == "suppressed"

    def test_email_block_is_none_when_nothing_was_nudged(self) -> None:
        """``notify=False`` queues no email, so there is no email outcome. ``None`` rather than a
        block saying "not sending": nothing was refused, nothing was asked for."""
        client, _ = make_client(Reply(201, {**PUSH_RESULT, "email": None}))

        result = client.messages.push(recipient_email="a@b.c", subject="s", body="b", notify=False)

        assert result.email is None

    def test_email_block_survives_a_reason_the_sdk_has_never_heard_of(self) -> None:
        """The vocabulary grows server-side. An unknown reason must arrive intact rather than
        raising or being dropped — a merchant branching on it can then treat it as "not sent",
        which is what the API documents."""
        client, _ = make_client(
            Reply(
                201,
                {**PUSH_RESULT, "email": {"sending": False, "skipped_reason": "invented_later"}},
            )
        )

        result = client.messages.push(recipient_email="a@b.c", subject="s", body="b")

        assert result.email is not None
        assert result.email.skipped_reason == "invented_later"

    def test_omits_optional_fields_rather_than_sending_null(self) -> None:
        """`notify=None` means "apply the default". `notify=false` actively suppresses a nudge.
        Sending null for the first would ask for the second."""
        client, recorder = make_client(Reply(201, PUSH_RESULT))

        client.messages.push(recipient_email="a@b.c", subject="s", body="b")

        assert "notify" not in recorder.body()
        assert "send_at" not in recorder.body()
        assert "channels" not in recorder.body()

    def test_sends_notify_false_when_it_is_asked_for(self) -> None:
        client, recorder = make_client(Reply(201, PUSH_RESULT))

        client.messages.push(recipient_email="a@b.c", subject="s", body="b", notify=False)

        assert recorder.body()["notify"] is False

    def test_accepts_enums_and_plain_strings_alike(self) -> None:
        client, recorder = make_client(Reply(201, PUSH_RESULT))

        client.messages.push(
            recipient_email="a@b.c",
            subject="s",
            body="b",
            kind=MessageKind.UPDATEABLE,
            channels=["sms"],
        )

        assert recorder.body()["kind"] == "updateable"
        assert recorder.body()["channels"] == ["sms"]

    def test_order_status_accepts_the_enum_or_a_plain_string(self) -> None:
        client, recorder = make_client([Reply(201, PUSH_RESULT), Reply(201, PUSH_RESULT)])

        client.messages.push(
            recipient_email="a@b.c",
            subject="s",
            body="b",
            reference="#A-10294",
            order_status=OrderStatus.SHIPPED,
        )
        client.messages.push(
            recipient_email="a@b.c", subject="s", body="b", order_status="out_for_delivery"
        )

        assert recorder.body(0)["order_status"] == "shipped"
        assert recorder.body(1)["order_status"] == "out_for_delivery"

    def test_order_status_is_omitted_when_unset(self) -> None:
        client, recorder = make_client(Reply(201, PUSH_RESULT))

        client.messages.push(recipient_email="a@b.c", subject="s", body="b")

        assert "order_status" not in recorder.body()

    def test_a_batch_item_sends_its_order_status(self) -> None:
        client, recorder = make_client(Reply(200, {"items": [], "succeeded": 0, "failed": 0}))

        client.messages.push_batch(
            [
                MessagePush("a@b.c", "s", "b", order_status=OrderStatus.DELIVERED),
                MessagePush("d@e.f", "s", "b"),
            ]
        )

        items = recorder.body()["items"]
        assert items[0]["order_status"] == "delivered"
        assert "order_status" not in items[1]

    def test_whatsapp_opt_in_becomes_the_object_the_api_requires(self) -> None:
        """The API refuses a bare boolean on purpose: a copied `true` is not evidence of
        anything, and the source has to be a surface the merchant authored."""
        client, recorder = make_client(Reply(201, PUSH_RESULT))

        client.messages.push(
            recipient_email="a@b.c",
            subject="s",
            body="b",
            whatsapp_opt_in_source="checkout tickbox",
        )

        assert recorder.body()["whatsapp_opt_in"] == {"source": "checkout tickbox"}

    def test_a_skipped_channel_does_not_raise(self) -> None:
        """The push succeeded. The update is in the inbox and the email went."""
        client, _ = make_client(Reply(201, PUSH_RESULT))

        result = client.messages.push(recipient_email="a@b.c", subject="s", body="b")

        assert result.sms is not None
        assert result.sms.queued is False
        assert result.sms.skipped_reason == SkipReason.INSUFFICIENT_CREDIT

    @pytest.mark.parametrize("reason", ["sms_disabled", "sms_not_enabled"])
    def test_naming_a_channel_cannot_switch_it_on(self, reason: str) -> None:
        """``channels=["sms"]`` on an account with SMS off, or not enabled yet, is still a 201 and
        reports the reason rather than raising."""
        client, recorder = make_client(
            Reply(
                201,
                {**PUSH_RESULT, "sms": {"queued": False, "credits": 0, "skipped_reason": reason}},
            )
        )

        result = client.messages.push(
            recipient_email="buyer@example.com",
            subject="Shipped",
            body="Tracking XY123456789EE.",
            channels=["sms"],
        )

        assert recorder.body()["channels"] == ["sms"]
        assert result.sms is not None
        assert result.sms.queued is False
        assert result.sms.skipped_reason in (SkipReason.SMS_DISABLED, SkipReason.SMS_NOT_ENABLED)


class TestBatch:
    def test_sends_the_list_under_items(self) -> None:
        """`items` is what MessageBatchPush declares. Any other key is a 422 that only shows up
        against a real server."""
        client, recorder = make_client(Reply(200, {"items": [], "succeeded": 0, "failed": 0}))

        client.messages.push_batch([MessagePush(recipient_email="a@b.c", subject="s", body="b")])

        body = recorder.body()
        assert list(body) == ["items"]
        assert body["items"][0]["recipient_email"] == "a@b.c"

    def test_reports_failures_as_data(self) -> None:
        client, _ = make_client(
            Reply(
                200,
                {
                    "items": [
                        {"index": 0, "ok": True, "result": PUSH_RESULT},
                        {"index": 1, "ok": False, "error_code": "common.validation_failed"},
                    ],
                    "succeeded": 1,
                    "failed": 1,
                },
            )
        )

        result = client.messages.push_batch(
            [
                MessagePush(recipient_email="a@b.c", subject="s", body="b"),
                MessagePush(recipient_email="bad", subject="s", body="b"),
            ]
        )

        assert result.succeeded == 1
        assert [item.index for item in result.failures] == [1]
        assert result.failures[0].error_code == "common.validation_failed"
        assert result.items[0].result is not None
        assert result.items[0].result.id == "m_8sKq2Vd1"

    def test_carries_one_idempotency_key_for_the_batch(self) -> None:
        client, recorder = make_client(Reply(200, {"items": [], "succeeded": 0, "failed": 0}))

        client.messages.push_batch([], idempotency_key="nightly-2026-08-20")

        assert recorder.only.headers["Idempotency-Key"] == "nightly-2026-08-20"

    def test_a_batch_still_running_under_the_key_raises_and_is_not_retried(self) -> None:
        """The whole batch's 409, never an item's result. Retrying it inside the SDK would only
        hit the same live run, so it is the caller's to retry later with the same key."""
        client, recorder = make_client(
            [
                Reply(409, {"error_code": "idempotency.request_in_progress"}),
                Reply(200, {"items": [], "succeeded": 0, "failed": 0}),
            ],
            retry_policy=RetryPolicy(max_retries=3, base_delay=0, max_delay=0),
        )

        with pytest.raises(ConflictError) as caught:
            client.messages.push_batch(
                [MessagePush(recipient_email="a@b.c", subject="s", body="b")],
                idempotency_key="nightly-2026-08-20",
            )

        assert caught.value.error_code == "idempotency.request_in_progress"
        assert recorder.only.headers["Idempotency-Key"] == "nightly-2026-08-20"


class TestMessageReads:
    def test_get(self) -> None:
        client, recorder = make_client(Reply(200, MESSAGE_VIEW))

        message = client.messages.get("m_8sKq2Vd1")

        assert recorder.only.method == "GET"
        assert str(recorder.only.url).endswith("/api/v1/messages/m_8sKq2Vd1")
        assert message.delivery.delivered is True
        assert message.delivery.sms is not None
        assert message.delivery.sms.credits_charged == 1

    def test_get_of_another_businesss_id_is_a_404(self) -> None:
        """Deliberately indistinguishable from "does not exist": a probe must not be able to
        tell the two apart."""
        client, _ = make_client(Reply(404, {"error_code": "message.not_found"}))

        with pytest.raises(ResourceMissingError):
            client.messages.get("m_someone_elses")

    def test_list_passes_filters_and_drops_unset_ones(self) -> None:
        client, recorder = make_client(Reply(200, {"items": [], "next_cursor": None}))

        client.messages.list(recipient_email="buyer@example.com", limit=10)

        assert query(recorder.only.url) == {
            "recipient_email": ["buyer@example.com"],
            "limit": ["10"],
        }

    def test_iterate_follows_cursors_to_the_end(self) -> None:
        client, recorder = make_client(
            [
                Reply(200, {"items": [MESSAGE_VIEW], "next_cursor": "cur_2"}),
                Reply(200, {"items": [MESSAGE_VIEW], "next_cursor": None}),
            ]
        )

        messages = list(client.messages.iterate())

        assert len(messages) == 2
        assert query(recorder.requests[0].url).get("cursor") is None
        assert query(recorder.requests[1].url)["cursor"] == ["cur_2"]

    def test_iterate_stops_on_an_empty_final_page(self) -> None:
        client, recorder = make_client(Reply(200, {"items": [], "next_cursor": None}))

        assert list(client.messages.iterate()) == []
        assert len(recorder.requests) == 1

    def test_iterate_is_lazy(self) -> None:
        """A merchant with a year of history should not have to hold it in memory to count it."""
        client, recorder = make_client(
            Reply(200, {"items": [MESSAGE_VIEW], "next_cursor": "cur_next"})
        )

        iterator = client.messages.iterate()
        assert recorder.requests == []
        next(iterator)
        assert len(recorder.requests) == 1


class TestMessageWrites:
    def test_resend_sms(self) -> None:
        client, recorder = make_client(Reply(200, {"queued": True, "credits": 1}))

        outcome = client.messages.resend_sms("m_8sKq2Vd1")

        assert recorder.only.method == "POST"
        assert str(recorder.only.url).endswith("/messages/m_8sKq2Vd1/sms")
        assert outcome.queued is True
        assert recorder.only.headers["Idempotency-Key"]

    def test_resend_sms_conflicts_when_one_already_went(self) -> None:
        """A second send would add a second charge and a second interruption, nothing else."""
        client, _ = make_client(Reply(409, {"error_code": "sms.already_sent"}))

        with pytest.raises(ConflictError):
            client.messages.resend_sms("m_8sKq2Vd1")

    def test_resend_while_a_send_is_still_queued_is_a_conflict(self) -> None:
        client, _ = make_client(
            Reply(
                409,
                {"error_code": "sms.already_sent", "detail": "already queued"},
                headers={"X-Request-Id": "abc123"},
            )
        )

        with pytest.raises(ConflictError) as caught:
            client.messages.resend_sms("m_1")

        assert caught.value.error_code == "sms.already_sent"
        assert caught.value.request_id == "abc123"

    def test_resend_whatsapp(self) -> None:
        client, recorder = make_client(
            Reply(200, {"queued": True, "credits": 1, "sms_fallback": True})
        )

        outcome = client.messages.resend_whatsapp("m_8sKq2Vd1")

        assert str(recorder.only.url).endswith("/messages/m_8sKq2Vd1/whatsapp")
        assert outcome.sms_fallback is True

    def test_retract(self) -> None:
        client, recorder = make_client(
            Reply(
                200,
                {
                    "id": "m_8sKq2Vd1",
                    "status": "retracted",
                    "retracted": True,
                    "sends_cancelled": 1,
                    "already_notified": False,
                },
            )
        )

        result = client.messages.retract("m_8sKq2Vd1")

        assert str(recorder.only.url).endswith("/messages/m_8sKq2Vd1/retract")
        assert result.retracted is True
        assert result.sends_cancelled == 1

    def test_a_second_retraction_is_not_a_failure(self) -> None:
        client, _ = make_client(
            Reply(
                200,
                {
                    "id": "m_8sKq2Vd1",
                    "status": "retracted",
                    "retracted": False,
                    "sends_cancelled": 0,
                    "already_notified": True,
                },
            )
        )

        result = client.messages.retract("m_8sKq2Vd1")

        assert result.retracted is False
        assert result.already_notified is True


class TestCredits:
    def test_balance(self) -> None:
        client, recorder = make_client(
            Reply(200, {"balance": 3, "low_balance": True, "threshold": 10})
        )

        balance = client.credits.balance()

        assert str(recorder.only.url).endswith("/api/v1/credits")
        assert (balance.balance, balance.low_balance) == (3, True)


class TestRecipients:
    RECIPIENT: ClassVar[dict[str, object]] = {
        "recipient_email": "buyer@example.com",
        "phone": "+372 •••• 0134",
        "sms_status": "active",
        "sms_status_at": "2026-08-20T09:00:00Z",
    }

    def test_read_returns_a_masked_number(self) -> None:
        """A leaked key must not be usable to dump a phone book."""
        client, recorder = make_client(Reply(200, self.RECIPIENT))

        recipient = client.recipients.sms("buyer@example.com")

        assert str(recorder.only.url).endswith("/recipients/buyer%40example.com/sms")
        assert recipient.phone == "+372 •••• 0134"

    def test_set_phone_puts_without_an_idempotency_key(self) -> None:
        """Setting a number twice leaves one number."""
        client, recorder = make_client(Reply(200, self.RECIPIENT))

        client.recipients.set_phone("buyer@example.com", "+37255550134")

        assert recorder.only.method == "PUT"
        assert json.loads(recorder.only.content) == {"phone": "+37255550134"}
        assert "Idempotency-Key" not in recorder.only.headers

    def test_clear_phone(self) -> None:
        client, recorder = make_client(Reply(200, {**self.RECIPIENT, "phone": None}))

        assert client.recipients.clear_phone("buyer@example.com").phone is None
        assert recorder.only.method == "DELETE"

    def test_erase_whatsapp_returns_the_receipt(self) -> None:
        client, recorder = make_client(
            Reply(
                200,
                {
                    "replies_deleted": 4,
                    "webhook_payloads_scrubbed": 2,
                    "auto_reply_windows_dropped": 1,
                    "consent_withdrawn": True,
                    "replies_a_forward_email_may_have_carried": 3,
                    "forward_setting": "full",
                    "first_erased_at": "2026-08-20T10:00:00Z",
                },
            )
        )

        receipt = client.recipients.erase_whatsapp("buyer@example.com")

        assert recorder.only.method == "POST"
        assert str(recorder.only.url).endswith("/recipients/buyer%40example.com/whatsapp/erase")
        assert receipt.consent_withdrawn is True
        assert receipt.replies_a_forward_email_may_have_carried == 3

    def test_erase_posts_with_a_key_and_returns_the_report(self) -> None:
        client, recorder = make_client(
            Reply(
                200,
                {
                    "messages_deleted": 2,
                    "message_events_deleted": 5,
                    "channel_sends_deleted": 1,
                    "message_links_deleted": 2,
                    "whatsapp_replies_deleted": 0,
                    "webhook_payloads_scrubbed": 1,
                    "queued_jobs_cancelled": 0,
                    "idempotency_records_deleted": 2,
                    "audit_entries_pseudonymised": 1,
                    "subscription_deleted": True,
                    "stops_kept": 1,
                    "suppressions_kept": 0,
                    "auto_reply_windows_dropped": 0,
                    "replies_a_forward_email_may_have_carried": 0,
                },
            )
        )

        report = client.recipients.erase("buyer+tag@example.com")

        assert recorder.only.method == "POST"
        assert str(recorder.only.url).endswith("/recipients/buyer%2Btag%40example.com/erase")
        assert recorder.only.headers.get("Idempotency-Key")
        assert report.messages_deleted == 2
        assert report.stops_kept == 1
        assert report.subscription_deleted is True

    def test_erase_forwards_a_caller_key(self) -> None:
        """Retrying with the same key is how a timed-out erasure gets its original counts back."""
        client, recorder = make_client(Reply(200, {}))

        client.recipients.erase("buyer@example.com", idempotency_key="erase-buyer-1")

        assert recorder.only.headers["Idempotency-Key"] == "erase-buyer-1"

    def test_erase_cannot_be_steered_to_another_route(self) -> None:
        client, recorder = make_client(Reply(200, {}))

        client.recipients.erase("a/../../keys@example.com")

        assert recorder.only.url.raw_path.decode().endswith(
            "/recipients/a%2F..%2F..%2Fkeys%40example.com/erase"
        )

    @pytest.mark.parametrize(
        "email",
        ["", "   ", "buyer", "@example.com", "buyer@", "buyer@localhost", "a b@example.com"],
    )
    @pytest.mark.parametrize("method", ["erase", "erase_whatsapp"])
    def test_erase_refuses_a_value_that_is_not_an_address(self, method: str, email: str) -> None:
        """The API answers 422 for these anyway. An erasure is the call where a slip should fail
        at the caller's line, before anything is sent."""
        client, recorder = make_client(Reply(200, {}))

        with pytest.raises(ValueError, match="email address") as caught:
            getattr(client.recipients, method)(email)

        assert recorder.requests == []
        if email.strip():
            assert email not in str(caught.value)

    def test_erase_trims_surrounding_whitespace(self) -> None:
        client, recorder = make_client(Reply(200, {}))

        client.recipients.erase(" buyer@example.com\n")

        assert str(recorder.only.url).endswith("/recipients/buyer%40example.com/erase")


class TestKeys:
    def test_list_unwraps_items(self) -> None:
        client, _ = make_client(
            Reply(
                200,
                {
                    "items": [
                        {
                            "id": "k_1",
                            "name": "orders",
                            "masked": "bbx_live_••••••••cdef",
                            "created": "2026-08-01T00:00:00Z",
                        }
                    ]
                },
            )
        )

        keys = client.keys.list()

        assert len(keys) == 1
        assert keys[0].id == "k_1"
        assert keys[0].minted_by is None

    def test_list_carries_the_minting_key(self) -> None:
        client, _ = make_client(
            Reply(
                200,
                {
                    "items": [
                        {
                            "id": "k_2",
                            "name": "planted",
                            "masked": "bbx_live_••••••••abcd",
                            "created": "2026-08-01T00:00:00Z",
                            "minted_by": "k_1",
                        }
                    ]
                },
            )
        )

        (key,) = client.keys.list()

        assert key.minted_by == "k_1"

    def test_create(self) -> None:
        client, recorder = make_client(
            Reply(
                201,
                {
                    "id": "k_7Hq2",
                    "name": "orders",
                    "key": "bbx_live_secret",
                    "masked": "bbx_live_••••••••cret",
                    "created": "2026-08-20T00:00:00Z",
                },
            )
        )

        key = client.keys.create("orders")

        assert json.loads(recorder.only.content) == {"name": "orders"}
        assert key.key == "bbx_live_secret"
        # The id is what `revoke` takes. Without it a caller had to list and match on `name`, which
        # is ambiguous: a name is not unique and defaults to "Untitled key".
        assert key.id == "k_7Hq2"

    def test_detach_is_sent_only_when_asked_for(self) -> None:
        reply = {
            "id": "k_8",
            "name": "rotation",
            "key": "bbx_live_new",
            "masked": "bbx_live_••••••••_new",
            "created": "2026-10-07T00:00:00Z",
        }
        client, recorder = make_client(Reply(201, reply))

        client.keys.create("rotation", detach=True)

        assert json.loads(recorder.only.content) == {"name": "rotation", "detach": True}

    def test_revoke_handles_a_204(self) -> None:
        client, recorder = make_client(Reply(204))

        client.keys.revoke("k_1")
        assert recorder.only.method == "DELETE"


class TestWebhookEndpoints:
    ENDPOINT: ClassVar[dict[str, object]] = {
        "id": "we_1",
        "url": "https://example.com/hooks",
        "description": "",
        "secret": "whsec_real_secret",
        "event_types": [],
        "disabled": False,
        "consecutive_failures": 0,
        "last_success_at": None,
        "last_error": None,
        "created_at": "2026-08-20T00:00:00Z",
    }

    def test_create_defaults_to_every_event_type(self) -> None:
        """A narrow subscription is how a new event type silently passes an integration by."""
        client, recorder = make_client(Reply(201, self.ENDPOINT))

        endpoint = client.webhook_endpoints.create("https://example.com/hooks")

        assert json.loads(recorder.only.content) == {
            "url": "https://example.com/hooks",
            "event_types": [],
            "description": "",
        }
        assert endpoint.secret == "whsec_real_secret"

    def test_create_accepts_enums(self) -> None:
        from beaconbox import WebhookEventType

        client, recorder = make_client(Reply(201, self.ENDPOINT))

        client.webhook_endpoints.create(
            "https://example.com/hooks",
            [WebhookEventType.MESSAGE_BOUNCED, "sms.failed"],
            description="orders",
        )

        body = json.loads(recorder.only.content)
        assert body["event_types"] == ["message.bounced", "sms.failed"]
        assert body["description"] == "orders"

    def test_list_surfaces_a_disabled_endpoint(self) -> None:
        """A silent endpoint looks exactly like nothing having happened."""
        client, _ = make_client(
            Reply(200, {"items": [{**self.ENDPOINT, "disabled": True, "last_error": "timeout"}]})
        )

        endpoints = client.webhook_endpoints.list()

        assert endpoints[0].disabled is True
        assert endpoints[0].last_error == "timeout"

    def test_delete(self) -> None:
        client, recorder = make_client(Reply(204))

        client.webhook_endpoints.delete("we_1")
        assert recorder.only.method == "DELETE"

    def test_test_reports_a_working_endpoint(self) -> None:
        client, recorder = make_client(
            Reply(200, {"delivered": True, "status_code": 200, "error": None, "duration_ms": 42})
        )

        result = client.webhook_endpoints.test("we_1")

        assert recorder.only.method == "POST"
        assert recorder.only.url.path.endswith("/webhook-endpoints/we_1/test")
        assert result.delivered is True
        assert result.status_code == 200
        assert result.duration_ms == 42

    def test_test_does_not_raise_when_the_receiver_is_broken(self) -> None:
        """**The property that makes this usable.** The failure is at the caller's own server, so
        a raise would describe the wrong hop — and a merchant debugging their handler would be
        fighting an exception instead of reading a status code."""
        client, _ = make_client(
            Reply(
                200,
                {
                    "delivered": False,
                    "status_code": 500,
                    "error": "The endpoint answered HTTP 500.",
                    "duration_ms": 17,
                },
            )
        )

        result = client.webhook_endpoints.test("we_1")

        assert result.delivered is False
        assert result.status_code == 500
        assert "500" in (result.error or "")

    def test_test_reports_an_unreachable_endpoint(self) -> None:
        client, _ = make_client(
            Reply(
                200,
                {"delivered": False, "status_code": None, "error": "no route", "duration_ms": 0},
            )
        )

        result = client.webhook_endpoints.test("we_1")

        assert result.delivered is False
        assert result.status_code is None


class TestPaginationCannotHang:
    """A server that hands back a cursor it already gave would spin `iterate` forever, yielding
    the same page over and over. That is a server bug, but its shape in a merchant's process is a
    worker that never returns, which is far harder to diagnose than an exception."""

    def test_a_repeated_cursor_raises_instead_of_looping(self) -> None:
        client, recorder = make_client(
            Reply(200, {"items": [MESSAGE_VIEW], "next_cursor": "stuck"})
        )

        with pytest.raises(BeaconBoxError, match="did not advance"):
            list(client.messages.iterate())

        # Two pages fetched, then it stopped: the first `stuck`, and the repeat that proved it.
        assert len(recorder.requests) == 2

    def test_distinct_cursors_are_followed_normally(self) -> None:
        client, _ = make_client(
            [
                Reply(200, {"items": [MESSAGE_VIEW], "next_cursor": "a"}),
                Reply(200, {"items": [MESSAGE_VIEW], "next_cursor": "b"}),
                Reply(200, {"items": [MESSAGE_VIEW], "next_cursor": None}),
            ]
        )

        assert len(list(client.messages.iterate())) == 3

    def test_an_empty_cursor_string_ends_iteration(self) -> None:
        """`""` is not a cursor. Treating it as one would fetch page one forever."""
        client, recorder = make_client(Reply(200, {"items": [MESSAGE_VIEW], "next_cursor": ""}))

        assert len(list(client.messages.iterate())) == 1
        assert len(recorder.requests) == 1

    @pytest.mark.parametrize("page_size", [0, -1])
    def test_a_page_size_below_one_is_refused(self, page_size: int) -> None:
        """The server clamps `limit` into its own range, so this is not an error there — it
        quietly becomes one message per request, and a merchant walking a year of history meets
        that as a job that takes hours rather than as a mistake in their call."""
        client, recorder = make_client(Reply(200, {"items": [], "next_cursor": None}))

        with pytest.raises(ValueError, match="page_size must be at least 1"):
            list(client.messages.iterate(page_size=page_size))

        assert recorder.requests == []

    def test_the_async_client_does_not_carry_its_own_copy_of_the_rule(self) -> None:
        """Both `iterate` generators go through `_PageWalk`, so the check and the sentence
        explaining it exist once. Written out twice, a safety check gets improved in one copy."""
        source = inspect.getsource(async_resources)

        assert "_PageWalk" in source
        assert "did not advance" not in source
