"""The transport-free core: request building, URL safety, error mapping.

These need no client and no fake network, which is the point of the module they cover.
"""

from __future__ import annotations

import json

import pytest

from beaconbox._core import (
    Call,
    Core,
    new_idempotency_key,
    path_segment,
    retry_after_seconds,
)
from beaconbox.errors import (
    AuthenticationError,
    ConflictError,
    InvalidRequestError,
    PermissionDeniedError,
    RateLimitError,
    ResourceMissingError,
    ServerError,
)

KEY = "bbx_live_0123456789abcdef0123456789abcdef"


def core(base_url: str = "https://api.beaconbox.test") -> Core:
    return Core(KEY, base_url=base_url)


def noop(body: object) -> object:
    return body


class TestPrepare:
    def test_authenticates_and_identifies_itself(self) -> None:
        request = core().prepare(Call("GET", "/credits", noop))

        assert request.headers["Authorization"] == f"Bearer {KEY}"
        assert request.headers["Accept"] == "application/json"
        assert request.headers["User-Agent"].startswith("beaconbox-python/")

    def test_user_agent_suffix_is_appended(self) -> None:
        prepared = Core(KEY, user_agent_suffix="acme-orders/2.1").prepare(
            Call("GET", "/credits", noop)
        )
        assert prepared.headers["User-Agent"].endswith(" acme-orders/2.1")

    def test_builds_the_versioned_url(self) -> None:
        request = core().prepare(Call("GET", "/messages", noop))
        assert request.url == "https://api.beaconbox.test/api/v1/messages"

    def test_drops_unset_query_parameters(self) -> None:
        """A `None` filter must vanish, not become `?cursor=None` and match nothing."""
        request = core().prepare(
            Call("GET", "/messages", noop, params={"limit": 50, "cursor": None})
        )
        assert request.url.endswith("/messages?limit=50")

    def test_encodes_json_body_and_sets_content_type(self) -> None:
        request = core().prepare(Call("POST", "/messages", noop, body={"subject": "hi"}))

        assert request.headers["Content-Type"] == "application/json"
        assert request.content is not None
        assert json.loads(request.content) == {"subject": "hi"}

    def test_no_content_type_without_a_body(self) -> None:
        request = core().prepare(Call("POST", "/messages/m_1/retract", noop))
        assert "Content-Type" not in request.headers
        assert request.content is None


class TestIdempotency:
    def test_every_post_carries_a_key(self) -> None:
        request = core().prepare(Call("POST", "/messages", noop, body={}))
        assert request.headers["Idempotency-Key"]

    def test_a_caller_key_is_used_verbatim(self) -> None:
        request = core().prepare(Call("POST", "/messages", noop, body={}), "order-4711-shipped")
        assert request.headers["Idempotency-Key"] == "order-4711-shipped"

    @pytest.mark.parametrize("method", ["GET", "PUT", "DELETE"])
    def test_nothing_else_carries_one(self, method: str) -> None:
        """PUT and DELETE on a phone number are idempotent by construction: setting a number
        twice leaves one number. Sending a message twice sends two."""
        request = core().prepare(Call(method, "/recipients/a@b.c/phone", noop))
        assert "Idempotency-Key" not in request.headers

    def test_generated_keys_are_unique(self) -> None:
        assert len({new_idempotency_key() for _ in range(1000)}) == 1000


class TestBaseUrl:
    def test_trailing_slash_does_not_double_up(self) -> None:
        prepared = core("https://api.beaconbox.test/").prepare(Call("GET", "/credits", noop))
        assert prepared.url == "https://api.beaconbox.test/api/v1/credits"

    def test_plain_http_to_a_remote_host_is_refused(self) -> None:
        """Otherwise a typo in configuration puts a live API key on the wire in clear."""
        with pytest.raises(ValueError, match="plain http"):
            core("http://api.beaconbox.test")

    @pytest.mark.parametrize("url", ["http://localhost:8000", "http://127.0.0.1:8000"])
    def test_plain_http_to_loopback_is_allowed(self, url: str) -> None:
        assert core(url).base_url == url

    @pytest.mark.parametrize("url", ["ftp://api.beaconbox.test", "api.beaconbox.test", ""])
    def test_a_url_that_is_not_a_url_is_refused(self, url: str) -> None:
        with pytest.raises(ValueError):
            core(url)

    def test_an_empty_api_key_is_refused_at_construction(self) -> None:
        """Not at the first request, where it would surface as a confusing 401."""
        with pytest.raises(ValueError, match="API key is required"):
            Core("   ")


class TestPathSegment:
    def test_encodes_an_email_address(self) -> None:
        assert path_segment("buyer+tag@example.com") == "buyer%2Btag%40example.com"

    def test_a_slash_cannot_escape_its_segment(self) -> None:
        """Otherwise a crafted id could address a different endpoint entirely."""
        assert path_segment("../../admin/keys") == "..%2F..%2Fadmin%2Fkeys"


class TestErrorMapping:
    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (401, AuthenticationError),
            (403, PermissionDeniedError),
            (404, ResourceMissingError),
            (409, ConflictError),
            (422, InvalidRequestError),
            (400, InvalidRequestError),
            (429, RateLimitError),
            (500, ServerError),
            (503, ServerError),
        ],
    )
    def test_status_selects_the_exception(self, status: int, expected: type[Exception]) -> None:
        with pytest.raises(expected):
            core().interpret(Call("GET", "/x", noop), status, {}, b'{"error_code":"x.y"}')

    def test_carries_the_machine_readable_code(self) -> None:
        with pytest.raises(ResourceMissingError) as caught:
            core().interpret(
                Call("GET", "/x", noop), 404, {}, b'{"error_code":"message.not_found"}'
            )

        assert caught.value.error_code == "message.not_found"
        assert caught.value.status_code == 404
        assert caught.value.body == {"error_code": "message.not_found"}

    def test_captures_a_request_id_when_one_is_sent(self) -> None:
        """The one thing support needs and a caller never thinks to record."""
        with pytest.raises(ServerError) as caught:
            core().interpret(Call("GET", "/x", noop), 500, {"X-Request-Id": "req_9"}, b"{}")

        assert caught.value.request_id == "req_9"

    def test_a_non_json_error_body_still_raises_the_right_class(self) -> None:
        """An HTML 502 from a proxy in front of the API must not become a JSON parse error."""
        with pytest.raises(ServerError) as caught:
            core().interpret(Call("GET", "/x", noop), 502, {}, b"<html>bad gateway</html>")

        assert "bad gateway" in caught.value.body["raw"]


class TestInterpret:
    def test_an_empty_body_parses(self) -> None:
        """A 204 answers with nothing, and that is a success."""
        assert core().interpret(Call("DELETE", "/keys/k_1", lambda _: "ok"), 204, {}, b"") == "ok"

    def test_parses_with_the_calls_own_parser(self) -> None:
        call = Call("GET", "/credits", lambda body: body["balance"])
        assert core().interpret(call, 200, {}, b'{"balance": 42}') == 42


class TestRetryAfter:
    def test_reads_delta_seconds(self) -> None:
        assert retry_after_seconds({"Retry-After": "12"}) == 12.0

    def test_is_case_insensitive(self) -> None:
        assert retry_after_seconds({"retry-after": "3"}) == 3.0

    @pytest.mark.parametrize("value", ["Wed, 21 Oct 2026 07:28:00 GMT", "soon", "-5", ""])
    def test_anything_it_cannot_trust_falls_back_to_our_own_backoff(self, value: str) -> None:
        assert retry_after_seconds({"Retry-After": value}) is None

    def test_absent_header(self) -> None:
        assert retry_after_seconds({}) is None


def test_core_repr_never_contains_the_key() -> None:
    """It ends up in tracebacks and log lines."""
    assert KEY not in repr(core())
