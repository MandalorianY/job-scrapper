from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Protocol
from urllib.parse import urlparse

import httpx


class _GetClient(Protocol):
    async def get(self, url: str, /) -> httpx.Response: ...


class HostThrottle:
    """Limit parallel request pressure per host while allowing different hosts to run."""

    def __init__(self, per_host_concurrency: int, min_delay_secs: float) -> None:
        self.per_host_concurrency = max(1, per_host_concurrency)
        self.min_delay_secs = max(0.0, min_delay_secs)
        self._semaphores: defaultdict[str, asyncio.Semaphore] = defaultdict(
            lambda: asyncio.Semaphore(self.per_host_concurrency)
        )
        self._delay_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._next_start_by_host: defaultdict[str, float] = defaultdict(float)

    async def get(self, client: _GetClient, url: str, **kwargs: object) -> httpx.Response:
        return await self.request(client, "get", url, **kwargs)

    async def post(self, client: httpx.AsyncClient, url: str, **kwargs: object) -> httpx.Response:
        return await self.request(client, "post", url, **kwargs)

    async def request(self, client: _GetClient, method: str, url: str, **kwargs: object) -> httpx.Response:
        host = _host_key(url)
        async with self._semaphores[host]:
            await self._wait_turn(host)
            return await getattr(client, method)(url, **kwargs)

    async def _wait_turn(self, host: str) -> None:
        if not self.min_delay_secs:
            return
        async with self._delay_locks[host]:
            loop = asyncio.get_running_loop()
            now = loop.time()
            wait_secs = self._next_start_by_host[host] - now
            if wait_secs > 0:
                await asyncio.sleep(wait_secs)
                now = loop.time()
            self._next_start_by_host[host] = now + self.min_delay_secs


def _host_key(url: str) -> str:
    parsed = urlparse(str(url))
    return parsed.netloc.lower() or parsed.path or "local"
