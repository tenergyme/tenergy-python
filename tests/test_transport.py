"""The default stdlib transport against a real local HTTP server: no mocks between the client and
the socket, so what is signed is checked against what actually went over the wire."""
from __future__ import annotations

import socket

import pytest
from local_server import Recorder, ok

from tenergy import Client, TenergyError, TenergyTransportError, sign_request

KEY = "ak_live_test"
SECRET = "sk_test_example"


def test_signed_post_on_the_wire_verifies(server: str) -> None:
    Recorder.script = [ok({"id": "ord_1"}, 201)]
    client = Client(KEY, SECRET, server, now=lambda: 1_789_000_000_000)
    body = {"resource": "energy", "amount": 65000, "tier": "1h", "receiver": "T…", "memo": "энергия"}
    assert client.create_order(body, idempotency_key="idem-1") == {"id": "ord_1"}
    sent = Recorder.seen[0]
    headers = sent["headers"]
    assert isinstance(headers, dict) and isinstance(sent["body"], bytes)
    assert sent["method"] == "POST" and sent["path"] == "/v1/orders"
    assert headers["content-type"] == "application/json"
    assert headers["idempotency-key"] == "idem-1"
    assert headers["user-agent"].startswith("tenergy-sdk-py/")
    # The server recomputes the signature from the bytes it received — the api's own check.
    expected = sign_request(SECRET, headers["x-api-timestamp"], "POST", "/v1/orders", "", sent["body"].decode("utf-8"))
    assert headers["x-api-sign"] == expected


def test_query_string_is_sent_exactly_as_signed(server: str) -> None:
    Recorder.script = [ok({"data": []})]
    Client(KEY, SECRET, server).list_orders(status=["active", "paid"], limit=20)
    sent = Recorder.seen[0]
    headers = sent["headers"]
    assert isinstance(headers, dict)
    assert sent["path"] == "/v1/orders?status=active&status=paid&limit=20"
    expected = sign_request(SECRET, headers["x-api-timestamp"], "GET", "/v1/orders", "status=active&status=paid&limit=20", "")
    assert headers["x-api-sign"] == expected


def test_error_status_is_read_as_an_envelope_not_a_transport_failure(server: str) -> None:
    envelope = {"error": {"code": 4001, "slug": "insufficient_funds", "message": "Top up"}, "request_id": "req_9"}
    Recorder.script = [ok(envelope, 402)]
    with pytest.raises(TenergyError) as caught:
        Client(KEY, SECRET, server).create_order({"resource": "energy"})
    assert (caught.value.slug, caught.value.http_status, caught.value.request_id) == ("insufficient_funds", 402, "req_9")


def test_get_retries_a_503_honouring_retry_after(server: str) -> None:
    busy = {"error": {"code": 9000, "slug": "internal_error", "message": "x", "retryable": True}}
    Recorder.script = [ok(busy, 503, {"retry-after": "1"}), ok({"balance_sun": 5})]
    slept: list[float] = []
    client = Client(KEY, SECRET, server, sleep=slept.append)
    assert client.get_balance() == {"balance_sun": 5}
    assert slept == [1.0] and len(Recorder.seen) == 2
    # Each attempt is signed afresh (a reused signature is refused as a replay).
    first, second = (seen["headers"] for seen in Recorder.seen)
    assert isinstance(first, dict) and isinstance(second, dict)
    assert "x-api-sign" in first and "x-api-sign" in second


def test_post_is_never_retried(server: str) -> None:
    busy = {"error": {"code": 9000, "slug": "internal_error", "message": "x", "retryable": True}}
    Recorder.script = [ok(busy, 503)]
    with pytest.raises(TenergyError):
        Client(KEY, SECRET, server, sleep=lambda _: None).create_order({"resource": "energy"})
    assert len(Recorder.seen) == 1


def test_redirect_is_surfaced_not_followed(server: str) -> None:
    Recorder.script = [(301, b"", {"location": "https://example.com/v1/balance"})]
    with pytest.raises(TenergyTransportError) as caught:
        Client(KEY, SECRET, server).get_balance()
    assert caught.value.http_status == 301 and len(Recorder.seen) == 1


def test_non_json_body_is_a_transport_error(server: str) -> None:
    Recorder.script = [(502, b"<html>bad gateway</html>", {"content-type": "text/html"})] * 3
    with pytest.raises(TenergyTransportError) as caught:
        Client(base_url=server, sleep=lambda _: None).get_prices()
    assert caught.value.http_status == 502 and "bad gateway" in (caught.value.body_excerpt or "")


def test_connection_refused_retries_then_raises_transport_error() -> None:
    with socket.socket() as probe:  # a port with nothing listening
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    slept: list[float] = []
    client = Client(base_url=f"http://127.0.0.1:{port}/v1", sleep=slept.append, max_retries=2)
    with pytest.raises(TenergyTransportError):
        client.get_prices()
    assert len(slept) == 2
