from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator, Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, TypedDict, cast

from pydantic import AliasChoices, BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    EnvSettingsSource,
    InitSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

from scrapper.text import matches_job_title, normalize_text

PACKAGE_CONFIG_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_CONFIG_DIR.parents[2]
LOCAL_CONFIG_FILE = PACKAGE_CONFIG_DIR / "config.local.yaml"


def _migrate_aggregator_options(values: dict[str, object]) -> None:
    options = {}
    legacy_search = values.pop("web_search", {})
    if isinstance(legacy_search, dict) and "location_terms" in legacy_search:
        options["location_terms"] = legacy_search["location_terms"]
    if "web_search_max_detail_pages" in values:
        options["max_detail_pages"] = values.pop("web_search_max_detail_pages")
    if options:
        current = cast("Mapping[str, object] | None", values.get("aggregators"))
        values["aggregators"] = {**options, **(current or {})}


def _migrate_aggregator_env(source: EnvSettingsSource) -> None:
    values = dict(source.env_vars)
    legacy_search = values.pop("scrapper_web_search", None)
    if legacy_search:
        search = json.loads(legacy_search)
        if isinstance(search, dict) and "location_terms" in search:
            values.setdefault("scrapper_web_search__location_terms", json.dumps(search["location_terms"]))
    for old, new in (
        ("scrapper_web_search__location_terms", "scrapper_aggregators__location_terms"),
        ("scrapper_web_search_max_detail_pages", "scrapper_aggregators__max_detail_pages"),
    ):
        if old in values:
            values.setdefault(new, values.pop(old))
    source.env_vars = values


def _yaml_config_files(app_env: str) -> list[Path]:
    files = [
        PACKAGE_CONFIG_DIR / "config.yaml",
        PACKAGE_CONFIG_DIR / f"config.{app_env}.yaml",
    ]
    if os.getenv("SCRAPPER_DISABLE_LOCAL_CONFIG") not in {"1", "true", "TRUE", "yes", "YES"}:
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
                if path in {("SCRAPPER_APP_ENV",), ("APP_ENV",)}:
                    path = ("app_env",)
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
    app_env: str = Field(default="dev", validation_alias=AliasChoices("SCRAPPER_APP_ENV", "APP_ENV"))

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        validate_by_alias=True,
        validate_by_name=True,
    )


class JobspyConfig(BaseModel):
    google_zero_results_error: str
    glassdoor_zero_results_error: str
    glassdoor_results_wanted_per_query: int
    google_query_places: tuple[str, ...]
    google_query_exclude_terms: tuple[str, ...]
    glassdoor_regional_fallback_term: str
    glassdoor_location_override: str


class HttpConfig(BaseModel):
    accept_language: str


class WelcomeToTheJungleConfig(BaseModel):
    source: str
    site_url: str
    algolia_url: str
    algolia_app_id: str
    algolia_api_key: SecretStr | None = None
    job_index: str
    around_lat_lng: str
    around_radius: int | None = Field(default=None, gt=0)
    office_country_filter: str


class LocationCenterConfig(BaseModel):
    name: str
    latitude: float
    longitude: float
    aliases: tuple[str, ...] = ()


class LocationScopeConfig(BaseModel):
    enabled: bool = True
    radius_km: float
    centers: tuple[LocationCenterConfig, ...]
    city_coordinates: dict[str, tuple[float, float]] = Field(default_factory=dict)


class DirectAggregatorConfig(BaseModel):
    source: str
    search_url_templates: tuple[str, ...]
    link_prefixes: tuple[str, ...]
    base_url: str
    max_links: int
    query_style: str = "quote_plus"
    use_direct_locations: bool = False
    blocked_direct_hosts: tuple[str, ...] = ()
    blocked_provider_terms: tuple[str, ...] = ()


class SiteSearchConfig(BaseModel):
    source: str
    # Keep the persisted query key stable for incremental searches.
    query_template: str
    host: str
    search_url_templates: tuple[str, ...] = ()
    search_urls_by_location: dict[str, tuple[str, ...]] = Field(default_factory=dict)


class SiteLocationConfig(BaseModel):
    department: str
    region_slug: str


class AggregatorConfig(BaseModel):
    source: str
    max_detail_pages: int
    location_terms: tuple[str, ...]
    direct_locations: tuple[str, ...]
    direct_specs: tuple[DirectAggregatorConfig, ...]
    site_search_sources: tuple[SiteSearchConfig, ...]
    site_url_templates: dict[str, str] = Field(default_factory=dict)
    site_locations: dict[str, SiteLocationConfig] = Field(default_factory=dict)


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
    app_env: str = Field(default="dev", validation_alias=AliasChoices("SCRAPPER_APP_ENV", "APP_ENV"))
    queries: tuple[str, ...]
    location_label: str
    locations: tuple[str, ...]
    country: str
    distance_km: int
    location_scope: LocationScopeConfig
    exclude_terms: tuple[str, ...]
    include_terms: tuple[str, ...]
    excluded_title_patterns: tuple[str, ...] = ()
    jobspy_sites: tuple[str, ...]
    custom_sites: tuple[str, ...]
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
    custom_max_detail_pages: int
    refresh_known_details: bool
    custom_source_concurrency: int = 4
    http_max_connections: int = 12
    http_per_host_concurrency: int = 2
    http_request_delay_secs: float = 0.25
    user_agent: str
    extra_headers: dict[str, str] = Field(default_factory=dict)
    paths: PathsConfig
    export: ExportConfig = Field(default_factory=ExportConfig)
    http: HttpConfig
    jobspy: JobspyConfig
    welcome_to_the_jungle: WelcomeToTheJungleConfig
    aggregators: AggregatorConfig

    @field_validator("locations")
    @classmethod
    def _locations_must_not_be_empty(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("At least one search location must be configured.")
        return value

    def __init__(self, **data: object) -> None:
        _migrate_aggregator_options(data)
        if "app_env" in data and "yaml_file" not in data:
            data["yaml_file"] = _yaml_config_files(str(data["app_env"]))
        paths = dict(cast("Mapping[str, object] | None", data.get("paths")) or {})
        for field_name in ("database_path", "export_dir", "log_file"):
            if field_name in data:
                paths[field_name] = data.pop(field_name)
        if paths:
            data["paths"] = paths
        super().__init__(**cast("dict[str, Any]", data))

    model_config = SettingsConfigDict(
        env_nested_delimiter="__",
        env_prefix="SCRAPPER_",
        env_file=".env",
        env_file_encoding="utf-8",
        yaml_file=_yaml_config_files("dev"),
        yaml_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        validate_by_alias=True,
        validate_by_name=True,
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
        config_sources = init_settings.init_kwargs.pop("_config_sources", None)
        yaml_files = yaml_override or _yaml_config_files(app_env)
        if isinstance(yaml_files, (str, Path)):
            yaml_files = [yaml_files]
        for source in (env_settings, dotenv_settings):
            if isinstance(source, EnvSettingsSource):
                _migrate_aggregator_env(source)
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
                _migrate_aggregator_options(source.init_kwargs)
                source.__name__ = str(path)
                sources[source.__name__] = str(path)
                yaml_sources.append(source)
        settings_sources = (init_settings, env_settings, dotenv_settings, *yaml_sources)
        if config_sources is None:
            return settings_sources
        return (*settings_sources, _ConfigSources(settings_cls, sources, config_sources))

    def describe(self, sources: dict[str, str]) -> SettingsReport:
        values = self.model_dump(mode="json")
        values["extra_headers"] = dict.fromkeys(self.extra_headers, "**********")
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

    def is_source_enabled(self, source: str) -> bool:
        return self.enabled_sources.get(source, True)

    def is_custom_source_enabled(self, source: str) -> bool:
        return source in self.custom_sites and self.is_source_enabled(source)

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
