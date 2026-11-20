from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from scrapper.config import SearchConfig
from scrapper.text import clean_text


@dataclass(frozen=True)
class SearchResult:
    url: str
    title: str | None
    snippet: str | None


class SearchUnavailableError(RuntimeError):
    """All free search engines failed to return usable responses."""


async def _search(
    client: httpx.AsyncClient,
    query: str,
    limit: int,
    config: SearchConfig | None = None,
) -> list[SearchResult]:
    if config is None:
        config = SearchConfig()
    results: list[SearchResult] = []
    failures: list[str] = []
    for search in (_search_google, _search_duckduckgo, _search_bing):
        try:
            results.extend(await search(client, query, limit, config))
        except (httpx.HTTPError, SearchUnavailableError) as exc:
            reason = f"HTTP {exc.response.status_code}" if isinstance(exc, httpx.HTTPStatusError) else type(exc).__name__
            failures.append(f"{search.__name__.removeprefix('_search_')}: {reason}")
            continue
        if len(_dedupe_results(results)) >= limit:
            break
    if len(failures) == 3:
        raise SearchUnavailableError("All free search providers failed: " + "; ".join(failures))
    return _dedupe_results(results)[:limit]


async def _search_duckduckgo(client: httpx.AsyncClient, query: str, limit: int, config: SearchConfig) -> list[SearchResult]:
    response = await client.get(config.web_search.duckduckgo_html_url, params={"q": query})
    response.raise_for_status()
    if response.status_code == 202 or "challenge-form" in response.text:
        raise SearchUnavailableError("DuckDuckGo returned a challenge page")
    soup = BeautifulSoup(response.text, "lxml")
    results: list[SearchResult] = []
    for result in soup.select(".result"):
        anchor = result.select_one("a.result__a")
        if not anchor:
            continue
        url = _unwrap_ddg(anchor.get("href"))
        if not url or any(existing.url == url for existing in results):
            continue
        snippet_node = result.select_one(".result__snippet")
        results.append(
            SearchResult(
                url=url,
                title=clean_text(anchor.get_text(" ")),
                snippet=clean_text(snippet_node.get_text(" ")) if snippet_node else None,
            )
        )
        if len(results) >= limit:
            break
    return results


async def _search_google(client: httpx.AsyncClient, query: str, limit: int, config: SearchConfig) -> list[SearchResult]:
    origin = f"https://www.{config.search_engines.google_domain}"
    response = await client.get(
        f"{origin}/search",
        params={
            "q": query,
            "num": limit,
            "hl": config.search_engines.hl,
            "gl": config.search_engines.gl,
            "pws": "0",
        },
    )
    response.raise_for_status()
    if response.url.path.startswith("/sorry"):
        raise SearchUnavailableError("Google returned a challenge page")
    soup = BeautifulSoup(response.text, "lxml")
    results: list[SearchResult] = []
    for heading in soup.select("h3"):
        anchor = heading.find_parent("a")
        url = _unwrap_google(anchor.get("href") if anchor else None, origin)
        if not url or any(existing.url == url for existing in results):
            continue
        snippet = None
        container = heading.find_parent("div")
        if container:
            text = clean_text(container.get_text(" "))
            title = clean_text(heading.get_text(" "))
            snippet = text.replace(title or "", "", 1).strip() if text and title else text
        results.append(SearchResult(url=url, title=clean_text(heading.get_text(" ")), snippet=snippet))
        if len(results) >= limit:
            break
    return results


async def _search_bing(client: httpx.AsyncClient, query: str, limit: int, config: SearchConfig) -> list[SearchResult]:
    response = await client.get(
        "https://www.bing.com/search",
        params={
            "q": query,
            "count": limit,
            "setlang": config.http.accept_language.split(",", 1)[0],
            "cc": config.search_engines.gl,
        },
    )
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "lxml")
    if "Veuillez résoudre le défi" in soup.get_text(" ", strip=True):
        raise SearchUnavailableError("Bing returned a challenge page")
    results: list[SearchResult] = []
    for item in soup.select("li.b_algo"):
        anchor = item.select_one("h2 a[href]")
        url = _unwrap_bing(anchor.get("href") if anchor else None)
        if not url or any(existing.url == url for existing in results):
            continue
        snippet = item.select_one(".b_caption p, p")
        results.append(
            SearchResult(
                url=url,
                title=clean_text(anchor.get_text(" ")) if anchor else None,
                snippet=clean_text(snippet.get_text(" ")) if snippet else None,
            )
        )
        if len(results) >= limit:
            break
    return results


def _dedupe_results(results: list[SearchResult]) -> list[SearchResult]:
    deduped: list[SearchResult] = []
    for result in results:
        if result.url and not any(existing.url == result.url for existing in deduped):
            deduped.append(result)
    return deduped


def _unwrap_ddg(href: str | None) -> str | None:
    if not href:
        return None
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg", [None])[0]
        return unquote(target) if target else None
    if parsed.scheme in {"http", "https"}:
        return href
    return None


def _unwrap_google(href: str | None, origin: str = "https://www.google.com") -> str | None:
    if not href:
        return None
    if href.startswith("/url?"):
        target = parse_qs(urlparse(href).query).get("q", [None])[0]
        return _valid_result_url(unquote(target)) if target else None
    return _valid_result_url(urljoin(origin, href))


def _unwrap_bing(href: str | None) -> str | None:
    if not href:
        return None
    parsed = urlparse(href)
    if parsed.hostname in {"bing.com", "www.bing.com"} and parsed.path == "/ck/a":
        target = parse_qs(parsed.query).get("u", [""])[0]
        if target.startswith("a1"):
            encoded = target[2:]
            try:
                target = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8")
            except (ValueError, binascii.Error):
                return None
        return _valid_result_url(target)
    return _valid_result_url(href)


def _valid_result_url(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return None
    blocked_hosts = ("google.", "bing.com", "microsoft.com", "support.google.com", "consent.google.com")
    if any(host in parsed.netloc.lower() for host in blocked_hosts):
        return None
    return url
