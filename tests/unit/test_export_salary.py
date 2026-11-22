from __future__ import annotations

import pandas as pd
import pytest

from scrapper.salary import _review_note, salary_bounds


def test_salary_bounds_leave_untrusted_non_annual_units_empty() -> None:
    row = pd.Series(
        {
            "min_amount": None,
            "max_amount": None,
            "salary": {
                "@type": "MonetaryAmount",
                "currency": "EUR",
                "value": {
                    "@type": "QuantitativeValue",
                    "minValue": 20,
                    "maxValue": 30,
                    "unitText": "HOUR",
                },
            },
            "raw_json": None,
        }
    )

    assert salary_bounds(row.get) == (None, None)


@pytest.mark.parametrize(
    ("minimum", "maximum", "unit", "currency", "expected"),
    [
        (2400, 2500, "monthly", "EUR", (28800, 30000)),
        (2900, 2900, "MONTH", "EUR", (34800, 34800)),
        (36000, 44000, "yearly", "EUR", (36000, 44000)),
        (40, 56, "hourly", "EUR", (None, None)),
        (300, 400, "daily", "EUR", (None, None)),
        (2400, 2500, None, "EUR", (None, None)),
        (40000, 45000, "yearly", "USD", (None, None)),
        (3000, 2000, "monthly", "EUR", (None, None)),
        (-100, 2000, "monthly", "EUR", (None, None)),
        (None, 3000, "monthly", "EUR", (None, 36000)),
        (3000, None, "monthly", "EUR", (36000, None)),
    ],
)
def test_audit_salary_units_and_unreliable_ranges(
    minimum: int | None, maximum: int | None, unit: str | None, currency: str, expected: tuple[int | None, int | None]
) -> None:
    assert (
        salary_bounds(
            pd.Series(
                {
                    "min_amount": minimum,
                    "max_amount": maximum,
                    "interval": unit,
                    "currency": currency,
                }
            ).get
        )
        == expected
    )


def test_review_notes_distinguish_teaching_recruitment_from_mentoring() -> None:
    assert "formation" in _review_note(pd.Series({"description": "Nous recrutons des formateurs/trices indépendants."}))
    assert not _review_note(
        pd.Series({"description": "Vous encadrez les stagiaires et préparez les contenus marketing."})
    )


def test_export_flags_expiry_and_uses_only_unambiguous_text_salary() -> None:
    assert salary_bounds(pd.Series({"description": "Rémunération : entre 39 000€ et 41600€ par an"}).get) == (
        39000,
        41600,
    )
    assert salary_bounds(pd.Series({"description": "Salaire : 2\u00a0000€ brut par mois"}).get) == (24000, 24000)
    assert salary_bounds(pd.Series({"description": "Budget publicitaire : 42 000€ par an"}).get) == (None, None)
    assert salary_bounds(pd.Series({"description": "Salaire : 50 000 USD par an"}).get) == (None, None)
    assert "Date limite dépassée" in _review_note(
        pd.Series({"description": "Example description", "raw_json": {"json_ld": {"validThrough": "2020-04-04"}}})
    )
