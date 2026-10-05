"""Webhook signature check, the scheme TEnergy signs webhooks with:

    X-API-SIGN = base64(HMAC_SHA256(endpoint_secret, X-API-TIMESTAMP + "." + raw_body))

X-API-TIMESTAMP is Unix seconds. `body` must be the raw bytes as received, never a
re-serialization of the parsed JSON: a different key order is a different body.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time
from typing import Mapping, Optional, Union

SIGNATURE_HEADER = "X-API-SIGN"
TIMESTAMP_HEADER = "X-API-TIMESTAMP"


def webhook_signature(secret: str, timestamp: str, body: Union[str, bytes]) -> str:
    raw = body if isinstance(body, bytes) else body.encode("utf-8")
    message = timestamp.encode("utf-8") + b"." + raw
    digest = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def header_value(headers: Mapping[str, str], name: str) -> Optional[str]:
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None


def verify_signature(
    secret: str,
    headers: Mapping[str, str],
    body: Union[str, bytes],
    tolerance_seconds: Optional[int] = None,
    now: Optional[float] = None,
) -> bool:
    """True when X-API-SIGN matches the body. Header names are matched case-insensitively.

    `tolerance_seconds` (off by default) also rejects a timestamp further than that from `now`.
    """
    timestamp = header_value(headers, TIMESTAMP_HEADER)
    signature = header_value(headers, SIGNATURE_HEADER)
    if not timestamp or not signature:
        return False
    if tolerance_seconds is not None:
        try:
            sent = int(timestamp)
        except ValueError:
            return False
        current = time.time() if now is None else now
        if abs(current - sent) > tolerance_seconds:
            return False
    expected = webhook_signature(secret, timestamp, body)
    return hmac.compare_digest(expected.encode("ascii"), signature.encode("utf-8"))
