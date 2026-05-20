"""Shared HTTP transport with timeouts, retries, and useful errors."""

from __future__ import annotations

import json
import logging
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from http.client import HTTPResponse
from typing import Final, cast

from src_py_lib.json_types import JSONDict, json_dict
from src_py_lib.logging import event

DEFAULT_TIMEOUT_SECONDS: Final[float] = 30.0
DEFAULT_MAX_ATTEMPTS: Final[int] = 3
DEFAULT_RETRY_BASE_DELAY_SECONDS: Final[float] = 0.5
DEFAULT_RETRY_MAX_DELAY_SECONDS: Final[float] = 30.0
RETRYABLE_STATUS_CODES: Final[frozenset[int]] = frozenset({408, 429, 500, 502, 503, 504})
ERROR_BODY_PREVIEW_CHARS: Final[int] = 500

UrlOpen = Callable[..., HTTPResponse]

logger = logging.getLogger(__name__)


class HTTPClientError(RuntimeError):
    """Raised when an HTTP request fails after retries."""

    def __init__(self, message: str, *, status_code: int | None = None, body: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


@dataclass(frozen=True)
class RetryConfig:
    """Retry policy for transient HTTP failures."""

    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    base_delay_seconds: float = DEFAULT_RETRY_BASE_DELAY_SECONDS
    max_delay_seconds: float = DEFAULT_RETRY_MAX_DELAY_SECONDS
    retryable_status_codes: frozenset[int] = RETRYABLE_STATUS_CODES


@dataclass
class HTTPClient:
    """Small stdlib HTTP client for JSON APIs."""

    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    retry: RetryConfig = field(default_factory=RetryConfig)
    user_agent: str = "src-py-lib"
    opener: UrlOpen = urllib.request.urlopen

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        query: Mapping[str, str | int | float | bool | None] | None = None,
        json_body: object | None = None,
        data: bytes | None = None,
    ) -> bytes:
        """Make an HTTP request and return raw response bytes."""
        request_url = _with_query(url, query)
        body = data
        request_headers = {"User-Agent": self.user_agent, **dict(headers or {})}
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        request = urllib.request.Request(
            request_url, data=body, headers=request_headers, method=method
        )
        for attempt in range(1, self.retry.max_attempts + 1):
            try:
                with event(
                    "http_request",
                    level=logging.DEBUG,
                    method=method,
                    url=_safe_url(request_url),
                    attempt=attempt,
                ) as fields:
                    response = self.opener(request, timeout=self.timeout_seconds)
                    with response:
                        payload = response.read()
                        fields["status_code"] = response.status
                        fields["response_bytes"] = len(payload)
                        return payload
            except urllib.error.HTTPError as exception:
                body_text = _read_error_body(exception)
                if not self._should_retry(exception.code, attempt):
                    raise HTTPClientError(
                        f"HTTP {exception.code} for {method} {_safe_url(request_url)}: {body_text}",
                        status_code=exception.code,
                        body=body_text,
                    ) from exception
                self._sleep_before_retry(attempt, exception.headers.get("Retry-After"))
            except urllib.error.URLError as exception:
                if not self._should_retry(None, attempt):
                    raise HTTPClientError(
                        f"HTTP request failed for {method} {_safe_url(request_url)}: "
                        f"{exception.reason}"
                    ) from exception
                self._sleep_before_retry(attempt, None)
        raise AssertionError("HTTP retry loop exited without returning or raising")

    def json(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        query: Mapping[str, str | int | float | bool | None] | None = None,
        json_body: object | None = None,
    ) -> JSONDict:
        """Make an HTTP request and decode a JSON object response."""
        raw = self.request(method, url, headers=headers, query=query, json_body=json_body)
        try:
            return json_dict(json.loads(raw.decode("utf-8")) if raw else {})
        except json.JSONDecodeError as exception:
            raise HTTPClientError(
                f"Invalid JSON response from {method} {_safe_url(url)}"
            ) from exception

    def _should_retry(self, status_code: int | None, attempt: int) -> bool:
        if attempt >= self.retry.max_attempts:
            return False
        return status_code is None or status_code in self.retry.retryable_status_codes

    def _sleep_before_retry(self, attempt: int, retry_after: str | None) -> None:
        delay = _retry_after_seconds(retry_after)
        if delay is None:
            delay = min(
                self.retry.base_delay_seconds * (2 ** (attempt - 1)),
                self.retry.max_delay_seconds,
            ) * random.uniform(0.5, 1.5)
        logger.warning("HTTP request failed; retrying in %.2fs (attempt %d).", delay, attempt + 1)
        time.sleep(delay)


def _with_query(
    url: str,
    query: Mapping[str, str | int | float | bool | None] | None,
) -> str:
    if not query:
        return url
    filtered = {key: value for key, value in query.items() if value is not None}
    separator = "&" if urllib.parse.urlsplit(url).query else "?"
    return f"{url}{separator}{urllib.parse.urlencode(filtered)}"


def _safe_url(url: str) -> str:
    split = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((split.scheme, split.netloc, split.path, split.query, ""))


def _read_error_body(exception: urllib.error.HTTPError) -> str:
    raw = exception.read()
    text = raw.decode("utf-8", errors="replace").strip()
    if len(text) <= ERROR_BODY_PREVIEW_CHARS:
        return text
    return f"{text[:ERROR_BODY_PREVIEW_CHARS]}... (+{len(text) - ERROR_BODY_PREVIEW_CHARS} chars)"


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(float(value), 0.0)
    except ValueError:
        return None


def cast_json_dict(value: object) -> JSONDict:
    """Compatibility wrapper for call sites that want an explicit boundary cast."""
    return cast(JSONDict, value) if isinstance(value, dict) else {}
