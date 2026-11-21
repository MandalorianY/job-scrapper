"""Personal job-search scraper."""

from importlib.metadata import PackageNotFoundError, version

__all__ = ["__version__"]

try:
    __version__ = version("scrapper")
except PackageNotFoundError:
    __version__ = "0+unknown"
