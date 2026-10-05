"""TEnergy API client: signed requests, public reads, webhook verification.

`Client` needs nothing beyond the standard library. `AsyncClient` needs httpx
(`pip install "tenergy[async]"`) and is imported lazily, so `import tenergy` never loads httpx.
"""
from typing import TYPE_CHECKING, Any

from .client import (
    API_URLS,
    DEFAULT_BASE_URL,
    SETTLED_ORDER_STATUSES,
    Client,
    canonical_string,
    encode_query,
    sign_request,
)
from .errors import TenergyError, TenergyTimeoutError, TenergyTransportError
from .transport import HttpRequest, HttpResponse, Transport, urllib_transport
from .webhooks import verify_signature, webhook_signature

__all__ = [
    "API_URLS",
    "AsyncClient",
    "DEFAULT_BASE_URL",
    "Client",
    "HttpRequest",
    "HttpResponse",
    "SETTLED_ORDER_STATUSES",
    "TenergyError",
    "TenergyTimeoutError",
    "TenergyTransportError",
    "Transport",
    "canonical_string",
    "encode_query",
    "sign_request",
    "urllib_transport",
    "verify_signature",
    "webhook_signature",
]
__version__ = "0.1.0b0"

if TYPE_CHECKING:
    from .aio import AsyncClient


def __getattr__(name: str) -> Any:
    if name == "AsyncClient":
        from .aio import AsyncClient

        return AsyncClient
    raise AttributeError(f"module 'tenergy' has no attribute {name!r}")
