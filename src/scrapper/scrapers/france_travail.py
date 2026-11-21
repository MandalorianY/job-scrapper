from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx
from bs4 import Tag

from scrapper.config import SearchConfig
from scrapper.dates import parse_posted_date
from scrapper.models import JobOffer
from scrapper.scrapers.async_http import HostThrottle, get_checked_page
from scrapper.source_rules.france_travail import public_offer_location
from scrapper.text import clean_text


def listing_date(anchor: Tag) -> str | None:
    date_node = anchor.select_one(".date")
    label = clean_text(date_node.get_text(" ")) if date_node else None
    if not label or not label.startswith("Publié"):
        return None
    parsed = parse_posted_date(label, now=datetime.now(ZoneInfo("Europe/Paris")))
    return parsed.day.isoformat() if parsed else None


async def extract_public_offer(
    client: httpx.AsyncClient,
    url: str,
    query: str,
    config: SearchConfig,
    throttle: HostThrottle | None = None,
) -> JobOffer | None:
    """Read the public JSON used by France Travail's descriptif-offre component."""
    parsed = urlparse(url)
    match = re.fullmatch(r"/offres/recherche/detail/([A-Za-z0-9]+)", parsed.path.rstrip("/"))
    if parsed.hostname != "candidat.francetravail.fr" or not match:
        return None
    offer_id = match.group(1)
    endpoint = f"https://candidat.francetravail.fr/api-descriptifoffre/offre/{offer_id}"
    response = await get_checked_page(client, endpoint, throttle)
    if response.status_code in {404, 410}:
        return None
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict) or not data.get("intituleOffre"):
        raise ValueError("France Travail returned no offer details")
    employer = data.get("etablissement") or {}
    description = data.get("descriptif")
    return JobOffer(
        source="france_travail",
        source_id=offer_id,
        source_url=url,
        direct_url=url,
        title=clean_text(data.get("intituleOffre")),
        company=clean_text(employer.get("libelleEnseigne")),
        location=public_offer_location(data.get("lieuTravail"), description),
        description=clean_text(description),
        employment_type=clean_text(data.get("syntheseContrat")),
        date_posted=clean_text(data.get("datePublication") or data.get("dateCreation")),
        salary=clean_text(data.get("salaire")),
        company_url=clean_text(employer.get("urlEntreprise")),
        company_description=clean_text(employer.get("descriptionEtablissement")),
        search_query=query,
        search_location=config.location_label,
        raw={"public_offer": data},
    )
