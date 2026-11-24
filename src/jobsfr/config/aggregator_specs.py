from pydantic import BaseModel, ConfigDict, Field, PositiveInt


class DirectAggregatorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str
    search_url_templates: tuple[str, ...]
    link_prefixes: tuple[str, ...]
    base_url: str
    max_links: PositiveInt
    use_direct_locations: bool = False
    blocked_direct_hosts: tuple[str, ...] = ()
    blocked_provider_terms: tuple[str, ...] = ()


class SiteSearchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str
    query_template: str
    host: str
    search_url_templates: tuple[str, ...] = ()
    search_urls_by_location: dict[str, tuple[str, ...]] = Field(default_factory=dict)


def default_direct_specs() -> tuple[DirectAggregatorConfig, ...]:
    return (
        DirectAggregatorConfig(
            source="france_travail",
            search_url_templates=(
                (
                    "https://candidat.francetravail.fr/offres/recherche?motsCles={query}&lieux={department}"
                    "&offresPartenaires=true&rayon={radius}&tri=0"
                ),
            ),
            link_prefixes=("/offres/recherche/detail/",),
            base_url="https://candidat.francetravail.fr",
            max_links=12,
            use_direct_locations=True,
        ),
        DirectAggregatorConfig(
            source="emploi_territorial",
            search_url_templates=("https://www.emploi-territorial.fr/rechercher?adv-search={query}",),
            link_prefixes=("/offre/",),
            base_url="https://www.emploi-territorial.fr",
            max_links=20,
        ),
        DirectAggregatorConfig(
            source="meteojob",
            search_url_templates=("https://www.meteojob.com/jobsearch/offers?what={query}",),
            link_prefixes=("/jobs/",),
            base_url="https://www.meteojob.com",
            max_links=18,
        ),
        DirectAggregatorConfig(
            source="jobijoba",
            search_url_templates=("https://www.jobijoba.com/fr/emploi/{query}",),
            link_prefixes=("/fr/annonce/",),
            base_url="https://www.jobijoba.com",
            max_links=18,
            blocked_direct_hosts=("jobleads.com",),
            blocked_provider_terms=("jobleads",),
        ),
        DirectAggregatorConfig(
            source="makesense",
            search_url_templates=(
                "https://jobs.makesense.org/fr/s/jobs/all/all/cdi?contracts=cdd&sortBy=relevance&s={query}",
            ),
            link_prefixes=("/fr/jobs/",),
            base_url="https://jobs.makesense.org",
            max_links=12,
        ),
    )


def default_site_search_sources() -> tuple[SiteSearchConfig, ...]:
    # Keep these query keys stable so existing searches retain their watermarks.
    return tuple(
        SiteSearchConfig(
            source=source,
            host=host,
            query_template=f'site:{host}{path} emploi CDI "{{location}}" {{query}} -stage -alternance',
        )
        for source, host, path in (
            ("apec", "apec.fr", "/candidat/recherche-emploi"),
            ("isarta", "isarta.fr", "/emplois"),
            ("emploi_collectivites", "emploi-collectivites.fr", ""),
            ("cadremploi", "cadremploi.fr", ""),
            ("optioncarriere", "optioncarriere.com", ""),
        )
    )
