from __future__ import annotations

import pandas as pd
import pytest

from jobsfr.offers.salary import _review_note, salary_bounds


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


@pytest.mark.parametrize(
    ("field", "text", "expected"),
    [
        ("salary", "Annuel de 35000.0 Euros à 38000.0 Euros", (35000, 38000)),
        ("salary", "Mensuel de 2308.82 Euros - prime d'intéressement", (27706, 27706)),
        ("description", "Rémunération : 39 000,00€ à 41 600,00€ par an", (39000, 41600)),
        ("description", "Salaire brut mensuel à partir de 2497,21 €", (29967, None)),
        ("description", "Rémunération : 26 à 30K€ brut / an", (26000, 30000)),
        ("description", "Fourchette salariale €37,100.00 - €59,375.00 EUR Annuelle", (37100, 59375)),
        ("description", "Salaire 35000 EUR (€) - 38000 EUR (€) / an", (35000, 38000)),
        ("description", "La rémunération brute annuelle est à partir de 37k€.", (37000, None)),
        ("description", "Package annuel brut de 40 à 50 K€, fixe et variable compris.", (40000, 50000)),
        ("description", "Rémunération annuelle brute entre 38 460€ et 49 230€", (38460, 49230)),
        ("description", "Rémunération mensuelle brute : 2088€", (25056, 25056)),
        ("description", "Rémunération mensuelle brute: 2 534,14 euro En rejoignant l’université.", (30410, 30410)),
        ("description", "Rémunération mensuelle brute : 1929euro prime annuelle", (23148, 23148)),
        ("description", "Salaire mensuel brut pour un 80% : 1780,37€.", (21364, 21364)),
        ("description", "Salaire proposé De 32000 à 35000 € (Euros) par an", (32000, 35000)),
        (
            "description",
            "Rémunération : 2 320,86 € brut mensuel sur 14 mois, soit 32 492,10 € brut annuel",
            (32492, 32492),
        ),
        ("description", "Rémunération : 2452€ (1er échelon) à 3053€ (7ème échelon), brut mensuel", (29424, 36636)),
        ("description", "Rémunération mensuelle 2950€ bruts et 3200€ bruts prime annuelle de 1826,35€", (35400, 38400)),
        ("description", "Rémunération : régime indemnitaire d'un montant mensuel de 518 €", (None, None)),
        ("title", "Example coordinator - €26.000 - €30.000 Par An", (26000, 30000)),
        ("description", "Rémunération : 12,31€ par heure", (None, None)),
        ("description", "Salaire : 35 000 dollars par an", (None, None)),
        ("description", "Salaire estimé : 40 000 € par an", (None, None)),
        ("description", "Rémunération et avantages : carte restaurant de 100 €/mois", (None, None)),
        ("description", "Salaire de 2 800€ nets par mois", (None, None)),
        ("description", "Salaire : 2 800 € par mois ou 45 000 € par an", (None, None)),
        ("description", "Rémunération : 35k€ sur 13 mois", (None, None)),
        ("salary", "Mensuel de 28320.0 Euros à 29160.0 Euros", (None, None)),
        ("description", "Rémunération variable pouvant aller jusqu'à 1 000 € bruts par an", (None, None)),
        ("description", "Rémunération : à partir de 1,00 € par an", (None, None)),
    ],
)
def test_advertised_salary_formats(field: str, text: str, expected: tuple[int | None, int | None]) -> None:
    assert salary_bounds({field: text}.get) == expected


def test_unsourced_glassdoor_percentiles_do_not_override_advertised_pay() -> None:
    row = {"source": "glassdoor", "min_amount": 40000, "max_amount": 50000, "currency": "EUR", "interval": "yearly"}
    assert salary_bounds(row.get) == (None, None)
    assert salary_bounds({**row, "description": "Salaire : 45 000 € par an"}.get) == (45000, 45000)


def test_nested_salary_estimates_are_ignored() -> None:
    amount = {"@type": "MonetaryAmount", "currency": "EUR", "value": {"value": 43000, "unitText": "YEAR"}}
    row = {"raw_json": {"json_ld": {"estimatedSalary": {"estimatedValue": amount}}}}
    assert salary_bounds(row.get) == (None, None)


def test_civic_service_allowance_is_not_annualized_as_salary() -> None:
    row = {"title": "Service Civique – Communication", "min_amount": 620, "currency": "EUR", "interval": "month"}
    assert salary_bounds(row.get) == (None, None)
