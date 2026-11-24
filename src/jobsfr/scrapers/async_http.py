from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Protocol
from urllib.parse import urlparse

import httpx

_RETRYABLE_STATUSES = {408, 429, 500, 502, 503, 504}
_RETRYABLE_EXCEPTIONS = (httpx.NetworkError, httpx.RemoteProtocolError, httpx.TimeoutException)
_DEFAULT_RETRY_DELAY = 1.0
_MAX_RETRY_AFTER = 10.0


class SourceBlockedError(RuntimeError):
    message = "source blocked: verification challenge"

    def __init__(self) -> None:
        super().__init__(self.message)


class HostCooldownError(httpx.HTTPError):
    """A temporary host pause would exceed the request's wait budget."""


class _GetClient(Protocol):
    async def get(self, url: str, /) -> httpx.Response: ...


def _raise_for_verification_challenge(response: httpx.Response) -> None:
    if response.headers.get("cf-mitigated", "").strip().lower() == "challenge":
        raise SourceBlockedError()
    if any(marker in response.content for marker in (b"captcha.careerjet.net", b"captcha-delivery.com")):
        raise SourceBlockedError()


async def get_checked_page(client: _GetClient, url: str, throttle: HostThrottle | None = None) -> httpx.Response:
    return await (throttle or HostThrottle(1, 0)).get(client, url)


class HostThrottle:
    """Pace requests and handle temporary backoff and challenges independently per host."""

    def __init__(self, per_host_concurrency: int, min_delay_secs: float) -> None:
        self.per_host_concurrency = max(1, per_host_concurrency)
        self.min_delay_secs = max(0.0, min_delay_secs)
        self._semaphores: defaultdict[str, asyncio.Semaphore] = defaultdict(
            lambda: asyncio.Semaphore(self.per_host_concurrency)
        )
        self._delay_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._next_start_by_host: defaultdict[str, float] = defaultdict(float)
        self._blocked_hosts: set[str] = set()
        self._cooldown_by_host: dict[str, tuple[float, int]] = {}

    async def get(self, client: _GetClient, url: str, **kwargs: object) -> httpx.Response:
        return await self.request(client, "get", url, **kwargs)

    async def post(self, client: httpx.AsyncClient, url: str, **kwargs: object) -> httpx.Response:
        # Custom scrapers use POST for read-only search requests.
        return await self.request(client, "post", url, **kwargs)

    async def request(self, client: _GetClient, method: str, url: str, **kwargs: object) -> httpx.Response:
        host = _host_key(url)
        async with self._semaphores[host]:
            for attempt in range(2):
                await self._wait_turn(host)
                try:
                    response = await getattr(client, method)(url, **kwargs)
                    _raise_for_verification_challenge(response)
                except SourceBlockedError:
                    self._blocked_hosts.add(host)
                    raise
                except _RETRYABLE_EXCEPTIONS:
                    if attempt:
                        raise
                    await asyncio.sleep(_DEFAULT_RETRY_DELAY)
                    continue
                status = response.status_code
                if status in {429, 503}:
                    self._defer_host(host, response)
                if attempt or status not in _RETRYABLE_STATUSES:
                    return response
                if status not in {429, 503}:
                    await asyncio.sleep(_DEFAULT_RETRY_DELAY)
        raise AssertionError("request attempts exhausted without a response or exception")

    def _defer_host(self, host: str, response: httpx.Response) -> None:
        retry_at = asyncio.get_running_loop().time() + _retry_after_seconds(response)
        previous, _ = self._cooldown_by_host.get(host, (0.0, 0))
        if retry_at > previous:
            self._cooldown_by_host[host] = (retry_at, response.status_code)

    async def _wait_turn(self, host: str) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _MAX_RETRY_AFTER
        async with self._delay_locks[host]:
            while True:
                if host in self._blocked_hosts:
                    raise SourceBlockedError()
                now = loop.time()
                retry_at, status = self._cooldown_by_host.get(host, (0.0, 0))
                if retry_at > deadline and retry_at > now:
                    raise HostCooldownError(f"HTTP {status}: Retry-After delay is still active")
                wait_secs = max(retry_at, self._next_start_by_host[host]) - now
                if wait_secs <= 0:
                    self._next_start_by_host[host] = now + self.min_delay_secs
                    return
                await asyncio.sleep(wait_secs)


def _retry_after_seconds(response: httpx.Response) -> float:
    value = response.headers.get("retry-after", "").strip()
    if not value:
        return _DEFAULT_RETRY_DELAY
    if value.isdecimal():
        return float(value)
    try:
        retry_at = parsedate_to_datetime(value)
    except ValueError:
        return _DEFAULT_RETRY_DELAY
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=UTC)
    return max(0.0, (retry_at - datetime.now(UTC)).total_seconds())


def _host_key(url: str) -> str:
    parsed = urlparse(str(url))
    return parsed.netloc.lower() or parsed.path or "local"
