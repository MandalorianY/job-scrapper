from __future__ import annotations

import re
from urllib.parse import urlparse

import httpx

from scrapper.config import SearchConfig
from scrapper.models import JobOffer
from scrapper.scrapers.async_http import HostThrottle
from scrapper.text import clean_text


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
    response = await throttle.get(client, endpoint) if throttle else await client.get(endpoint)
    if response.status_code in {404, 410}:
        return None
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict) or not data.get("intituleOffre"):
        raise ValueError("France Travail returned no offer details")
    employer = data.get("etablissement") or {}
    return JobOffer(
        source="france_travail", source_id=offer_id, source_url=url, direct_url=url,
        title=clean_text(data.get("intituleOffre")),
        company=clean_text(employer.get("libelleEnseigne")),
        location=clean_text(data.get("lieuTravail")),
        description=clean_text(data.get("descriptif")),
        employment_type=clean_text(data.get("syntheseContrat")),
        date_posted=clean_text(data.get("dateDerniereModification")),
        salary=clean_text(data.get("salaire")),
        company_url=clean_text(employer.get("urlEntreprise")),
        company_description=clean_text(employer.get("descriptionEtablissement")),
        search_query=query, search_location=config.location_label,
        raw={"public_offer": data},
    )
