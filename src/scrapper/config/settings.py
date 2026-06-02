from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, BaseModel, Field, SecretStr, field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)


PACKAGE_CONFIG_DIR = Path(__file__).resolve().parent
LOCAL_CONFIG_FILE = PACKAGE_CONFIG_DIR / "config.local.yaml"


def _yaml_config_files(app_env: str) -> list[Path]:
    files = [
        PACKAGE_CONFIG_DIR / "config.yaml",
        PACKAGE_CONFIG_DIR / f"config.{app_env}.yaml",
    ]
    if os.getenv("SCRAPPER_DISABLE_LOCAL_CONFIG") not in {"1", "true", "TRUE", "yes", "YES"}:
        files.append(LOCAL_CONFIG_FILE)
    return files


class EnvironmentSettings(BaseSettings):
    app_env: str = Field(default="dev", validation_alias=AliasChoices("SCRAPPER_APP_ENV", "APP_ENV"))

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )


class SearchEnginesConfig(BaseModel):
    google_domain: str
    gl: str
    hl: str


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


class WebSearchConfig(BaseModel):
    source: str
    duckduckgo_html_url: str
    query_template: str
    excluded_domains: tuple[str, ...]
    location_terms: tuple[str, ...]
    info_path_markers: tuple[str, ...]
    job_signal_terms: tuple[str, ...]


class HelloworkConfig(BaseModel):
    source: str
    base_url: str


class WelcomeToTheJungleConfig(BaseModel):
    source: str
    site_url: str
    algolia_url: str
    algolia_app_id: str
    algolia_api_key: SecretStr | None = None
    job_index: str
    around_lat_lng: str
    around_radius: int
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


class SiteSearchConfig(BaseModel):
    source: str
    query_template: str
    host: str


class AggregatorConfig(BaseModel):
    source: str
    direct_locations: tuple[str, ...]
    direct_specs: tuple[DirectAggregatorConfig, ...]
    site_search_sources: tuple[SiteSearchConfig, ...]


class PathsConfig(BaseModel):
    database_path: Path
    export_dir: Path

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
    def _description_fields_must_start_with_description(cls, value: tuple[JobOfferField, ...]) -> tuple[JobOfferField, ...]:
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
    jobspy_sites: tuple[str, ...]
    custom_sites: tuple[str, ...]
    enabled_sources: dict[str, bool] = Field(default_factory=dict)
    dedupe_fields: DedupeFieldsConfig = Field(default_factory=DedupeFieldsConfig)
    dedupe_source_priority: dict[str, int] = Field(default_factory=dict)
    dedupe_default_source_priority: int = 100
    default_hours_old: int
    jobspy_hours_old_override: int | None = None
    overlap_hours: int
    results_wanted_per_query: int
    custom_max_pages: int
    custom_max_detail_pages: int
    web_search_max_results_per_query: int
    web_search_max_detail_pages: int
    refresh_known_details: bool
    request_delay_secs: float
    user_agent: str
    extra_headers: dict[str, str] = Field(default_factory=dict)
    paths: PathsConfig
    export: ExportConfig = Field(default_factory=ExportConfig)
    http: HttpConfig
    search_engines: SearchEnginesConfig
    jobspy: JobspyConfig
    web_search: WebSearchConfig
    hellowork: HelloworkConfig
    welcome_to_the_jungle: WelcomeToTheJungleConfig
    aggregators: AggregatorConfig

    @field_validator("locations")
    @classmethod
    def _locations_must_not_be_empty(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("At least one search location must be configured.")
        return value

    def __init__(self, **data):
        if "app_env" in data and "yaml_file" not in data:
            data["yaml_file"] = _yaml_config_files(str(data["app_env"]))
        paths = dict(data.get("paths") or {})
        for field_name in ("database_path", "export_dir"):
            if field_name in data:
                paths[field_name] = data.pop(field_name)
        if paths:
            data["paths"] = paths
        super().__init__(**data)

    model_config = SettingsConfigDict(
        env_nested_delimiter="__",
        env_prefix="SCRAPPER_",
        env_file=".env",
        env_file_encoding="utf-8",
        yaml_file=_yaml_config_files("dev"),
        yaml_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,  # noqa: ARG003
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        app_env = init_settings.init_kwargs.get("app_env") or EnvironmentSettings().app_env
        yaml_override = init_settings.init_kwargs.pop("yaml_file", None)
        yaml_source = (
            YamlConfigSettingsSource(settings_cls, yaml_file=yaml_override, deep_merge=True)
            if yaml_override
            else YamlConfigSettingsSource(settings_cls, yaml_file=_yaml_config_files(app_env), deep_merge=True)
        )
        return init_settings, env_settings, dotenv_settings, yaml_source

    @property
    def database_path(self) -> Path:
        return self.paths.database_path

    @property
    def export_dir(self) -> Path:
        return self.paths.export_dir

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


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

DEFAULT_QUERIES = settings.queries
DEFAULT_EXCLUDE_TERMS = settings.exclude_terms
DEFAULT_INCLUDE_TERMS = settings.include_terms
