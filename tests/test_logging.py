"""The logging contract a library owes its host application.

The rules are in :mod:`beaconbox._logging`. These tests exist because every one of them is
invisible when broken: an SDK that quietly calls ``basicConfig`` or logs a recipient's email
address does not fail any other test, it just does damage in somebody else's production logs.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from typing import Any

import pytest

from beaconbox import BeaconBox, RetryPolicy
from tests.conftest import API_KEY, PUSH_RESULT, Reply, make_client, no_sleep

PUSH: dict[str, Any] = {
    "recipient_email": "buyer@example.com",
    "subject": "Your order has shipped",
    "body": "Tracking XY123456789EE.",
}


class TestHygiene:
    def test_the_package_logger_has_a_null_handler(self) -> None:
        """So a warning emitted before the application configures logging does not land on
        stderr uninvited."""
        handlers = logging.getLogger("beaconbox").handlers

        assert any(isinstance(handler, logging.NullHandler) for handler in handlers)

    def test_importing_the_sdk_does_not_configure_the_root_logger(self) -> None:
        """A library that calls basicConfig() at import hijacks the logging of every application
        that imports it.

        In a subprocess, because pytest installs its own handlers on the root logger, so asserting
        in-process would only ever prove something about pytest.
        """
        script = (
            "import logging, sys; import beaconbox; "
            "sys.stdout.write(repr(logging.getLogger().handlers))"
        )
        result = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, check=True
        )

        assert result.stdout == "[]"

    def test_importing_the_sdk_prints_nothing(self) -> None:
        """Not even a warning. Importing a library is not an event the user asked to hear about."""
        result = subprocess.run(
            [sys.executable, "-c", "import beaconbox"], capture_output=True, text=True, check=True
        )

        assert result.stdout == ""
        assert result.stderr == ""

    def test_the_package_logger_sets_no_level(self) -> None:
        """NOTSET means the application decides. Anything else is the SDK overriding a choice
        that was not its to make."""
        assert logging.getLogger("beaconbox").level == logging.NOTSET

    def test_every_logger_lives_under_the_package_name(self) -> None:
        """One `logging.getLogger("beaconbox").setLevel(...)` has to reach all of it."""
        from beaconbox import _transport

        assert _transport.logger.name.startswith("beaconbox.")

    def test_installing_the_null_handler_twice_does_not_stack_them(self) -> None:
        from beaconbox._logging import install_null_handler

        before = len(logging.getLogger("beaconbox").handlers)
        install_null_handler()
        install_null_handler()

        assert len(logging.getLogger("beaconbox").handlers) == before


class TestNothingSensitiveIsLogged:
    """The rule with teeth. A log line is copied to an aggregator, retained for months, and read
    by people who were never meant to see a customer's address or a live credential."""

    def test_a_successful_push_logs_no_secret_and_no_personal_data(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        client, _ = make_client(Reply(201, PUSH_RESULT))

        with caplog.at_level(logging.DEBUG, logger="beaconbox"):
            client.messages.push(**PUSH)

        blob = _everything(caplog)
        assert API_KEY not in blob
        assert "Bearer" not in blob
        assert "buyer@example.com" not in blob, "a recipient's address reached a log line"
        assert "Tracking XY123456789EE" not in blob, "a message body reached a log line"

    def test_a_path_containing_an_email_is_logged_as_a_template(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """`/recipients/buyer@example.com/sms` is personal data in a URL. The route template is
        logged instead."""
        client, _ = make_client(
            Reply(
                200,
                {
                    "recipient_email": "buyer@example.com",
                    "phone": None,
                    "sms_status": "none",
                    "sms_status_at": None,
                },
            )
        )

        with caplog.at_level(logging.DEBUG, logger="beaconbox"):
            client.recipients.sms("buyer@example.com")

        blob = _everything(caplog)
        assert "buyer@example.com" not in blob
        assert "/recipients/{email}/sms" in blob

    def test_a_query_string_is_never_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        """`?recipient_email=` is the other place an address hides."""
        client, _ = make_client(Reply(200, {"items": [], "next_cursor": None}))

        with caplog.at_level(logging.DEBUG, logger="beaconbox"):
            client.messages.list(recipient_email="buyer@example.com")

        assert "buyer@example.com" not in _everything(caplog)

    def test_an_error_body_is_not_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        """The exception carries the body to the caller. A log line does not need a second copy,
        and error bodies can echo input."""
        from beaconbox import InvalidRequestError

        client, _ = make_client(
            Reply(422, {"error_code": "common.validation_failed", "detail": "buyer@example.com"})
        )

        with caplog.at_level(logging.DEBUG, logger="beaconbox"), pytest.raises(InvalidRequestError):
            client.messages.push(**PUSH)

        assert "buyer@example.com" not in _everything(caplog)


class TestWhatIsLogged:
    def test_silent_at_default_level(self, caplog: pytest.LogCaptureFixture) -> None:
        """A successful call says nothing unless somebody turned DEBUG on. An SDK that chatters
        at INFO is an SDK whose users filter it out, warnings included."""
        client, _ = make_client(Reply(201, PUSH_RESULT))

        with caplog.at_level(logging.INFO, logger="beaconbox"):
            client.messages.push(**PUSH)

        assert caplog.records == []

    def test_debug_records_the_request_and_the_response(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        client, _ = make_client(Reply(201, PUSH_RESULT))

        with caplog.at_level(logging.DEBUG, logger="beaconbox"):
            client.messages.push(**PUSH)

        messages = [record.getMessage() for record in caplog.records]
        assert any(m.startswith("request POST /messages") for m in messages)
        assert any("response POST /messages 201" in m for m in messages)

    def test_a_retry_warns_with_the_reason_and_the_backoff(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A retry means something went wrong, and it is what explains a job taking four seconds
        instead of one."""
        no_sleep(monkeypatch)
        client, _ = make_client(
            [Reply(503), Reply(201, PUSH_RESULT)], retry_policy=RetryPolicy(max_retries=1)
        )

        with caplog.at_level(logging.WARNING, logger="beaconbox"):
            client.messages.push(**PUSH)

        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "retrying POST /messages" in warnings[0]
        assert "HTTP 503" in warnings[0]

    def test_the_retry_log_names_the_key_it_reused(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """So an operator reading the logs can see the retry reused the key. A retry that minted
        a fresh one would be a duplicate message, and this is where that is visible."""
        no_sleep(monkeypatch)
        client, _ = make_client(
            [Reply(503), Reply(201, PUSH_RESULT)], retry_policy=RetryPolicy(max_retries=1)
        )

        with caplog.at_level(logging.WARNING, logger="beaconbox"):
            client.messages.push(**PUSH, idempotency_key="order-4711-shipped")

        record = next(r for r in caplog.records if r.levelno == logging.WARNING)
        assert record.beaconbox["idempotency_key"] == "order-4711-shipped"  # type: ignore[attr-defined]

    def test_giving_up_warns_once(
        self, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import httpx

        from beaconbox import APIConnectionError

        no_sleep(monkeypatch)
        client, _ = make_client(
            Reply(raise_error=httpx.ConnectError("refused")),
            retry_policy=RetryPolicy(max_retries=1),
        )

        with (
            caplog.at_level(logging.WARNING, logger="beaconbox"),
            pytest.raises(APIConnectionError),
        ):
            client.messages.push(**PUSH)

        messages = [r.getMessage() for r in caplog.records]
        assert sum(1 for m in messages if m.startswith("giving up")) == 1


def test_the_client_does_not_log_the_key_at_construction(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG, logger="beaconbox"):
        BeaconBox(API_KEY, base_url="https://api.beaconbox.test").close()

    assert API_KEY not in _everything(caplog)


def _everything(caplog: pytest.LogCaptureFixture) -> str:
    """Formatted message plus every structured field, because a leak can hide in either."""
    parts = []
    for record in caplog.records:
        parts.append(record.getMessage())
        parts.append(repr(getattr(record, "beaconbox", "")))
    return " ".join(parts)
