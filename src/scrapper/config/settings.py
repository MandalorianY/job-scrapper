from __future__ import annotations

import os
import re
from collections.abc import Iterator, Mapping
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypedDict

from pydantic import BaseModel, Field, SecretStr, ValidationInfo, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    InitSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

from scrapper.config.aggregator_specs import (
    DirectAggregatorConfig,
    SiteSearchConfig,
    default_direct_specs,
    default_site_search_sources,
)
from scrapper.text import matches_job_title, normalize_text

PACKAGE_CONFIG_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_CONFIG_DIR.parents[2]
LOCAL_CONFIG_FILE = PACKAGE_CONFIG_DIR / "config.local.yaml"


def _yaml_config_files(app_env: str, profile: Path | None = None) -> list[Path]:
    files = [
        PACKAGE_CONFIG_DIR / "config.yaml",
        PACKAGE_CONFIG_DIR / f"config.{app_env}.yaml",
    ]
    if profile is not None:
        files.append(profile)
    elif os.getenv("SCRAPPER_DISABLE_LOCAL_CONFIG") not in {"1", "true", "TRUE", "yes", "YES"}:
        files.append(LOCAL_CONFIG_FILE)
    return files


class _ConfigSources(InitSettingsSource):
    def __init__(self, settings_cls: type[BaseSettings], sources: dict[str, str], result: dict[str, str]) -> None:
        super().__init__(settings_cls, {})
        self.sources = sources
        self.result = result

    def __call__(self) -> dict[str, object]:
        for name, label in self.sources.items():
            for path in _leaf_paths(self.settings_sources_data[name]):
                # The settings report renders provider specs as arrays.
                if path[0] == "aggregators" and path[1:2] in (("direct_specs",), ("site_search_sources",)):
                    path = path[:2]
                self.result.setdefault(".".join(path), label)
        return {}


def _leaf_paths(values: Mapping[str, object], prefix: tuple[str, ...] = ()) -> Iterator[tuple[str, ...]]:
    for path, _ in _leaf_values(values, prefix):
        yield path


def _leaf_values(
    values: Mapping[str, object], prefix: tuple[str, ...] = ()
) -> Iterator[tuple[tuple[str, ...], object]]:
    for key, value in values.items():
        path = (*prefix, key)
        if isinstance(value, dict) and value:
            yield from _leaf_values(value, path)
        else:
            yield path, value


class SettingsReport(TypedDict):
    values: dict[str, object]
    sources: dict[str, str]


class EnvironmentSettings(BaseSettings):
    app_env: str = "dev"

    model_config = SettingsConfigDict(
        env_prefix="SCRAPPER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )


class JobspyConfig(BaseModel):
    glassdoor_results_wanted_per_query: int
    google_query_places: tuple[str, ...] = ()
    google_query_exclude_terms: tuple[str, ...]
    glassdoor_regional_fallback_term: str | None = None
    glassdoor_location_override: str | None = None


class HttpConfig(BaseModel):
    accept_language: str


class WelcomeToTheJungleConfig(BaseModel):
    site_url: str = "https://www.welcometothejungle.com"
    algolia_url: str | None = None
    algolia_app_id: str | None = None
    algolia_api_key: SecretStr | None = None
    job_index: str | None = None
    around_radius: int | None = Field(default=None, gt=0)
    office_country_filter: str = '["offices.country_code:FR"]'


class LocationCenterConfig(BaseModel):
    name: str
    latitude: float
    longitude: float


class LocationScopeConfig(BaseModel):
    enabled: bool = True
    radius_km: float = 25
    centers: tuple[LocationCenterConfig, ...] = ()
    city_coordinates: dict[str, tuple[float, float]] = Field(default_factory=dict)
    city_aliases: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    region_names: tuple[str, ...] = ()


class SiteLocationConfig(BaseModel):
    department: str
    region_slug: str


class AggregatorConfig(BaseModel):
    max_detail_pages: int
    location_terms: tuple[str, ...] = ()
    direct_locations: tuple[str, ...] = ()
    direct_specs: tuple[DirectAggregatorConfig, ...] = Field(default_factory=default_direct_specs)
    site_search_sources: tuple[SiteSearchConfig, ...] = Field(default_factory=default_site_search_sources)
    site_url_templates: dict[str, str] = Field(default_factory=dict)
    site_locations: dict[str, SiteLocationConfig] = Field(default_factory=dict)

    @field_validator("direct_specs", "site_search_sources", mode="before")
    @classmethod
    def _apply_provider_overrides(cls, value: object, info: ValidationInfo) -> object:
        if not isinstance(value, Mapping):
            return value
        specs = default_direct_specs() if info.field_name == "direct_specs" else default_site_search_sources()
        unknown = set(value) - {spec.source for spec in specs}
        if unknown:
            raise ValueError(f"Unknown aggregator sources: {', '.join(sorted(map(str, unknown)))}")
        if any(not isinstance(override, Mapping) or "source" in override for override in value.values()):
            raise ValueError("Aggregator overrides must be mappings keyed by source name.")
        return tuple(spec.model_dump() | dict(value.get(spec.source, {})) for spec in specs)


class PathsConfig(BaseModel):
    database_path: Path
    export_dir: Path
    log_file: Path = Path("data/job-scrapper.jsonl")

    @model_validator(mode="after")
    def resolve_project_relative_paths(self) -> PathsConfig:
        for field_name in ("database_path", "export_dir", "log_file"):
            path = getattr(self, field_name)
            if not path.is_absolute():
                setattr(self, field_name, PROJECT_ROOT / path)
        return self

    @property
    def excel_path(self) -> Path:
        return self.export_dir / "jobs.xlsx"

    @property
    def csv_path(self) -> Path:
        return self.export_dir / "jobs.csv"

    @property
    def raw_csv_path(self) -> Path:
        return self.export_dir / "jobs_raw.csv"


DEFAULT_EXCEL_COLUMNS = (
    "Postulé",
    "Rejeté",
    "Lien",
    "Intitulé",
    "Entreprise",
    "Lieu",
    "Contrat",
    "Télétravail",
    "Salaire min (€ annuel)",
    "Salaire max (€ annuel)",
    "Salaire",
    "Publié le",
    "Sources",
    "Recherche",
)


class ExportConfig(BaseModel):
    excel_columns: tuple[str, ...] = DEFAULT_EXCEL_COLUMNS
    repair_fields_enabled_by_default: bool = True
    repair_fields_by_source: dict[str, bool] = Field(default_factory=dict)
    direct_links_enabled_by_source: dict[str, bool] = Field(default_factory=dict)


JobOfferField = Literal["title", "company", "location", "description"]


class DedupeFieldsConfig(BaseModel):
    primary: tuple[JobOfferField, ...] = ("title", "company")
    fallback: tuple[JobOfferField, ...] = ("location",)
    description: tuple[JobOfferField, ...] = ("description", "company", "location")

    @field_validator("primary")
    @classmethod
    def _primary_fields_must_not_be_empty(cls, value: tuple[JobOfferField, ...]) -> tuple[JobOfferField, ...]:
        if not value:
            raise ValueError("At least one primary dedupe field must be configured.")
        return value

    @field_validator("description")
    @classmethod
    def _description_fields_must_start_with_description(
        cls, value: tuple[JobOfferField, ...]
    ) -> tuple[JobOfferField, ...]:
        if value and value[0] != "description":
            raise ValueError("Description dedupe fields must start with 'description'.")
        return value


class Settings(BaseSettings):
    if TYPE_CHECKING:
        # Pydantic also loads required fields from YAML and the environment.
        def __init__(self, **values: object) -> None: ...

    app_env: str = "dev"
    queries: tuple[str, ...]
    location_label: str = ""
    locations: tuple[str, ...]
    country: str
    distance_km: int
    location_scope: LocationScopeConfig = Field(default_factory=LocationScopeConfig)
    exclude_terms: tuple[str, ...]
    include_terms: tuple[str, ...]
    excluded_title_patterns: tuple[str, ...] = ()
    enabled_sources: dict[str, bool] = Field(default_factory=dict)
    excluded_companies_by_source: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    dedupe_fields: DedupeFieldsConfig = Field(default_factory=DedupeFieldsConfig)
    dedupe_source_priority: dict[str, int] = Field(default_factory=dict)
    dedupe_default_source_priority: int = 100
    default_hours_old: int
    jobspy_hours_old_override: int | None = None
    jobspy_batch_sites: bool = True
    overlap_hours: int
    results_wanted_per_query: int
    custom_max_pages: int
    refresh_known_details: bool
    custom_source_concurrency: int = 4
    http_max_connections: int = 12
    http_per_host_concurrency: int = 2
    http_request_delay_secs: float = 0.25
    user_agent: str
    paths: PathsConfig
    export: ExportConfig = Field(default_factory=ExportConfig)
    http: HttpConfig
    jobspy: JobspyConfig
    welcome_to_the_jungle: WelcomeToTheJungleConfig = Field(default_factory=WelcomeToTheJungleConfig)
    aggregators: AggregatorConfig

    model_config = SettingsConfigDict(
        env_nested_delimiter="__",
        env_prefix="SCRAPPER_",
        env_file=".env",
        env_file_encoding="utf-8",
        yaml_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        assert isinstance(init_settings, InitSettingsSource)
        app_env = init_settings.init_kwargs.get("app_env") or EnvironmentSettings().app_env
        yaml_override = init_settings.init_kwargs.pop("yaml_file", None)
        profile = init_settings.init_kwargs.pop("_profile", None)
        config_sources = init_settings.init_kwargs.pop("_config_sources", None)
        yaml_files = yaml_override or _yaml_config_files(app_env, profile)
        if isinstance(yaml_files, (str, Path)):
            yaml_files = [yaml_files]
        sources = {
            type(init_settings).__name__: "command line",
            type(env_settings).__name__: "environment",
            type(dotenv_settings).__name__: str(Path(".env").absolute()),
        }
        yaml_sources = []
        for path in reversed(yaml_files):
            path = Path(path).expanduser().absolute()
            if path.is_file():
                source = YamlConfigSettingsSource(settings_cls, yaml_file=path)
                source.__name__ = str(path)
                sources[source.__name__] = str(path)
                yaml_sources.append(source)
        settings_sources = (init_settings, env_settings, dotenv_settings, *yaml_sources)
        if config_sources is None:
            return settings_sources
        return (*settings_sources, _ConfigSources(settings_cls, sources, config_sources))

    def describe(self, sources: dict[str, str]) -> SettingsReport:
        values = self.model_dump(mode="json")
        paths = map(".".join, _leaf_paths(values))
        return {"values": values, "sources": {path: sources.get(path, "default") for path in paths}}

    @property
    def database_path(self) -> Path:
        return self.paths.database_path

    @property
    def export_dir(self) -> Path:
        return self.paths.export_dir

    @property
    def log_file(self) -> Path:
        return self.paths.log_file

    @property
    def excel_path(self) -> Path:
        return self.paths.excel_path

    @property
    def csv_path(self) -> Path:
        return self.paths.csv_path

    @property
    def raw_csv_path(self) -> Path:
        return self.paths.raw_csv_path

    @property
    def search_locations(self) -> tuple[str, ...]:
        return self.locations

    @field_validator("location_scope", mode="before")
    @classmethod
    def _location_coordinates_are_derived(cls, value: object) -> object:
        if isinstance(value, Mapping) and {"centers", "city_coordinates", "region_names"}.intersection(value):
            raise ValueError("Set locations by municipality name. The scraper resolves coordinates automatically.")
        return value

    @model_validator(mode="after")
    def set_search_defaults(self) -> Settings:
        if not self.location_label:
            self.location_label = " / ".join(self.locations)
        if "radius_km" not in self.location_scope.model_fields_set:
            self.location_scope = self.location_scope.model_copy(update={"radius_km": self.distance_km})
        return self

    def is_source_enabled(self, source: str) -> bool:
        return self.enabled_sources.get(source, True)

    @field_validator("excluded_title_patterns")
    @classmethod
    def _valid_title_patterns(cls, patterns: tuple[str, ...]) -> tuple[str, ...]:
        for pattern in patterns:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"Invalid excluded title pattern: {pattern}") from exc
        return patterns

    def title_is_allowed(self, title: object | None) -> bool:
        return matches_job_title(title, self.include_terms, self.excluded_title_patterns)

    def is_company_excluded(self, source: str, company: object | None) -> bool:
        normalized_company = normalize_text(company)
        if not normalized_company:
            return False
        excluded = self.excluded_companies_by_source.get(source, ())
        excluded += self.excluded_companies_by_source.get("*", ())
        return normalized_company in {normalize_text(value) for value in excluded}


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

DEFAULT_QUERIES = settings.queries
DEFAULT_EXCLUDE_TERMS = settings.exclude_terms
DEFAULT_INCLUDE_TERMS = settings.include_terms
