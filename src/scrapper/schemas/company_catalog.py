"""Validate the structure and contents of the employer catalog."""

import pandas as pd
import pandera.pandas as pa
from pandera.api.pandas.model_config import BaseConfig
from pandera.typing import Series

from scrapper.text import company_name_key


class CompanyCatalogSchema(pa.DataFrameModel):
    company: Series[str]
    type: Series[str] = pa.Field(str_matches=r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*\Z")
    sources: Series[str] = pa.Field(str_matches=r"https?://[^\s|]+(?: \| https?://[^\s|]+)*\Z")

    class Config(BaseConfig):
        strict = True
        unique_column_names = True

    @pa.check("company")
    @classmethod
    def usable_company_name(cls, companies: pd.Series) -> pd.Series:
        return companies.map(company_name_key).ne("")

    @pa.dataframe_check
    @classmethod
    def has_entries(cls, catalog: pd.DataFrame) -> bool:
        return not catalog.empty

    @pa.dataframe_check
    @classmethod
    def unique_company_type(cls, catalog: pd.DataFrame) -> pd.Series:
        normalized = catalog.assign(company=catalog["company"].map(company_name_key, na_action="ignore"))
        return ~normalized.duplicated(["company", "type"], keep=False)
