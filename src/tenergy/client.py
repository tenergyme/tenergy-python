"""The TEnergy API client, mirroring the TypeScript SDK (@tenergy/sdk). `Client` blocks; the async
twin `AsyncClient` (tenergy/aio.py, `pip install "tenergy[async]"`) shares `ClientCore`.

* Standard library only: HTTP goes through `transport.urllib_transport` unless the caller
  passes its own `transport` (see transport.py for the contract).

* Signed calls carry X-API-KEY, X-API-TIMESTAMP (ISO 8601 UTC with milliseconds) and
  X-API-SIGN = base64(HMAC_SHA256(api_secret, timestamp + METHOD + path + ["?" + query] + body)),
  where path includes the /v1 prefix and query/body are the exact bytes sent.
* The body is serialized once; the signature and the request use that same string.
* Every attempt gets a fresh timestamp and signature (a reused one is 1009 replayed_signature).
* Only GET/HEAD/DELETE are retried. Make a create safe to repeat with `client_order_id` or an
  Idempotency-Key and repeat it yourself.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, Optional, Tuple
from urllib.parse import quote, urlsplit

from .errors import TenergyError, TenergyTimeoutError, TenergyTransportError, is_error_envelope
from .transport import HttpRequest, HttpResponse, Transport, urllib_transport

# The two public API hosts. `network` picks one; `base_url` overrides both (a self-hosted or
# local API). Mainnet is the default, so a production integration needs no argument at all.
API_URLS = {
    "mainnet": "https://api.tenergy.me/v1",
    "nile": "https://api-nile.tenergy.me/v1",
}
DEFAULT_BASE_URL = API_URLS["mainnet"]
USER_AGENT = "tenergy-sdk-py/0.1.0"
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "DELETE"})
# Where wait_for_order stops by default: the caller either has the energy or will not get it.
SETTLED_ORDER_STATUSES = frozenset({"active", "expired", "reclaimed", "failed", "refunded"})
# encodeURIComponent leaves exactly these unescaped besides letters and digits.
URI_COMPONENT_SAFE = "-_.!~*'()"


def canonical_string(timestamp: str, method: str, path: str, query: str, body: str) -> str:
    mark = "" if query == "" else f"?{query}"
    return f"{timestamp}{method.upper()}{path}{mark}{body}"


def sign_request(secret: str, timestamp: str, method: str, path: str, query: str, body: str) -> str:
    message = canonical_string(timestamp, method, path, query, body).encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def api_timestamp(at_ms: int) -> str:
    """Same string as JavaScript's `new Date(ms).toISOString()`."""
    moment = datetime.fromtimestamp(at_ms // 1000, tz=timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{at_ms % 1000:03d}Z"


def encode_component(value: Any) -> str:
    if isinstance(value, bool):
        value = "true" if value else "false"
    return quote(str(value), safe=URI_COMPONENT_SAFE)


def encode_query(query: Optional[Mapping[str, Any]]) -> str:
    """Caller key order, arrays repeat the key; the exact string is both signed and sent."""
    if not query:
        return ""
    parts: list[str] = []
    for key, value in query.items():
        if value is None:
            continue
        values = value if isinstance(value, (list, tuple)) else [value]
        for entry in values:
            if entry is None:
                continue
            parts.append(f"{encode_component(key)}={encode_component(entry)}")
    return "&".join(parts)


def serialize_body(body: Any) -> str:
    """JSON.stringify equivalent: compact separators, non-ASCII kept as UTF-8."""
    if body is None:
        return ""
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True)
class PreparedCall:
    """One logical call, computed once; every attempt re-signs it with a fresh timestamp."""

    method: str
    url: str
    signed_path: str
    query: str
    body: str
    content: Optional[bytes]
    retries: int
    idempotency_key: Optional[str]
    auth: str
    bootstrap_token: Optional[str] = None


class ClientCore:
    """Everything both clients share: configuration, signing, retry decisions, response reading.

    `Client` and `AsyncClient` add only the two things that differ — how a request is sent and
    how a backoff is waited out — so the signature can never drift between them.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        base_url: Optional[str] = None,
        *,
        network: str = "mainnet",
        timeout: float = 15.0,
        max_retries: int = 2,
        base_delay: float = 0.2,
        max_delay: float = 5.0,
        now: Optional[Callable[[], int]] = None,
        user_agent: str = USER_AGENT,
    ) -> None:
        if network not in API_URLS:
            raise ValueError(f"network must be 'mainnet' or 'nile', got {network!r}")
        if base_url is not None and not base_url:
            raise ValueError("base_url must be a non-empty URL, e.g. http://localhost:3000/v1")
        # A key belongs to one network: ak_test_ keys exist only on Nile, ak_live_ only on
        # mainnet. Said here, at construction, instead of as a 401 on the first signed call.
        if base_url is None and api_key is not None:
            if network == "mainnet" and api_key.startswith("ak_test_"):
                raise ValueError("an ak_test_ key belongs to the Nile sandbox: pass network=\"nile\"")
            if network == "nile" and api_key.startswith("ak_live_"):
                raise ValueError("an ak_live_ key belongs to mainnet: drop network=\"nile\"")
        # The network picked by `network`; None when `base_url` pointed the client elsewhere.
        self.network: Optional[str] = network if base_url is None else None
        # A trailing slash would make the signed path `//orders` and fail with 1008.
        self.base_url = (base_url or API_URLS[network]).rstrip("/")
        self.api_key = api_key
        self.api_secret = api_secret
        self.timeout = timeout
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.user_agent = user_agent
        self.now = now or (lambda: int(time.time() * 1000))

    # ---- public reads: anonymous without a key, signed when the client holds one ----
    # Each method returns `self.call(...)`: the value on `Client`, an awaitable on `AsyncClient`.

    def get_prices(self, **query: Any) -> Any:
        return self.call("GET", "/prices", query=query, auth="optional")

    def estimate_order(self, **query: Any) -> Any:
        """GET /estimate: stateless price estimate for an order (public)."""
        return self.call("GET", "/estimate", query=query, auth="optional")

    def get_address_resources(self, address: str) -> Any:
        return self.call("GET", f"/resources/{encode_component(address)}", auth="optional")

    # ---- signed ----

    def estimate_transfer(
        self, from_address: str, to_address: str, contract_address: Optional[str] = None, **extra: Any
    ) -> Any:
        """POST /estimate/transfer. The contract requires a key here (no anonymous override)."""
        body: dict[str, Any] = {"from_address": from_address, "to_address": to_address}
        if contract_address is not None:
            body["contract_address"] = contract_address
        body.update(extra)
        return self.call("POST", "/estimate/transfer", body=body)

    def get_account(self) -> Any:
        return self.call("GET", "/account")

    def get_balance(self) -> Any:
        return self.call("GET", "/balance")

    def get_deposit_addresses(self) -> Any:
        return self.call("GET", "/deposit-addresses")

    def create_quote(self, body: Mapping[str, Any], idempotency_key: Optional[str] = None) -> Any:
        return self.call("POST", "/quotes", body=body, idempotency_key=idempotency_key)

    def get_quote(self, quote_id: str) -> Any:
        return self.call("GET", f"/quotes/{encode_component(quote_id)}")

    def create_order(self, body: Mapping[str, Any], idempotency_key: Optional[str] = None) -> Any:
        return self.call("POST", "/orders", body=body, idempotency_key=idempotency_key)

    def get_order(self, order_id: str) -> Any:
        """Accepts our order id or `cid:<client_order_id>`."""
        return self.call("GET", f"/orders/{encode_component(order_id)}")

    def list_orders(self, **query: Any) -> Any:
        return self.call("GET", "/orders", query=query)

    def create_api_key(self, body: Mapping[str, Any], *, bootstrap_token: Optional[str] = None) -> Any:
        """`scopes` is required and never defaulted: a key gets exactly what its creator lists.
        Right after bootstrap() pass its `bootstrap_token` — the account has no key yet."""
        if bootstrap_token is not None:
            return self.call("POST", "/api-keys", body=body, auth="bootstrap", bootstrap_token=bootstrap_token)
        return self.call("POST", "/api-keys", body=body)

    # ---- sign-up steps; bootstrap() runs all of them ----

    def create_account_challenge(self, address: str, purpose: str = "signup") -> Any:
        return self.call("POST", "/accounts/challenge", body={"address": address, "purpose": purpose}, auth="none")

    def verify_account_challenge(self, address: str, nonce: str, signature: str) -> Any:
        body = {"address": address, "nonce": nonce, "signature": signature}
        return self.call("POST", "/accounts/challenge/verify", body=body, auth="none")

    def create_account(self, body: Mapping[str, Any], *, bootstrap_token: str) -> Any:
        return self.call("POST", "/accounts", body=body, auth="bootstrap", bootstrap_token=bootstrap_token)

    def get_signup_deposit_address(self, bootstrap_token: str, currency: str = "TRX") -> Any:
        return self.call(
            "GET", "/accounts/deposit-address", query={"currency": currency}, auth="bootstrap", bootstrap_token=bootstrap_token
        )

    def list_api_keys(self) -> Any:
        return self.call("GET", "/api-keys")

    def list_webhooks(self) -> Any:
        return self.call("GET", "/webhooks")

    def create_webhook(self, body: Mapping[str, Any]) -> Any:
        return self.call("POST", "/webhooks", body=body)

    def get_webhook(self, webhook_id: str) -> Any:
        return self.call("GET", f"/webhooks/{encode_component(webhook_id)}")

    def update_webhook(self, webhook_id: str, body: Mapping[str, Any]) -> Any:
        return self.call("PATCH", f"/webhooks/{encode_component(webhook_id)}", body=body)

    def delete_webhook(self, webhook_id: str) -> Any:
        return self.call("DELETE", f"/webhooks/{encode_component(webhook_id)}")

    def rotate_webhook_secret(self, webhook_id: str) -> Any:
        return self.call("POST", f"/webhooks/{encode_component(webhook_id)}/rotate-secret")

    def test_webhook(self, webhook_id: str, body: Optional[Mapping[str, Any]] = None) -> Any:
        return self.call("POST", f"/webhooks/{encode_component(webhook_id)}/test", body=body)

    def list_webhook_deliveries(self, webhook_id: str, **query: Any) -> Any:
        """Delivery attempts of one endpoint, newest first."""
        return self.call("GET", f"/webhooks/{encode_component(webhook_id)}/deliveries", query=query)

    def update_api_key(self, key_id: str, body: Mapping[str, Any]) -> Any:
        return self.call("PATCH", f"/api-keys/{encode_component(key_id)}", body=body)

    def delete_api_key(self, key_id: str) -> Any:
        return self.call("DELETE", f"/api-keys/{encode_component(key_id)}")

    # ---- orders: early return ----

    def reclaim_order(self, order_id: str, idempotency_key: Optional[str] = None) -> Any:
        """POST /orders/{id}/reclaim: take the resource back before the term ends (202 until the
        undelegation lands). An order already reclaimed or expired answers 200 and changes nothing."""
        return self.call("POST", f"/orders/{encode_component(order_id)}/reclaim", idempotency_key=idempotency_key)

    # ---- batches ----

    def create_batch(self, body: Mapping[str, Any], idempotency_key: Optional[str] = None) -> Any:
        return self.call("POST", "/batches", body=body, idempotency_key=idempotency_key)

    def list_batches(self, **query: Any) -> Any:
        return self.call("GET", "/batches", query=query)

    def get_batch(self, batch_id: str, **query: Any) -> Any:
        return self.call("GET", f"/batches/{encode_component(batch_id)}", query=query)

    def cancel_batch(self, batch_id: str) -> Any:
        return self.call("POST", f"/batches/{encode_component(batch_id)}/cancel")

    # ---- subscriptions ----

    def list_subscription_plans(self) -> Any:
        return self.call("GET", "/subscriptions/plans", auth="none")

    def create_subscription(self, body: Mapping[str, Any], idempotency_key: Optional[str] = None) -> Any:
        return self.call("POST", "/subscriptions", body=body, idempotency_key=idempotency_key)

    def list_subscriptions(self, **query: Any) -> Any:
        return self.call("GET", "/subscriptions", query=query)

    def get_subscription(self, subscription_id: str) -> Any:
        return self.call("GET", f"/subscriptions/{encode_component(subscription_id)}")

    def update_subscription(self, subscription_id: str, body: Mapping[str, Any]) -> Any:
        return self.call("PATCH", f"/subscriptions/{encode_component(subscription_id)}", body=body)

    def cancel_subscription(self, subscription_id: str) -> Any:
        return self.call("DELETE", f"/subscriptions/{encode_component(subscription_id)}")

    # ---- market, order book, status (anonymous) ----

    def get_order_book(self, **query: Any) -> Any:
        """The ask ladder, cheapest first; anonymous. Pass `amount` for the walk at that size."""
        return self.call("GET", "/orderbook", query=query, auth="none")

    def get_market(self) -> Any:
        """Every provider's live price per term, ranked; anonymous."""
        return self.call("GET", "/market", auth="optional")

    def get_market_history(self, **query: Any) -> Any:
        return self.call("GET", "/market/history", query=query, auth="optional")

    def get_market_summary(self, **query: Any) -> Any:
        return self.call("GET", "/market/summary", query=query, auth="optional")

    def get_status(self) -> Any:
        return self.call("GET", "/status", auth="none")

    # ---- reports ----

    def get_account_stats(self, **query: Any) -> Any:
        """Orders and spend per day over `from`..`to`."""
        return self.call("GET", "/account/stats", query=query)

    # ---- node keys (tener.gy) ----

    def list_node_keys(self) -> Any:
        return self.call("GET", "/node-keys")

    def create_node_key(self, body: Optional[Mapping[str, Any]] = None) -> Any:
        return self.call("POST", "/node-keys", body=body or {})

    def revoke_node_key(self, key_id: str) -> Any:
        return self.call("DELETE", f"/node-keys/{encode_component(key_id)}")

    def call(
        self,
        method: str,
        path: str,
        *,
        query: Optional[Mapping[str, Any]] = None,
        body: Any = None,
        idempotency_key: Optional[str] = None,
        auth: str = "api-key",
        bootstrap_token: Optional[str] = None,
    ) -> Any:
        raise NotImplementedError

    # ---- shared mechanics ----

    def prepare(
        self,
        method: str,
        path: str,
        query: Optional[Mapping[str, Any]],
        body: Any,
        idempotency_key: Optional[str],
        auth: str,
        bootstrap_token: Optional[str] = None,
    ) -> PreparedCall:
        method = method.upper()
        query_string = encode_query(query)
        body_string = serialize_body(body)
        url = f"{self.base_url}{path}" + (f"?{query_string}" if query_string else "")
        return PreparedCall(
            method=method,
            url=url,
            signed_path=urlsplit(url).path,
            query=query_string,
            body=body_string,
            content=body_string.encode("utf-8") if body_string else None,
            retries=self.max_retries if method in IDEMPOTENT_METHODS else 0,
            idempotency_key=idempotency_key,
            auth=auth,
            bootstrap_token=bootstrap_token,
        )

    def request_for(self, call: PreparedCall) -> HttpRequest:
        """A freshly signed request for one attempt."""
        headers = self.headers_for(call.method, call.signed_path, call.query, call.body, call.idempotency_key, call.auth)
        if call.auth == "bootstrap":
            if not call.bootstrap_token:
                raise ValueError("this call needs the bootstrap token (abt_...) from bootstrap()")
            headers["authorization"] = f"Bearer {call.bootstrap_token}"
        return HttpRequest(call.method, call.url, headers, call.content, self.timeout)

    def headers_for(
        self,
        method: str,
        signed_path: str,
        query: str,
        body: str,
        idempotency_key: Optional[str],
        auth: str,
    ) -> dict[str, str]:
        headers = {"accept": "application/json", "user-agent": self.user_agent}
        if body:
            headers["content-type"] = "application/json"
        if idempotency_key is not None:
            headers["idempotency-key"] = idempotency_key
        if auth in ("none", "bootstrap"):
            return headers
        if self.api_key is None or self.api_secret is None:
            if auth == "optional":
                return headers
            raise ValueError("api_key and api_secret are required for authenticated calls")
        timestamp = api_timestamp(self.now())
        headers["x-api-key"] = self.api_key
        headers["x-api-timestamp"] = timestamp
        headers["x-api-sign"] = sign_request(self.api_secret, timestamp, method, signed_path, query, body)
        return headers

    def after_no_answer(self, call: PreparedCall, attempt: int, caught: Exception) -> float:
        """Delay before the next attempt, or raise when the call has no retries left."""
        if attempt < call.retries:
            return self.backoff(attempt + 1, None)
        raise TenergyTransportError(f"{call.method} {call.signed_path} failed: {caught}") from caught

    def after_answer(self, call: PreparedCall, attempt: int, response: HttpResponse) -> Tuple[bool, Any]:
        """(True, value) on success; (False, delay) to retry; raises the error otherwise."""
        text = response.text
        if 200 <= response.status < 300:
            return True, (None if text == "" else parse_json(text, response.status))
        error = to_error(response, text)
        if attempt < call.retries and should_retry(response.status, error):
            retry_after = error.retry_after_seconds if isinstance(error, TenergyError) else None
            return False, self.backoff(attempt + 1, retry_after)
        raise error

    def wait_timeout(self, order_id: str, timeout: float, last: Any) -> TenergyTimeoutError:
        status = last.get("status") if isinstance(last, dict) else None
        return TenergyTimeoutError(
            f"order {order_id} was still {status} after {timeout:g} s", waited_seconds=timeout, last=last
        )

    def backoff(self, attempt: int, retry_after_seconds: Optional[int]) -> float:
        if retry_after_seconds is not None and retry_after_seconds > 0:
            return min(float(retry_after_seconds), self.max_delay)
        return min(self.base_delay * 2.0 ** (attempt - 1), self.max_delay)


class Client(ClientCore):
    """Blocking client over the standard library (`transport.urllib_transport`)."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        base_url: Optional[str] = None,
        *,
        network: str = "mainnet",
        timeout: float = 15.0,
        max_retries: int = 2,
        base_delay: float = 0.2,
        max_delay: float = 5.0,
        transport: Optional[Transport] = None,
        now: Optional[Callable[[], int]] = None,
        sleep: Optional[Callable[[float], None]] = None,
        user_agent: str = USER_AGENT,
    ) -> None:
        super().__init__(
            api_key, api_secret, base_url, network=network, timeout=timeout, max_retries=max_retries,
            base_delay=base_delay, max_delay=max_delay, now=now, user_agent=user_agent,
        )
        self.transport: Transport = transport or urllib_transport
        self.sleep = sleep or time.sleep

    def close(self) -> None:
        """Nothing to release (no pooled connections); kept so `with Client() as c:` works."""

    def bootstrap(
        self,
        address: str,
        sign_message: Callable[[str], str],
        *,
        label: Optional[str] = None,
        email: Optional[str] = None,
        purpose: str = "signup",
    ) -> dict[str, Any]:
        """Sign up with a TRON wallet: challenge -> your signature -> account -> deposit address.

        `sign_message(message)` returns a TRON message signature (signMessageV2, hex) made with the
        address's key — the key stays with you. Returns `bootstrap_token` (15 minutes): create the
        first API key with it, `create_api_key(..., bootstrap_token=...)`.
        """
        challenge = self.create_account_challenge(address, purpose)
        verified = self.verify_account_challenge(address, challenge["nonce"], sign_message(challenge["message"]))
        token = verified["bootstrap_token"]
        account = self.create_account(signup_body(address, label, email), bootstrap_token=token)
        deposit = self.get_signup_deposit_address(token)
        return {"challenge": challenge, "bootstrap_token": token, "account": account, "deposit": deposit}

    def wait_for_order(
        self,
        order_id: str,
        *,
        timeout: float = 60.0,
        poll_interval: float = 1.0,
        until: Iterable[str] = SETTLED_ORDER_STATUSES,
    ) -> Any:
        """Poll until the order settles (`active` = energy delegated). A 201 from create_order
        means accepted and paid, not yet delivered. A timeout raises TenergyTimeoutError and
        cancels nothing."""
        stop = set(until)
        deadline = time.monotonic() + timeout
        while True:
            last = self.get_order(order_id)
            if last.get("status") in stop:
                return last
            if time.monotonic() >= deadline:
                raise self.wait_timeout(order_id, timeout, last)
            self.sleep(poll_interval)

    def __enter__(self) -> "Client":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def call(
        self,
        method: str,
        path: str,
        *,
        query: Optional[Mapping[str, Any]] = None,
        body: Any = None,
        idempotency_key: Optional[str] = None,
        auth: str = "api-key",
        bootstrap_token: Optional[str] = None,
    ) -> Any:
        """Any route: `client.call("GET", "/quotes/qt_1")`. Path is relative to the base URL."""
        call = self.prepare(method, path, query, body, idempotency_key, auth, bootstrap_token)
        attempt = 0
        while True:
            # Fresh timestamp and signature on every attempt.
            request = self.request_for(call)
            try:
                response = self.transport(request)
            except OSError as caught:
                delay = self.after_no_answer(call, attempt, caught)
            else:
                done, value = self.after_answer(call, attempt, response)
                if done:
                    return value
                delay = value
            attempt += 1
            self.sleep(delay)


def signup_body(address: str, label: Optional[str], email: Optional[str]) -> dict[str, Any]:
    body: dict[str, Any] = {"address": address}
    if label is not None:
        body["label"] = label
    if email is not None:
        body["email"] = email
    return body


def parse_json(text: str, status: int) -> Any:
    try:
        return json.loads(text)
    except ValueError as caught:
        raise TenergyTransportError(
            "The api answered with a body that is not JSON", http_status=status, body_excerpt=text[:512]
        ) from caught


def to_error(response: HttpResponse, text: str) -> Exception:
    try:
        parsed = json.loads(text)
    except ValueError:
        return TenergyTransportError(
            f"HTTP {response.status} with a non-JSON body",
            http_status=response.status,
            body_excerpt=text[:512],
        )
    if not is_error_envelope(parsed):
        return TenergyTransportError(
            f"HTTP {response.status} without an error envelope",
            http_status=response.status,
            body_excerpt=text[:512],
        )
    error = parsed["error"]
    header = response.headers.get("retry-after")
    retry_after = int(header) if header is not None and header.strip().isdigit() else None
    return TenergyError(
        error["code"],
        error["slug"],
        str(error.get("message", "")),
        parsed.get("request_id"),
        http_status=response.status,
        field=error.get("field"),
        retryable=error.get("retryable") is True,
        details=error.get("details"),
        retry_after_seconds=retry_after,
    )


def should_retry(status: int, error: Exception) -> bool:
    if status == 429 or status >= 500:
        return True
    return isinstance(error, TenergyError) and error.retryable
