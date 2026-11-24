"""Refresh the community employer catalog and keep the last valid snapshot."""

from pathlib import Path
from time import time

import httpx
import structlog
from pydantic import BaseModel

from jobsfr.catalogs.company_types import (
    CATALOG_SOURCE_PATH,
    CompanyCatalog,
    bundled_company_catalog,
    parse_company_catalog,
    read_company_catalog,
)
from jobsfr.runtime.file_lock import file_lock
from jobsfr.runtime.files import write_text_atomically

CATALOG_URL = f"https://raw.githubusercontent.com/MandalorianY/jobsfr/main/{CATALOG_SOURCE_PATH.as_posix()}"
_REFRESH_INTERVAL = 24 * 60 * 60
log = structlog.get_logger(__name__)


class _CachedCatalog(BaseModel):
    checked_at: float = 0
    content: str | None = None
    etag: str | None = None


def resolve_company_catalog(database_path: Path, pinned_path: Path | None, *, refresh: bool) -> CompanyCatalog:
    if pinned_path is not None:
        return read_company_catalog(pinned_path)
    path = database_path.parent / ".catalogs" / "employers-v1.json"
    cached, catalog = _read_cache(path)
    if refresh and not 0 <= time() - cached.checked_at < _REFRESH_INTERVAL:
        try:
            with file_lock(path.with_suffix(".lock")):
                cached, catalog = _read_cache(path)
                now = time()
                if not 0 <= now - cached.checked_at < _REFRESH_INTERVAL:
                    catalog = _refresh_catalog(path, cached, catalog, now)
        except OSError as exc:
            log.warning("company_catalog_cache_lock_failed", error=str(exc))
    return catalog if catalog is not None else bundled_company_catalog()


def _read_cache(path: Path) -> tuple[_CachedCatalog, CompanyCatalog | None]:
    try:
        cached = _CachedCatalog.model_validate_json(path.read_bytes())
    except (OSError, ValueError):
        return _CachedCatalog(), None
    if cached.content is not None:
        try:
            return cached, parse_company_catalog(cached.content)
        except ValueError:
            return _CachedCatalog(checked_at=cached.checked_at), None
    return cached, None


def _refresh_catalog(
    path: Path, cached: _CachedCatalog, catalog: CompanyCatalog | None, now: float
) -> CompanyCatalog | None:
    try:
        headers = {"If-None-Match": cached.etag} if catalog is not None and cached.etag else {}
        response = httpx.get(CATALOG_URL, headers=headers, timeout=5.0)
        if response.status_code != 304 or catalog is None:
            response.raise_for_status()
            content = response.content.decode("utf-8")
            catalog = parse_company_catalog(content)
            cached = _CachedCatalog(content=content, etag=response.headers.get("ETag"))
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("company_catalog_refresh_failed", error=str(exc))

    cached.checked_at = now
    try:
        write_text_atomically(path, cached.model_dump_json())
    except OSError as exc:
        log.warning("company_catalog_cache_write_failed", error=str(exc))
    return catalog
