from typing import TYPE_CHECKING

from scrapper.config import settings
from scrapper.config.settings import (
    DirectAggregatorConfig,
    Settings,
    SiteSearchConfig,
    get_settings,
)

_settings_module = settings
del settings

SearchConfig = Settings

if TYPE_CHECKING:
    DEFAULT_EXCLUDE_TERMS: tuple[str, ...]
    DEFAULT_INCLUDE_TERMS: tuple[str, ...]
    DEFAULT_QUERIES: tuple[str, ...]
    settings: Settings


def __getattr__(name: str) -> object:
    if name in {"DEFAULT_EXCLUDE_TERMS", "DEFAULT_INCLUDE_TERMS", "DEFAULT_QUERIES", "settings"}:
        return getattr(_settings_module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "DEFAULT_EXCLUDE_TERMS",
    "DEFAULT_INCLUDE_TERMS",
    "DEFAULT_QUERIES",
    "DirectAggregatorConfig",
    "SearchConfig",
    "Settings",
    "SiteSearchConfig",
    "get_settings",
    "settings",
]
