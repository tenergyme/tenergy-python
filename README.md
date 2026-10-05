# TEnergy SDK for Python

Rent TRON energy from code, so your USDT transfers stop burning TRX. Each order goes to the
cheapest provider available right now and is delegated on chain to the address you name. Your
wallet keys never leave your side.

- **Zero dependencies** for the regular client — standard library only. Python 3.9+.
- **Async when you need it:** `AsyncClient` for asyncio (aiogram, FastAPI), same methods.
- **Typed** (`py.typed`), every request signed, retries only where repeating is harmless.

```sh
pip install tenergy             # Client
pip install "tenergy[async]"    # + AsyncClient (adds httpx)
```

## Energy for one USDT transfer

```python
import os
from tenergy import Client

client = Client(os.environ["TENERGY_KEY"], os.environ["TENERGY_SECRET"])

order = client.create_order({
    "resource": "energy",
    "amount": 65000,                    # one USDT transfer to an address that already holds USDT
    "tier": "1h",                       # the energy is yours for an hour
    "receiver": "T...",                 # who gets the energy: the wallet that will send the USDT
    "client_order_id": "payout-42",     # makes a retry safe: same id, same order
})

delivered = client.wait_for_order(order["id"])   # status "active", delegate_hashes on chain
```

The same with asyncio — create one client, reuse it, close it:

```python
from tenergy import AsyncClient

async with AsyncClient(key, secret) as client:
    order = await client.create_order({...})
    delivered = await client.wait_for_order(order["id"])
```

## How much energy to order

| The USDT transfer goes to | Energy |
|---|---|
| an address that already holds USDT | 65,000 |
| an address with zero USDT | 131,000 |
| not sure | `client.estimate_transfer(from_address, to_address)["recommended_amount"]` |

## Try it on the testnet first

```python
client = Client(key, secret, network="nile")
```

Nile is a separate sandbox with `ak_test_` keys and free test TRX and USDT
([faucet](https://nileex.io/join/getJoinPage)); delegations show up on [nile.tronscan.org](https://nile.tronscan.org). Mainnet is the default and takes `ak_live_` keys;
mixing them up fails when the client is built, not on the first order.

## Errors and retries

```python
from tenergy import TenergyError

try:
    client.create_order({...})
except TenergyError as e:
    if e.slug == "insufficient_funds":
        ...                             # top up, then repeat with the same client_order_id
    else:
        raise
```

Branch on `slug`, never on `message` — [all codes](https://tenergy.me/docs/errors).
Reads are retried with backoff; creates are sent once, so a timed-out create is repeated by you
with the same `client_order_id`. No answer at all raises `TenergyTransportError`.

## Webhooks

Get `order.confirmed` the moment the delegation is on chain instead of polling:

```python
from tenergy import verify_signature

ok = verify_signature(secret, request.headers, raw_body, tolerance_seconds=300)
# raw_body: the bytes as received, before json.loads
```

## Links

[Docs](https://tenergy.me/docs) · [API reference](https://tenergy.me/docs/api/orders) ·
[Examples](https://github.com/tenergyme/tenergy-examples-py) · [TypeScript SDK](https://www.npmjs.com/package/@tenergy/sdk) ·
support@tenergy.me

Get a key in the [dashboard](https://tenergy.me/app), or from code with a wallet signature:
`client.bootstrap(address, sign_message)` then `create_api_key(..., bootstrap_token=...)` — how Nile keys are made. The regular client uses `urllib` with the
system certificate store; on a python.org install for macOS run "Install Certificates.command"
once. Your own HTTP stack plugs in through `Client(transport=...)`.

MIT licensed.
