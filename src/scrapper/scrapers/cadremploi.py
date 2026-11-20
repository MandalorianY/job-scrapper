from __future__ import annotations

import asyncio

import httpx
from jobspy.util import TLSRotating

from scrapper.scrapers.async_http import HostThrottle


def _tls_get(url: str, user_agent: str) -> httpx.Response:
    """Use the existing JobSpy HTTP transport for Cadremploi's public pages."""
    session = TLSRotating()
    try:
        session.headers["user-agent"] = user_agent
        response = session.get(url, timeout=20, allow_redirects=True)
        return httpx.Response(
            response.status_code, text=response.text,
            headers={"content-type": httpx.Headers(response.headers).get("content-type", "")},
            request=httpx.Request("GET", response.url or url),
        )
    except Exception as exc:
        raise httpx.RequestError("Cadremploi public-page request failed", request=httpx.Request("GET", url)) from exc
    finally:
        session.close()


class _PublicClient:
    def __init__(self, user_agent: str):
        self.user_agent = user_agent

    async def get(self, url: str) -> httpx.Response:
        return await asyncio.to_thread(_tls_get, url, self.user_agent)


async def get_page(client: httpx.AsyncClient, url: str, throttle: HostThrottle | None = None) -> httpx.Response:
    response = await throttle.get(client, url) if throttle else await client.get(url)
    if response.status_code != 403:
        return response
    public_client = _PublicClient(client.headers.get("user-agent", ""))
    # Keep the fallback inside the same per-host concurrency and delay limits.
    return await throttle.get(public_client, url) if throttle else await public_client.get(url)
