"""The error envelope of docs/api/errors.md as exceptions.

TenergyError           the api answered with an error envelope; branch on `slug` (or `code`),
                       never parse `message`.
TenergyTransportError  the api did not answer, or answered something that is not an envelope
                       (a proxy's HTML 502, a DNS failure, a timeout).
TenergyTimeoutError    wait_for_order gave up; `last` is the order as the api last showed it.
                       Nothing was cancelled: the order may still settle.
"""
from __future__ import annotations

from typing import Any, Optional


class TenergyError(Exception):
    def __init__(
        self,
        code: int,
        slug: str,
        message: str,
        request_id: Optional[str] = None,
        *,
        http_status: Optional[int] = None,
        field: Optional[str] = None,
        retryable: bool = False,
        details: Optional[dict[str, Any]] = None,
        retry_after_seconds: Optional[int] = None,
    ) -> None:
        super().__init__(f"{slug} ({code}): {message}")
        self.code = code
        self.slug = slug
        self.message = message
        self.request_id = request_id
        self.http_status = http_status
        self.field = field
        self.retryable = retryable
        self.details = details
        self.retry_after_seconds = retry_after_seconds


class TenergyTransportError(Exception):
    def __init__(
        self,
        message: str,
        *,
        http_status: Optional[int] = None,
        body_excerpt: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.body_excerpt = body_excerpt


class TenergyTimeoutError(Exception):
    def __init__(self, message: str, *, waited_seconds: float, last: Any) -> None:
        super().__init__(message)
        self.waited_seconds = waited_seconds
        self.last = last


def is_error_envelope(value: Any) -> bool:
    error = value.get("error") if isinstance(value, dict) else None
    return (
        isinstance(error, dict)
        and isinstance(error.get("code"), int)
        and not isinstance(error.get("code"), bool)
        and isinstance(error.get("slug"), str)
    )
