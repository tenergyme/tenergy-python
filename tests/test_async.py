"""AsyncClient against the same local server as the blocking client: identical bytes on the wire,
the same errors, the same retry rules. Coroutines run with asyncio.run — no pytest plugin needed."""
from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import sys
from typing import Any, Callable, Coroutine, TypeVar

import httpx
import pytest
from local_server import Recorder, ok

from tenergy import AsyncClient, Client, TenergyError, TenergyTransportError

KEY = "ak_live_test"
SECRET = "sk_test_example"
BASE = "https://api.tenergy.me/v1"
T = TypeVar("T")


def run(make: Callable[[], Coroutine[Any, Any, T]]) -> T:
    return asyncio.run(make())


def test_async_and_blocking_clients_send_identical_requests(server: str) -> None:
    body = {"resource": "energy", "amount": 65000, "tier": "1h", "receiver": "T…", "memo": "энергия"}
    now = lambda: 1_789_000_000_000  # noqa: E731
    Recorder.script = [ok({"id": "ord_1"}, 201), ok({"id": "ord_1"}, 201)]
    assert Client(KEY, SECRET, server, now=now).create_order(body, idempotency_key="k") == {"id": "ord_1"}

    async def go() -> Any:
        async with AsyncClient(KEY, SECRET, server, now=now) as client:
            return await client.create_order(body, idempotency_key="k")

    assert run(go) == {"id": "ord_1"}
    blocking, awaited = Recorder.seen
    assert awaited["path"] == blocking["path"] == "/v1/orders"
    assert awaited["body"] == blocking["body"]
    for header in ("x-api-key", "x-api-timestamp", "x-api-sign", "idempotency-key", "content-type", "user-agent"):
        assert awaited["headers"][header] == blocking["headers"][header], header  # type: ignore[index]


def test_frozen_signature_vector_through_a_mock_transport() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"balance_sun": 1})

    # Same vector as test_client.py and the TypeScript SDK's frozen vectors: 2026-09-12T10:00:00.000Z.
    now = 1_789_207_200_000

    async def go() -> Any:
        async with AsyncClient(KEY, SECRET, BASE, transport=httpx.MockTransport(handler), now=lambda: now) as client:
            return await client.get_balance()

    assert run(go) == {"balance_sun": 1}
    request = seen[0]
    assert request.url.path == "/v1/balance"
    assert request.headers["x-api-timestamp"] == "2026-09-12T10:00:00.000Z"
    assert request.headers["x-api-sign"] == "WwD3LdlmYhCpc+uiAJDhD4C0fAcO0JgZzUan5jci3fw="


def test_error_envelope_and_no_retry_for_post(server: str) -> None:
    busy = {"error": {"code": 9000, "slug": "internal_error", "message": "x", "retryable": True}, "request_id": "req_1"}
    Recorder.script = [ok(busy, 503)]

    async def go() -> None:
        async with AsyncClient(KEY, SECRET, server) as client:
            await client.create_order({"resource": "energy"})

    with pytest.raises(TenergyError) as caught:
        run(go)
    assert (caught.value.slug, caught.value.http_status, caught.value.request_id) == ("internal_error", 503, "req_1")
    assert len(Recorder.seen) == 1


def test_get_retries_with_retry_after(server: str) -> None:
    busy = {"error": {"code": 9000, "slug": "internal_error", "message": "x", "retryable": True}}
    Recorder.script = [ok(busy, 503, {"retry-after": "2"}), ok({"balance_sun": 5})]
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    async def go() -> Any:
        async with AsyncClient(KEY, SECRET, server, sleep=fake_sleep) as client:
            return await client.get_balance()

    assert run(go) == {"balance_sun": 5}
    assert slept == [2.0] and len(Recorder.seen) == 2


def test_redirect_is_surfaced_not_followed(server: str) -> None:
    Recorder.script = [(301, b"", {"location": "https://example.com/v1/balance"})]

    async def go() -> None:
        async with AsyncClient(KEY, SECRET, server) as client:
            await client.get_balance()

    with pytest.raises(TenergyTransportError) as caught:
        run(go)
    assert caught.value.http_status == 301 and len(Recorder.seen) == 1


def test_connection_refused_retries_then_raises_transport_error() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    async def go() -> None:
        async with AsyncClient(base_url=f"http://127.0.0.1:{port}/v1", sleep=fake_sleep) as client:
            await client.get_prices()

    with pytest.raises(TenergyTransportError):
        run(go)
    assert len(slept) == 2


def test_importing_tenergy_does_not_load_httpx() -> None:
    probe = "import sys, tenergy; tenergy.Client(); print(json.dumps('httpx' in sys.modules))"
    out = subprocess.run(
        [sys.executable, "-c", f"import json; {probe}"], capture_output=True, text=True, check=True
    ).stdout
    assert json.loads(out) is False


def test_async_wait_for_order_polls_until_settled() -> None:
    answers = [{"id": "ord_1", "status": "paid"}, {"id": "ord_1", "status": "active"}]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=answers.pop(0))

    naps: list[float] = []

    async def nap(seconds: float) -> None:
        naps.append(seconds)

    async def go() -> Any:
        async with AsyncClient(KEY, SECRET, BASE, transport=httpx.MockTransport(handler), sleep=nap) as client:
            return await client.wait_for_order("ord_1")

    assert run(go)["status"] == "active" and naps == [1.0]


def test_async_bootstrap_accepts_an_async_signer() -> None:
    answers = [{"nonce": "n1", "message": "sign me"}, {"bootstrap_token": "abt_x"}, {"account_id": "acc_1"}, {"data": []}]
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=answers.pop(0))

    async def sign(message: str) -> str:
        return "0xsig-" + message

    async def go() -> Any:
        async with AsyncClient(base_url=BASE, transport=httpx.MockTransport(handler)) as client:
            return await client.bootstrap("TAddr", sign)

    assert run(go)["bootstrap_token"] == "abt_x"
    assert json.loads(seen[1].content)["signature"] == "0xsig-sign me"
    assert seen[2].headers["authorization"] == "Bearer abt_x"
