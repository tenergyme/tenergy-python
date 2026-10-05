"""The HTTP layer: one request in, one response out, standard library only.

`Client(transport=...)` takes any callable with the `Transport` signature, so a test or an
application with its own HTTP stack (a session with pooling, a proxy, a mock) plugs in without
the SDK depending on it. Contract for a custom transport:

* return an `HttpResponse` for every HTTP answer, 4xx and 5xx included — the client reads the
  error envelope itself;
* raise `OSError` (or a subclass: `ConnectionError`, `TimeoutError`, `urllib.error.URLError`) when
  no answer arrived — the client retries idempotent calls and then raises `TenergyTransportError`;
* never follow redirects: the signature covers the path, so a redirected request would fail
  verification anyway, and a 3xx is better surfaced than silently retried elsewhere.
"""
from __future__ import annotations

import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, Mapping, Optional, Tuple


@dataclass(frozen=True)
class HttpRequest:
    method: str
    url: str
    headers: Mapping[str, str]
    body: Optional[bytes]
    timeout: float


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes = b""
    # Header names lower-cased.
    headers: Mapping[str, str] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


Transport = Callable[[HttpRequest], HttpResponse]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None  # urllib then raises HTTPError with the 3xx, returned below as a response


_OPENER = urllib.request.build_opener(_NoRedirect)


def _lowered(headers: Iterable[Tuple[str, str]]) -> Dict[str, str]:
    return {name.lower(): value for name, value in headers}


def urllib_transport(request: HttpRequest) -> HttpResponse:
    """The default transport: `urllib.request`, system CA store, `HTTP(S)_PROXY` honoured."""
    outgoing = urllib.request.Request(
        request.url, data=request.body, headers=dict(request.headers), method=request.method
    )
    try:
        with _OPENER.open(outgoing, timeout=request.timeout) as answer:
            return HttpResponse(answer.status, answer.read(), _lowered(answer.headers.items()))
    except urllib.error.HTTPError as answer:
        # A non-2xx status is an answer, not a transport failure.
        with answer:
            return HttpResponse(answer.code, answer.read(), _lowered(answer.headers.items()))
