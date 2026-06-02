from __future__ import annotations

import pytest
from hypothesis import given, strategies as st

from scrapper.config import DEFAULT_EXCLUDE_TERMS, DEFAULT_INCLUDE_TERMS
from scrapper.text import (
    clean_text,
    completeness_score,
    contains_any_terms,
    contains_excluded_terms,
    description_dedupe_key,
    dedupe_key,
    has_excluded_contract_signal,
    normalize_text,
    stable_hash,
    url_key,
)


def test_clean_text_strips_html_entities_and_whitespace() -> None:
    assert clean_text("  <p>Example&nbsp;<strong>role</strong></p>\n") == "Example role"


@pytest.mark.parametrize(
    ("title", "employment_type", "description"),
    [
        ("Internship example role", "Internship", "Mission de 6 mois"),
        ("Example coordinator", "Alternance", "Example description"),
        ("Example manager", "CDI", "Contrat de professionnalisation à pourvoir"),
        ("Sample specialist", "CDD", "Nous recherchons un alternant example role"),
        ("Offre d’alternance / Community Manager (H/F)", "Temps plein", "Office de tourisme"),
        ("Mission bénévole non rémunérée : Communication digitale", "VOLUNTEER", "Association locale"),
    ],
)
def test_excluded_contract_signals_are_rejected(title: str, employment_type: str, description: str) -> None:
    assert has_excluded_contract_signal(title, employment_type, description, DEFAULT_EXCLUDE_TERMS)


@pytest.mark.parametrize(
    ("title", "employment_type", "description"),
    [
        ("Example internal role", "Full-time", "Deployment of an internal plan."),
        ("Example international role", "Full-time", "Coordination across regions."),
        ("Example coordinator", "Contract", "You mentor an apprentice on the team."),
    ],
)
def test_filter_keeps_non_student_contract_contexts(title: str, employment_type: str, description: str) -> None:
    assert not has_excluded_contract_signal(title, employment_type, description, DEFAULT_EXCLUDE_TERMS)


def test_include_terms_are_title_focused_for_example_roles() -> None:
    assert contains_any_terms(("Example coordinator",), DEFAULT_INCLUDE_TERMS)
    assert contains_any_terms(("Sample manager",), DEFAULT_INCLUDE_TERMS)
    assert not contains_any_terms(("Responsable administratif",), DEFAULT_INCLUDE_TERMS)


def test_excluded_terms_are_word_based_not_substrings() -> None:
    assert not contains_excluded_terms(("example international role",), DEFAULT_EXCLUDE_TERMS)
    assert not contains_excluded_terms(("example internal role",), DEFAULT_EXCLUDE_TERMS)
    assert contains_excluded_terms(("internship example role",), DEFAULT_EXCLUDE_TERMS)


def test_url_key_ignores_tracking_params_but_preserves_known_job_ids() -> None:
    assert url_key("https://example.com/jobs/123?utm_source=x") == "example.com/jobs/123"
    assert (
        url_key("https://www.linkedin.com/jobs/view/current?currentJobId=42&utm_source=x")
        == "www.linkedin.com/jobs/view/current?currentJobId=42"
    )
    assert url_key("https://www.glassdoor.fr/job-listing/j?jl=1010154208293") == (
        "www.glassdoor.fr/job-listing/j?jl=1010154208293"
    )


@pytest.mark.property
@given(st.text(), st.text(), st.text())
def test_stable_hash_is_deterministic_for_arbitrary_text(a: str, b: str, c: str) -> None:
    assert stable_hash(a, b, c) == stable_hash(a, b, c)


JOB_TEXT = st.text(
    alphabet=list("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 éèêëàâçùûôîï-'"),
    min_size=1,
    max_size=80,
)


@pytest.mark.property
@given(JOB_TEXT, JOB_TEXT)
def test_dedupe_key_normalizes_accents_and_case(title: str, company: str) -> None:
    left = dedupe_key(title.upper(), company.upper())
    right = dedupe_key(normalize_text(title), normalize_text(company))
    assert left == right


def test_description_dedupe_key_requires_substantial_text_and_normalizes_case() -> None:
    rich_description = (
        "Build and coordinate the editorial plan across campaigns, stakeholders, and reporting, "
        "while owning copy review and publication workflows."
    )

    assert description_dedupe_key("Too short", company="Example", location="Paris") is None
    assert description_dedupe_key(rich_description.upper(), company="EXAMPLE", location="PARIS") == description_dedupe_key(
        normalize_text(rich_description),
        company="example",
        location="paris",
    )


@pytest.mark.property
@given(st.text(min_size=1, max_size=200))
def test_completeness_score_increases_when_description_is_added(description: str) -> None:
    base = {"title": "Example coordinator", "company": "Example Organization"}
    enriched = {**base, "description": description}
    assert completeness_score(enriched) >= completeness_score(base)
