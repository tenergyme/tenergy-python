"""Client behaviour against a stub transport, pinned to the TS SDK's frozen signature vectors."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from tenergy import Client, HttpRequest, HttpResponse, TenergyError, verify_signature

KEY = "ak_live_test"
# Same secret and vectors as the TypeScript SDK ("frozen signature vectors").
SECRET = "sk_test_example"
BASE = "https://api.tenergy.me/v1"


def epoch_ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp() * 1000)


def stub(seen: list[HttpRequest], status: int = 200, payload: object = None, headers: dict | None = None):
    def handler(request: HttpRequest) -> HttpResponse:
        seen.append(request)
        body = json.dumps(payload if payload is not None else {}).encode()
        return HttpResponse(status, body, {"content-type": "application/json", **(headers or {})})

    return handler


def test_public_read_is_anonymous_without_a_key() -> None:
    seen: list[HttpRequest] = []
    client = Client(base_url=BASE, transport=stub(seen, payload={"items": []}))
    assert client.get_prices(resource="energy") == {"items": []}
    request = seen[0]
    assert request.url == f"{BASE}/prices?resource=energy"
    assert "x-api-key" not in request.headers
    assert "x-api-sign" not in request.headers


def test_signed_get_matches_frozen_vector() -> None:
    seen: list[HttpRequest] = []
    now = epoch_ms("2026-09-12T10:00:00.000Z")
    client = Client(KEY, SECRET, BASE, transport=stub(seen), now=lambda: now)
    client.get_balance()
    request = seen[0]
    assert request.url == f"{BASE}/balance"
    assert request.headers["x-api-key"] == KEY
    assert request.headers["x-api-timestamp"] == "2026-09-12T10:00:00.000Z"
    assert request.headers["x-api-sign"] == "WwD3LdlmYhCpc+uiAJDhD4C0fAcO0JgZzUan5jci3fw="


def test_signed_post_signs_the_exact_body_sent() -> None:
    seen: list[HttpRequest] = []
    now = epoch_ms("2026-09-12T10:00:00.123Z")
    client = Client(KEY, SECRET, BASE, transport=stub(seen, status=201), now=lambda: now)
    body = {"resource": "energy", "amount": 65000, "tier": "1h", "receiver": "TGaBY3tR2JwABYznzY8XUUMQnuvPEjZCA2"}
    client.create_order(body, idempotency_key="idem-1")
    request = seen[0]
    assert request.body is not None and request.body.decode() == json.dumps(body, separators=(",", ":"))
    assert request.headers["idempotency-key"] == "idem-1"
    assert request.headers["x-api-sign"] == "qFViL4FnxyIkwvcMaANZxAsgcQVZVkLLpiZosVYLnGY="


def test_error_envelope_maps_to_tenergy_error() -> None:
    seen: list[HttpRequest] = []
    envelope = {
        "error": {"code": 1008, "slug": "invalid_signature", "message": "Signature does not match"},
        "request_id": "req_123",
    }
    client = Client(KEY, SECRET, BASE, transport=stub(seen, status=401, payload=envelope))
    with pytest.raises(TenergyError) as caught:
        client.get_account()
    error = caught.value
    assert (error.code, error.slug, error.request_id) == (1008, "invalid_signature", "req_123")
    assert error.message == "Signature does not match"
    assert len(seen) == 1  # a 401 without `retryable` is not retried


def test_webhook_signature_verifies_worker_scheme() -> None:
    # Vector from Node: createHmac('sha256','whsec_x').update('1789000000.' + body).digest('base64'),
    # the scheme TEnergy signs webhooks with.
    body = '{"event":"order.confirmed","event_id":"evt_1","memo":"энергия"}'
    headers = {"x-api-timestamp": "1789000000", "X-API-SIGN": "CDVw8J3ErIpz7KvV+NXgaWaeToo7y/ASO8cEz5Xp4Zk="}
    assert verify_signature("whsec_x", headers, body.encode("utf-8"))
    assert verify_signature("whsec_x", headers, body)
    assert not verify_signature("whsec_other", headers, body)
    assert not verify_signature("whsec_x", {**headers, "x-api-timestamp": "1789000001"}, body)
    assert not verify_signature("whsec_x", headers, body, tolerance_seconds=300, now=1789000400)


def test_network_defaults_to_mainnet_and_nile_is_one_argument() -> None:
    from tenergy import API_URLS

    assert Client().base_url == "https://api.tenergy.me/v1"
    assert Client().network == "mainnet"
    nile = Client(network="nile")
    assert nile.base_url == API_URLS["nile"] == "https://api-nile.tenergy.me/v1"
    local = Client(base_url="http://localhost:3000/v1/")
    assert local.base_url == "http://localhost:3000/v1"
    assert local.network is None


def test_a_key_from_the_other_network_is_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="network=\"nile\""):
        Client("ak_test_x", "s")
    with pytest.raises(ValueError, match="mainnet"):
        Client("ak_live_x", "s", network="nile")
    with pytest.raises(ValueError, match="'mainnet' or 'nile'"):
        Client(network="shasta")
    # A self-hosted API can issue either kind; the check only guards the two public hosts.
    Client("ak_test_x", "s", "http://localhost/v1")


def sequence(seen: list[HttpRequest], *payloads: object):
    answers = list(payloads)

    def handler(request: HttpRequest) -> HttpResponse:
        seen.append(request)
        return HttpResponse(200, json.dumps(answers.pop(0)).encode(), {"content-type": "application/json"})

    return handler


def test_wait_for_order_polls_until_settled() -> None:
    seen: list[HttpRequest] = []
    transport = sequence(seen, {"id": "ord_1", "status": "paid"}, {"id": "ord_1", "status": "delegated"},
                         {"id": "ord_1", "status": "active", "delegate_hashes": ["ab"]})
    naps: list[float] = []
    client = Client(KEY, SECRET, BASE, transport=transport, sleep=naps.append)
    order = client.wait_for_order("ord_1", poll_interval=0.5)
    assert order["status"] == "active" and len(seen) == 3 and naps == [0.5, 0.5]
    assert seen[0].url == f"{BASE}/orders/ord_1"


def test_wait_for_order_timeout_carries_the_last_order() -> None:
    from tenergy import TenergyTimeoutError

    seen: list[HttpRequest] = []
    client = Client(KEY, SECRET, BASE, transport=sequence(seen, {"id": "ord_1", "status": "paid"}))
    with pytest.raises(TenergyTimeoutError) as caught:
        client.wait_for_order("ord_1", timeout=0)
    assert caught.value.last == {"id": "ord_1", "status": "paid"}


def test_bootstrap_signs_up_and_carries_the_token() -> None:
    seen: list[HttpRequest] = []
    transport = sequence(
        seen,
        {"nonce": "n1", "message": "sign me"},
        {"bootstrap_token": "abt_x"},
        {"account_id": "acc_1"},
        {"data": [{"currency": "TRX", "address": "Tdeposit"}]},
        {"key": "ak_live_new", "secret": "s"},
    )
    client = Client(base_url=BASE, transport=transport)
    signed_messages: list[str] = []

    def sign(message: str) -> str:
        signed_messages.append(message)
        return "0xsig"

    result = client.bootstrap("TAddr", sign, label="demo")
    assert signed_messages == ["sign me"]
    assert result["bootstrap_token"] == "abt_x" and result["account"]["account_id"] == "acc_1"
    assert json.loads(seen[1].body or b"{}") == {"address": "TAddr", "nonce": "n1", "signature": "0xsig"}
    assert "authorization" not in seen[0].headers
    assert seen[2].headers["authorization"] == "Bearer abt_x"
    assert seen[3].url == f"{BASE}/accounts/deposit-address?currency=TRX"
    client.create_api_key({"label": "k", "scopes": ["orders.create"]}, bootstrap_token=result["bootstrap_token"])
    assert seen[4].headers["authorization"] == "Bearer abt_x" and "x-api-sign" not in seen[4].headers


def test_every_route_of_the_contract_is_reachable_at_its_path() -> None:
    """Parity with @tenergy/sdk (2026-10-05): one call per method, path and verb checked."""
    seen: list[HttpRequest] = []
    client = Client(base_url=BASE, api_key=KEY, api_secret=SECRET, transport=stub(seen))
    calls = [
        (lambda: client.get_webhook("wh_1"), "GET", "/webhooks/wh_1"),
        (lambda: client.update_webhook("wh_1", {"enabled": False}), "PATCH", "/webhooks/wh_1"),
        (lambda: client.delete_webhook("wh_1"), "DELETE", "/webhooks/wh_1"),
        (lambda: client.rotate_webhook_secret("wh_1"), "POST", "/webhooks/wh_1/rotate-secret"),
        (lambda: client.test_webhook("wh_1"), "POST", "/webhooks/wh_1/test"),
        (lambda: client.list_webhook_deliveries("wh_1", limit=5), "GET", "/webhooks/wh_1/deliveries"),
        (lambda: client.update_api_key("k_1", {"label": "x"}), "PATCH", "/api-keys/k_1"),
        (lambda: client.delete_api_key("k_1"), "DELETE", "/api-keys/k_1"),
        (lambda: client.reclaim_order("ord_1"), "POST", "/orders/ord_1/reclaim"),
        (lambda: client.create_batch({"items": []}), "POST", "/batches"),
        (lambda: client.list_batches(), "GET", "/batches"),
        (lambda: client.get_batch("b_1"), "GET", "/batches/b_1"),
        (lambda: client.cancel_batch("b_1"), "POST", "/batches/b_1/cancel"),
        (lambda: client.list_subscription_plans(), "GET", "/subscriptions/plans"),
        (lambda: client.create_subscription({"plan_id": "p"}), "POST", "/subscriptions"),
        (lambda: client.list_subscriptions(), "GET", "/subscriptions"),
        (lambda: client.get_subscription("s_1"), "GET", "/subscriptions/s_1"),
        (lambda: client.update_subscription("s_1", {"paused": True}), "PATCH", "/subscriptions/s_1"),
        (lambda: client.cancel_subscription("s_1"), "DELETE", "/subscriptions/s_1"),
        (lambda: client.get_order_book(amount=65000), "GET", "/orderbook"),
        (lambda: client.get_market(), "GET", "/market"),
        (lambda: client.get_market_history(hours=24), "GET", "/market/history"),
        (lambda: client.get_market_summary(month="2026-10"), "GET", "/market/summary"),
        (lambda: client.get_status(), "GET", "/status"),
        (lambda: client.get_account_stats(), "GET", "/account/stats"),
        (lambda: client.list_node_keys(), "GET", "/node-keys"),
        (lambda: client.create_node_key(), "POST", "/node-keys"),
        (lambda: client.revoke_node_key("nk_1"), "DELETE", "/node-keys/nk_1"),
        (lambda: client.list_orders(group_id="payout-1", label="withdrawals"), "GET", "/orders"),
    ]
    for call, method, path in calls:
        call()
        request = seen[-1]
        assert request.method == method, path
        assert request.url.split("?")[0] == f"{BASE}{path}"
    assert seen[-1].url.endswith("?group_id=payout-1&label=withdrawals")
    # Anonymous routes stay anonymous even on a keyed client.
    status = next(r for r in seen if r.url == f"{BASE}/status")
    assert "x-api-key" not in status.headers
