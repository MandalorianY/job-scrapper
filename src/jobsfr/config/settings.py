from __future__ import annotations

import os
from collections import ChainMap
from collections.abc import Iterator, Mapping
from hashlib import sha256
from pathlib import Path
from typing import Literal, TypedDict

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    NonNegativeInt,
    PositiveInt,
    PrivateAttr,
    SecretStr,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_settings import (
    BaseSettings,
    DotEnvSettingsSource,
    EnvSettingsSource,
    InitSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from jobsfr.catalogs.updates import resolve_company_catalog
from jobsfr.config.aggregator_specs import (
    DirectAggregatorConfig,
    SiteSearchConfig,
    default_direct_specs,
    default_site_search_sources,
)
from jobsfr.config.source_names import AGGREGATORS_SOURCE, JOBSPY_SITES, WTTJ_SOURCE
from jobsfr.config.user_paths import user_config_dir, user_data_dir, user_export_dir, user_log_dir
from jobsfr.config.yaml_settings import read_yaml_settings
from jobsfr.export.tracker import DEFAULT_LOCALIZED_TRACKER_COLUMNS, validate_tracker_columns
from jobsfr.offers.filters import ListingFilters

PACKAGE_CONFIG_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_CONFIG_DIR.parents[2] if PACKAGE_CONFIG_DIR.parents[1].name == "src" else None
LOCAL_CONFIG_FILE = PACKAGE_CONFIG_DIR / "config.local.yaml"
EXAMPLE_CONFIG_FILE = (
    PROJECT_ROOT / "docs" if PROJECT_ROOT is not None else PACKAGE_CONFIG_DIR
) / "config.local.example.yaml"


class _BootstrapSettings(BaseSettings):
    use_working_directory: bool = False
    disable_local_config: bool = False

    # These settings are needed to locate the configuration files.
    model_config = SettingsConfigDict(env_prefix="JOBSFR_", env_file=None, env_ignore_empty=True)

    @field_validator("use_working_directory", "disable_local_config", mode="before")
    @classmethod
    def _validate_flag_value(cls, value: object) -> object:
        if isinstance(value, str) and value.lower() not in {"1", "true", "0", "false"}:
            raise ValueError("Use 1 or true to enable, or 0 or false to disable.")
        return value


def _use_installed_paths() -> bool:
    return PROJECT_ROOT is None and not _BootstrapSettings().use_working_directory


def default_local_config() -> Path:
    if PROJECT_ROOT is not None:
        return LOCAL_CONFIG_FILE
    if _use_installed_paths():
        return user_config_dir() / "config.yaml"
    return Path.cwd() / LOCAL_CONFIG_FILE.name


def default_dotenv_path() -> Path:
    if _use_installed_paths() and (path := user_config_dir() / ".env").is_file():
        return path
    return Path.cwd() / ".env"


def yaml_config_files(app_env: str | None, profile: Path | None = None) -> list[Path]:
    files = [PACKAGE_CONFIG_DIR / "config.yaml"]
    if app_env is not None and "/" not in app_env and "\\" not in app_env:
        files.append(PACKAGE_CONFIG_DIR / f"config.{app_env}.yaml")
    if profile is not None:
        files.append(profile)
    elif not _BootstrapSettings().disable_local_config:
        files.append(default_local_config())
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


class SearchSetupRequired(ValueError):
    pass


class _ConfigSection(BaseModel):
    model_config = ConfigDict(extra="forbid")


class JobspyConfig(_ConfigSection):
    glassdoor_results_wanted_per_query: PositiveInt
    google_query_places: tuple[str, ...] = ()
    google_query_exclude_terms: tuple[str, ...]
    glassdoor_regional_fallback_term: str | None = None
    glassdoor_location_override: str | None = None


class HttpConfig(_ConfigSection):
    accept_language: str


class WelcomeToTheJungleConfig(_ConfigSection):
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


class LocationScopeConfig(_ConfigSection):
    enabled: bool = True
    include_unrecognized: bool = True
    radius_km: float = Field(default=25, ge=0, allow_inf_nan=False)
    centers: tuple[LocationCenterConfig, ...] = ()
    city_coordinates: dict[str, tuple[float, float]] = Field(default_factory=dict)
    city_aliases: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    region_names: tuple[str, ...] = ()
    known_city_names: frozenset[str] = Field(default_factory=frozenset, exclude=True)


class SiteLocationConfig(_ConfigSection):
    department: str
    region_slug: str


class AggregatorConfig(_ConfigSection):
    max_detail_pages: PositiveInt
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


class PathsConfig(_ConfigSection):
    database_path: Path = Field(
        default_factory=lambda: (user_data_dir() if _use_installed_paths() else Path("data")) / "job_search.duckdb"
    )
    export_dir: Path = Field(default_factory=lambda: user_export_dir() if _use_installed_paths() else Path("exports"))
    log_file: Path = Field(
        default_factory=lambda: (user_log_dir() if _use_installed_paths() else Path("data")) / "jobsfr.jsonl"
    )

    @model_validator(mode="after")
    def resolve_project_relative_paths(self) -> PathsConfig:
        root = PROJECT_ROOT or Path.cwd()
        for field_name in ("database_path", "export_dir", "log_file"):
            path = getattr(self, field_name).expanduser()
            setattr(self, field_name, root / path)
        return self


def profile_paths(profile: Path) -> dict[str, str]:
    # Docker mounts the profile at a temporary path. Use its host path for the same identity.
    host_path = os.environ.get("JOBSFR_PROFILE_HOST_PATH")
    identity = Path(host_path) if host_path else profile.expanduser().resolve()
    digest = sha256(os.fsencode(os.path.normcase(identity))).hexdigest()[:16]
    directory = Path("profiles") / f"{identity.stem[:48]}-{digest}"
    defaults = PathsConfig()
    paths = {
        "database_path": defaults.database_path.parent / directory / defaults.database_path.name,
        "export_dir": defaults.export_dir / directory,
        "log_file": defaults.log_file.parent / directory / defaults.log_file.name,
    }
    if not _use_installed_paths():
        root = PROJECT_ROOT or Path.cwd()
        paths = {name: path.relative_to(root) for name, path in paths.items()}
    return {name: path.as_posix() for name, path in paths.items()}


Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
WEEKDAYS: tuple[Weekday, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class ScheduleConfig(_ConfigSection):
    days: tuple[Weekday, ...] = Field(default=WEEKDAYS[:5], min_length=1)
    time: str = Field(default="15:30", pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")


ExportFormat = Literal["xlsx", "ods", "csv"]


class ExportConfig(_ConfigSection):
    formats: tuple[ExportFormat, ...] = Field(default=("xlsx", "csv"), min_length=1)
    keep_reposts: bool = True
    excel_columns: tuple[str, ...] = DEFAULT_LOCALIZED_TRACKER_COLUMNS
    repair_fields_enabled_by_default: bool = True
    repair_fields_by_source: dict[str, bool] = Field(default_factory=dict)
    direct_links_enabled_by_source: dict[str, bool] = Field(default_factory=dict)

    @field_validator("formats")
    @classmethod
    def _formats_must_be_unique(cls, value: tuple[ExportFormat, ...]) -> tuple[ExportFormat, ...]:
        if len(set(value)) != len(value):
            raise ValueError("List each export format once.")
        return value

    @field_validator("excel_columns")
    @classmethod
    def _excel_columns_must_be_valid(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        validate_tracker_columns(value)
        return value


class TrackerSyncConfig(_ConfigSection):
    enabled: bool = False
    target: str | None = None

    @model_validator(mode="after")
    def _require_target_when_enabled(self) -> TrackerSyncConfig:
        if self.enabled and not self.target:
            raise ValueError("Set sync.target to an rclone destination when sync.enabled is true.")
        return self


JobOfferField = Literal["title", "company", "location", "description"]


class DedupeFieldsConfig(_ConfigSection):
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
    app_env: str = "dev"
    queries: tuple[str, ...]
    location_label: str = ""
    locations: tuple[str, ...]
    country: str
    distance_km: NonNegativeInt
    location_scope: LocationScopeConfig = Field(default_factory=LocationScopeConfig)
    filters: ListingFilters = Field(default_factory=ListingFilters)
    company_catalog_path: Path | None = None
    enabled_sources: dict[str, bool] = Field(default_factory=dict)
    dedupe_fields: DedupeFieldsConfig = Field(default_factory=DedupeFieldsConfig)
    dedupe_source_priority: dict[str, int] = Field(default_factory=dict)
    dedupe_default_source_priority: int = 100
    default_hours_old: PositiveInt
    jobspy_hours_old_override: PositiveInt | None = None
    jobspy_batch_sites: bool = True
    overlap_hours: NonNegativeInt
    results_wanted_per_query: PositiveInt
    custom_max_pages: PositiveInt
    refresh_known_details: bool
    http_max_connections: PositiveInt = 12
    http_per_host_concurrency: PositiveInt = 2
    http_request_delay_secs: float = Field(default=0.25, ge=0, allow_inf_nan=False)
    user_agent: str
    paths: PathsConfig = Field(default_factory=PathsConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    export: ExportConfig = Field(default_factory=ExportConfig)
    sync: TrackerSyncConfig | None = None
    http: HttpConfig
    jobspy: JobspyConfig
    welcome_to_the_jungle: WelcomeToTheJungleConfig = Field(default_factory=WelcomeToTheJungleConfig)
    aggregators: AggregatorConfig
    _company_catalog_revision: str | None = PrivateAttr(None)

    model_config = SettingsConfigDict(
        env_nested_delimiter="__",
        env_prefix="JOBSFR_",
        env_file=".env",
        env_file_encoding="utf-8",
        dotenv_filtering="only_existing",
        extra="forbid",
        case_sensitive=False,
    )

    def __init__(
        self, *, _refresh_company_catalog: bool = False, _require_search_setup: bool = False, **values: object
    ) -> None:
        sources: dict[str, str] = {}
        if _require_search_setup:
            values["_config_sources"] = sources
        values.setdefault("_env_file", default_dotenv_path())
        super().__init__(**values)  # pyright: ignore[reportArgumentType]
        if _require_search_setup:
            defaults = {
                "default",
                str(PACKAGE_CONFIG_DIR / "config.yaml"),
                str(PACKAGE_CONFIG_DIR / f"config.{self.app_env}.yaml"),
            }
            for name, entries in (("queries", self.queries), ("locations", self.locations)):
                if not any(entry.strip() for entry in entries) or sources.get(name, "default") in defaults:
                    raise SearchSetupRequired(
                        "Set queries and locations before searching.\n\nTo create a profile, run\n\n  jobsfr setup"
                    )
        if not (types := self.filters.excluded_company_types()):
            return
        try:
            catalog = resolve_company_catalog(
                self.database_path, self.company_catalog_path, refresh=_refresh_company_catalog
            )
            self.filters = self.filters.with_company_catalog(catalog)
        except ValueError as exc:
            raise ValidationError.from_exception_data(
                type(self).__name__,
                [
                    {
                        "type": "value_error",
                        "loc": ("filters", "company", "exclude", "types"),
                        "input": sorted(types),
                        "ctx": {"error": exc},
                    }
                ],
            ) from exc
        self._company_catalog_revision = catalog.revision

    @property
    def company_catalog_revision(self) -> str | None:
        return self._company_catalog_revision

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
        assert isinstance(env_settings, EnvSettingsSource)
        assert isinstance(dotenv_settings, DotEnvSettingsSource)
        profile = init_settings.init_kwargs.pop("_profile", None)
        config_sources = init_settings.init_kwargs.pop("_config_sources", None)
        yaml_values = {
            path: read_yaml_settings(path)
            for file in yaml_config_files(None, profile)
            if (path := Path(file).expanduser().absolute()).is_file()
        }
        # Select the overlay using the same precedence as the final app_env field.
        app_env_field = settings_cls.model_fields["app_env"]
        env_value, _, _ = env_settings.get_field_value(app_env_field, "app_env")
        dotenv_value, _, _ = dotenv_settings.get_field_value(app_env_field, "app_env")
        app_env = ChainMap(
            init_settings.init_kwargs,
            *({"app_env": value} for value in (env_value, dotenv_value) if value is not None),
            *reversed(yaml_values.values()),
        ).get("app_env", app_env_field.default)
        env_file = dotenv_settings.env_file
        sources = {
            type(init_settings).__name__: "command line",
            type(env_settings).__name__: "environment",
            type(dotenv_settings).__name__: str(Path(env_file).absolute())
            if isinstance(env_file, (str, Path))
            else ".env",
        }
        yaml_sources = []
        for file in reversed(yaml_config_files(app_env if isinstance(app_env, str) else None, profile)):
            path = Path(file).expanduser().absolute()
            if path in yaml_values:
                values = yaml_values[path]
            elif path.is_file():
                values = read_yaml_settings(path)
            else:
                continue
            source = InitSettingsSource(settings_cls, values)
            source.__name__ = str(path)
            sources[source.__name__] = str(path)
            yaml_sources.append(source)
        if profile is not None:
            path_defaults = InitSettingsSource(settings_cls, {"paths": profile_paths(Path(profile))})
            path_defaults.__name__ = "profile defaults"
            sources[path_defaults.__name__] = "profile defaults"
            yaml_sources.append(path_defaults)
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
    def search_locations(self) -> tuple[str, ...]:
        return self.locations

    @field_validator("location_scope", mode="before")
    @classmethod
    def _location_coordinates_are_derived(cls, value: object) -> object:
        if isinstance(value, Mapping) and {
            "centers",
            "city_coordinates",
            "region_names",
            "known_city_names",
        }.intersection(value):
            raise ValueError("Set locations by municipality name. The scraper resolves coordinates automatically.")
        return value

    @model_validator(mode="after")
    def set_search_defaults(self) -> Settings:
        sources = {
            *JOBSPY_SITES,
            AGGREGATORS_SOURCE,
            WTTJ_SOURCE,
            *(spec.source for spec in (*self.aggregators.direct_specs, *self.aggregators.site_search_sources)),
            *(spec.source for spec in (*default_direct_specs(), *default_site_search_sources())),
        }
        if unknown := self.enabled_sources.keys() - sources:
            raise ValueError(f"Unknown enabled_sources key(s): {', '.join(sorted(unknown))}.")
        if not self.location_label:
            self.location_label = " / ".join(self.locations)
        if self.company_catalog_path is not None:
            self.company_catalog_path = (PROJECT_ROOT or Path.cwd()) / self.company_catalog_path.expanduser()
        if "radius_km" not in self.location_scope.model_fields_set:
            self.location_scope = self.location_scope.model_copy(update={"radius_km": self.distance_km})
        return self

    def is_source_enabled(self, source: str) -> bool:
        return self.enabled_sources.get(source, True)
