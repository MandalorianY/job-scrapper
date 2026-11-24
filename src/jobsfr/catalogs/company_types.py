"""Load the employer catalog and look up employer types."""

import csv
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256
from io import StringIO
from pathlib import Path

import pandas as pd
from pandera.errors import SchemaError, SchemaErrors

from jobsfr.catalogs.schema import CompanyCatalogSchema
from jobsfr.offers.text import company_name_key

CATALOG_SOURCE_PATH = Path("datasets/employers/v1/company_types.csv")
_CATALOG_DIR = Path(__file__).resolve().parent
COMPANY_TYPES_PATH = (
    _CATALOG_DIR.parents[2] / CATALOG_SOURCE_PATH
    if _CATALOG_DIR.parents[1].name == "src"
    else _CATALOG_DIR / CATALOG_SOURCE_PATH.name
)


@dataclass(frozen=True)
class CompanyCatalog:
    types: dict[str, frozenset[str]]
    revision: str


def parse_company_catalog(content: str, *, name: str = "company_types.csv") -> CompanyCatalog:
    try:
        with StringIO(content.removeprefix("\ufeff"), newline="") as stream:
            rows = csv.reader(stream, strict=True)
            columns = pd.Index(next(rows, []))
            records = [row for row in rows if row]
            catalog = CompanyCatalogSchema.validate(pd.DataFrame(records, columns=columns, dtype="str"))
    except (csv.Error, SchemaError, SchemaErrors, ValueError) as exc:
        raise ValueError(f"{name}: {exc}") from exc

    names: dict[str, set[str]] = defaultdict(set)
    for company, kind in catalog[["company", "type"]].itertuples(index=False, name=None):
        names[kind].add(company_name_key(company))
    return CompanyCatalog(
        types={kind: frozenset(companies) for kind, companies in names.items()},
        revision=sha256(content.encode("utf-8")).hexdigest(),
    )


def read_company_catalog(path: Path) -> CompanyCatalog:
    return parse_company_catalog(path.read_bytes().decode("utf-8"), name=path.name)


@lru_cache(maxsize=1)
def bundled_company_catalog() -> CompanyCatalog:
    return read_company_catalog(COMPANY_TYPES_PATH)


def company_types() -> dict[str, frozenset[str]]:
    return bundled_company_catalog().types


def has_company_type(value: object, types: tuple[str, ...], catalog: CompanyCatalog | None = None) -> bool:
    if not isinstance(value, str) or not types:
        return False
    key = company_name_key(value)
    available = (catalog or bundled_company_catalog()).types
    return any(key in available[kind] for kind in types)
