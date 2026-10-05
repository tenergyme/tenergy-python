"""`AsyncClient`: the same API as `Client`, awaitable, over httpx (`pip install "tenergy[async]"`).

    async with AsyncClient(api_key, api_secret, network="nile") as client:
        balance = await client.get_balance()

Signing, request building, retry decisions and error mapping are `ClientCore`'s, shared with the
blocking client byte for byte; this module only sends with `httpx.AsyncClient` and waits with
`asyncio.sleep`. One `AsyncClient` holds a connection pool: create it once, reuse it, close it
(`async with`, or `await client.aclose()`).
"""
from __future__ import annotations

import asyncio
import time
import inspect
from typing import Any, Awaitable, Callable, Iterable, Mapping, Optional, Union

try:
    import httpx
except ImportError as missing:  # pragma: no cover - exercised only without the extra
    raise ImportError(
        'AsyncClient needs httpx: pip install "tenergy[async]" (the blocking Client needs nothing)'
    ) from missing

from .client import SETTLED_ORDER_STATUSES, USER_AGENT, ClientCore, signup_body
from .transport import HttpResponse


class AsyncClient(ClientCore):
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
        transport: Optional[httpx.AsyncBaseTransport] = None,
        now: Optional[Callable[[], int]] = None,
        sleep: Optional[Callable[[float], Awaitable[None]]] = None,
        user_agent: str = USER_AGENT,
    ) -> None:
        super().__init__(
            api_key, api_secret, base_url, network=network, timeout=timeout, max_retries=max_retries,
            base_delay=base_delay, max_delay=max_delay, now=now, user_agent=user_agent,
        )
        # Redirects stay off (httpx's default): the signature covers the path.
        self.http = httpx.AsyncClient(timeout=timeout, transport=transport, follow_redirects=False)
        self.sleep = sleep or asyncio.sleep

    async def aclose(self) -> None:
        await self.http.aclose()

    async def bootstrap(
        self,
        address: str,
        sign_message: Callable[[str], Union[str, Awaitable[str]]],
        *,
        label: Optional[str] = None,
        email: Optional[str] = None,
        purpose: str = "signup",
    ) -> dict[str, Any]:
        """Sign up with a TRON wallet; see Client.bootstrap. `sign_message` may be async."""
        challenge = await self.create_account_challenge(address, purpose)
        signature = sign_message(challenge["message"])
        if inspect.isawaitable(signature):
            signature = await signature
        verified = await self.verify_account_challenge(address, challenge["nonce"], signature)
        token = verified["bootstrap_token"]
        account = await self.create_account(signup_body(address, label, email), bootstrap_token=token)
        deposit = await self.get_signup_deposit_address(token)
        return {"challenge": challenge, "bootstrap_token": token, "account": account, "deposit": deposit}

    async def wait_for_order(
        self,
        order_id: str,
        *,
        timeout: float = 60.0,
        poll_interval: float = 1.0,
        until: Iterable[str] = SETTLED_ORDER_STATUSES,
    ) -> Any:
        """Poll until the order settles; see Client.wait_for_order."""
        stop = set(until)
        deadline = time.monotonic() + timeout
        while True:
            last = await self.get_order(order_id)
            if last.get("status") in stop:
                return last
            if time.monotonic() >= deadline:
                raise self.wait_timeout(order_id, timeout, last)
            await self.sleep(poll_interval)

    async def __aenter__(self) -> "AsyncClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def call(
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
        """Any route: `await client.call("GET", "/quotes/qt_1")`. Path is relative to the base URL."""
        call = self.prepare(method, path, query, body, idempotency_key, auth, bootstrap_token)
        attempt = 0
        while True:
            # Fresh timestamp and signature on every attempt.
            request = self.request_for(call)
            try:
                answer = await self.http.request(
                    request.method, request.url, headers=dict(request.headers), content=request.body
                )
            except httpx.TransportError as caught:
                delay = self.after_no_answer(call, attempt, caught)
            else:
                response = HttpResponse(
                    answer.status_code, answer.content, {k.lower(): v for k, v in answer.headers.items()}
                )
                done, value = self.after_answer(call, attempt, response)
                if done:
                    return value
                delay = value
            attempt += 1
            await self.sleep(delay)
