from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from scrapper.config import SearchConfig
from scrapper.filters import RegexRule, TextFilter
from scrapper.text import (
    clean_text,
    dedupe_key,
    description_dedupe_key,
    has_excluded_contract_signal,
    listing_url_key,
    normalize_text,
    url_key,
)

SEARCH_CONFIG = SearchConfig()


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
    assert has_excluded_contract_signal(title, employment_type, description)


@pytest.mark.parametrize(
    ("title", "employment_type", "description"),
    [
        ("Example internal role", "Full-time", "Deployment of an internal plan."),
        ("Example international role", "Full-time", "Coordination across regions."),
        ("Example coordinator", "Contract", "You mentor an apprentice on the team."),
    ],
)
def test_filter_keeps_non_student_contract_contexts(title: str, employment_type: str, description: str) -> None:
    assert not has_excluded_contract_signal(title, employment_type, description)


def test_include_terms_are_title_focused_for_example_roles() -> None:
    assert SEARCH_CONFIG.filters.title.allows("Example coordinator")
    assert SEARCH_CONFIG.filters.title.allows("Sample manager")
    assert not SEARCH_CONFIG.filters.title.allows("Responsable administratif")


def test_url_key_ignores_tracking_params_but_preserves_known_job_ids() -> None:
    assert url_key("https://isarta.fr/?job=123&utm_source=search") == "isarta.fr?job=123"
    assert url_key("https://isarta.fr/?job=456") != url_key("https://isarta.fr/?job=123")
    assert url_key("https://example.com/jobs/123?utm_source=x") == "example.com/jobs/123"
    assert (
        url_key("https://www.linkedin.com/jobs/view/current?currentJobId=42&utm_source=x")
        == "www.linkedin.com/jobs/view/current?currentJobId=42"
    )
    assert url_key("https://www.glassdoor.fr/job-listing/j?jl=1010154208293") == (
        "www.glassdoor.fr/job-listing/j?jl=1010154208293"
    )


def test_shared_recruitment_detail_path_requires_a_job_id() -> None:
    assert listing_url_key("https://recruitment.example/front-jobs-detail.html?id_origin=1") is None
    assert (
        listing_url_key("https://careers.example/front-jobs-detail.html?id_job=10&id_origin=1")
        == "careers.example/front-jobs-detail.html?id_job=10"
    )


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
    assert description_dedupe_key(
        rich_description.upper(), company="EXAMPLE", location="PARIS"
    ) == description_dedupe_key(
        normalize_text(rich_description),
        company="example",
        location="paris",
    )


@pytest.mark.parametrize(
    "title",
    [
        "Assistant Communication - Marketing en Alt H/F",
        "Alt - Assistant(e) Administratif(ve) - Communication H/F",
        "Bts com alt chargé de communication (H/F)",
        "Responsable communication (bénévolat de compétence)",
        "JOB ÉTUDIANT : Chargé de projet marketing",
    ],
)
def test_audit_rejects_abbreviated_student_and_volunteer_titles(title: str) -> None:
    assert has_excluded_contract_signal(title, "FULL_TIME", None)


@pytest.mark.parametrize(
    "description",
    [
        "Assistant en contrat d’apprentissage à partir de septembre.",
        "Stage d'un ou deux mois temps plein 35h.",
        "Type d’alternance : Apprentissage. Type de contrat : CDD.",
        "Type d'emploi : alternance. Rémunération 850 euros.",
        "Ce stage est fait pour vous si vous préparez un Bac+3.",
        "Lieu du stage : Toulouse.",
        "Alternance à compter de septembre 2026.",
        "Dans le cadre d'une alternance, nous recrutons.",
        "Assistant marketing (Alternance sur 1 an).",
        "ALTERNANCE MARKETING / COMMUNICATION. Missions variées.",
        "Nous cherchons un(e) alternant(e) en marketing.",
        "On cherche notre futur alternant pour nous aider.",
        "Le poste est bénévole et nécessite quelques heures par semaine.",
        "This position is an internship.",
    ],
)
def test_audit_rejects_student_contracts_hidden_in_descriptions(description: str) -> None:
    assert has_excluded_contract_signal("Chargé de communication", "CDI, CDD", description)


@pytest.mark.parametrize(
    "description",
    [
        "Vous encadrez un stagiaire et une alternante.",
        "Vous travaillez avec notre alternante graphisme et communication.",
        "Une première expérience en alternance est appréciée.",
        "Expérience en pilotage de campagnes digitales, y compris dans le cadre d'une alternance.",
        "Au moins deux ans d'expérience, alternance comprise.",
        "Vous managez un assistant presse en stage et un chargé des réseaux sociaux.",
        "Poste CDD avec possibilité de mise en stage dans la fonction publique.",
        "Our hiring process: Stage 1: Applied, Stage 2: Peer Interview.",
        "Nous recherchons un responsable pour accompagner un stagiaire.",
    ],
)
def test_audit_preserves_supervision_experience_and_non_student_stage_contexts(description: str) -> None:
    assert not has_excluded_contract_signal("Communication interne", "CDI", description)


@pytest.mark.parametrize(
    ("title", "allowed"),
    [
        ("Ingénieur en communications sémantiques 6G", False),
        ("Agent de communication des dossiers patients", False),
        ("Formateur en marketing digital", False),
        ("Développeur Marketing Automation", False),
        ("Chargé de communication interne", True),
        ("Chargé marketing dans un centre de formation", True),
        ("Community manager & communication cible étudiante", True),
        ("Responsable administratif", False),
    ],
)
def test_configured_title_exclusions_preserve_relevant_roles(title: str, allowed: bool) -> None:
    patterns = (r"\bcommunications semantiques\b", r"\bdossiers patients\b", r"^formateur\b", r"^developpeur\b")
    filters = TextFilter(
        include=("communication", "communications", "marketing"),
        exclude=tuple(RegexRule(regex=pattern) for pattern in patterns),
    )
    assert filters.allows(title) is allowed
