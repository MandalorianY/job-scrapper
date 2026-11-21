from scrapper.scrapers.aggregators import scrape_aggregators
from scrapper.scrapers.jobspy_sources import JOBSPY_SITES, scrape_jobspy_sources
from scrapper.scrapers.wttj import scrape_welcome_to_the_jungle

__all__ = [
    "JOBSPY_SITES",
    "scrape_aggregators",
    "scrape_jobspy_sources",
    "scrape_welcome_to_the_jungle",
]
