"""Identify the legal employment type from the offer and its own terms."""

from __future__ import annotations

import re
from collections.abc import Callable

from jobsfr.offers.text import normalize_match_text

_TYPES = {
    "cdic": "CDI de chantier",
    "cdi de chantier": "CDI de chantier",
    "cdi": "CDI",
    "contrat a duree indeterminee": "CDI",
    "cdd saisonnier": "CDD saisonnier",
    "cdd": "CDD",
    "contrat a duree determinee": "CDD",
    "interim": "Intérim",
    "freelance": "Indépendant",
    "independant": "Indépendant",
    "contractor": "Indépendant",
    "saisonnier": "Saisonnier",
    "emploi permanent": "Emploi permanent",
    "fonctionnaire": "Fonctionnaire",
    "emploi non permanent": "Emploi non permanent",
    "profession liberale": "Profession libérale",
    "profession commerciale": "Profession commerciale",
    "contrat de projet": "Contrat de projet",
}
_TYPES_RE = "|".join(sorted(_TYPES, key=len, reverse=True))
_TOKEN = rf"(?:{_TYPES_RE})"
_SCHEDULE = r"(?:temps (?:plein|partiel|complet)|full time|part time)"
_LABEL = re.compile(
    rf"\b(?:type d emploi|type de contra(?:t|ct)|type de recrutement|nature (?:du|de) contrat|statut d emploi|contrat"
    rf"|contract(?: type)?"
    rf"|conditions(?: du poste| de travail| d emploi)?|ce qu on offre|(?:detail|description|missions) du poste)"
    rf" (?:de travail |propose |poste (?:ouvert )?en )?(?:(?:{_SCHEDULE}|permanent) )?"
    rf"(?P<types>{_TOKEN}(?: (?:et |ou )?{_TOKEN})*)\b"
)
_ROLE = re.compile(
    rf"\b(?:"
    rf"(?:poste|embauche|mission) (?:est )?(?:a pourvoir |basee? a [\w ]{{1,44}}? )?en (?P<role>{_TOKEN})"
    rf"|(?P<start>cdi|cdd|interim) (?:a pourvoir|jusqu a|a partir de|"
    rf"(?:a )?temps (?:plein|partiel|complet)|base(?:e| e)? a|statut (?:cadre|employe)|"
    rf"de (?:remplacement|droit public)|(?:(?:de|d|a) )?\d{{1,2}}(?: mois| ans?|\s*h\d{{0,2}}| heures))"
    rf")\b"
)
_DECLARED_LIST = re.compile(rf"^(?P<types>{_TOKEN}(?: (?:et |ou )?{_TOKEN})*)\b")
_MENTIONS = re.compile(rf"\b{_TOKEN}\b")
_SCHEDULE_ONLY = {"fulltime", "parttime", "full time", "part time", "temps plein", "temps partiel", "temps complet"}
_GENERIC = {"contract", "contrat", "other", "autre", "nan", "none"}
_CONCATENATED_HEADER = re.compile(r"(?<=[a-zà-ÿ0-9])(?=(?:Contrat|Type d['’]emploi|Type de contrat)\b)")
_HIRING = re.compile(
    r"\b(?:recherch(?:e|ons)|recrut(?:e|ons)|(?:sommes|est) a la recherche)"
    r"\b(?: actuellement)?(?: pour (?:le compte de )?(?:l un de |un de )?(?:notre|nos|son|ses) clients?\b"
    r".{0,100}?)? (?:d un|un une|un|une|notre|son sa)\b"
)
_HIRING_CONTRACT = re.compile(rf"\ben (?:{_SCHEDULE} en )?(?P<type>{_TOKEN})\b")
_FUTURE_CONTRACT = re.compile(r"\b(?:possible|possibilite|perspective|evolution|debouche|suivi|suivie)\b")
_SENTENCE_END = re.compile(r"[!?;\n]|\.(?=\s|$)")
_CIVIC_SERVICE = re.compile(r"^(?:mission de |volontariat en )?service civique\b")
_SHORT_CONTRACT_LABEL = re.compile(r"\btype\s*:\s*(?:\*+\s*)?(cdi|cdd|intérim|freelance)\b", re.IGNORECASE)


def _types(value: str) -> str:
    return ", ".join(dict.fromkeys(_TYPES[match[0]] for match in _MENTIONS.finditer(value)))


def _description_contract(description: str) -> tuple[int, str]:
    labels = {_types(match["types"]) for match in _LABEL.finditer(description)}
    if labels:
        return (4, labels.pop()) if len(labels) == 1 else (0, "")
    choices = set()
    for match in _ROLE.finditer(description):
        context = description[max(0, match.start() - 48) : match.start()]
        if re.search(r"\b(?:possible|possibilite|perspective|evolution|debouche|salaries?|encadrer)\b", context):
            continue
        choices.add(_TYPES[match["role"] or match["start"]])
    return (2, choices.pop()) if len(choices) == 1 else (0, "")


def _hiring_contract(description: str) -> str:
    choices = set()
    for sentence in _SENTENCE_END.split(description):
        normalized = normalize_match_text(sentence)
        for hiring in _HIRING.finditer(normalized):
            terms = normalized[hiring.end() : hiring.end() + 175]
            match = _HIRING_CONTRACT.search(terms)
            if not match or _FUTURE_CONTRACT.search(terms[: match.start()]):
                continue
            declared = _types(terms)
            if declared == _TYPES[match["type"]]:
                choices.add(declared)
    return choices.pop() if len(choices) == 1 else ""


def contract_evidence(get: Callable[[str], object]) -> tuple[int, str]:
    """Return a comparable evidence rank and a display label for this source copy."""
    if is_civic_service(get("title")) or is_civic_service(get("employment_type")):
        return 5, "Service civique"
    title = _types(normalize_match_text(get("title")))
    description = get("description")
    if isinstance(description, str):
        description = _CONCATENATED_HEADER.sub(" ", description)
        short_labels = {_types(normalize_match_text(match[1])) for match in _SHORT_CONTRACT_LABEL.finditer(description)}
        if len(short_labels) == 1:
            return 4, short_labels.pop()
    specific, detail = _description_contract(normalize_match_text(description))
    if specific == 4:
        return specific, detail
    if title:
        return 4, title
    if not detail and isinstance(description, str) and (hiring := _hiring_contract(description)):
        specific, detail = 2, hiring

    value = get("employment_type")
    if isinstance(value, str) and value:
        labels = [normalize_match_text(part) for part in value.split(",")]
        declared = ", ".join(
            dict.fromkeys(_types(match["types"]) for label in labels if (match := _DECLARED_LIST.match(label)))
        )
        # Older aggregator rows can contain both labels inferred from generic agency copy.
        source = get("source") or get("best_source")
        if declared and not (source in {"france_travail", "jobijoba"} and set(declared.split(", ")) == {"CDI", "CDD"}):
            return 3, declared
        if detail:
            return specific, detail
        if "temporary" in labels:
            return 1, "Temporaire"
        unknown = [
            part.strip()
            for part, label in zip(value.split(","), labels, strict=True)
            if label and label not in _GENERIC | _SCHEDULE_ONLY and not _DECLARED_LIST.match(label)
        ]
        if unknown:
            return 1, ", ".join(dict.fromkeys(unknown))
    return specific, detail


def is_civic_service(value: object) -> bool:
    return bool(_CIVIC_SERVICE.match(normalize_match_text(value)))


def infer_offer_contract(title: object, description: object) -> str | None:
    """Find an explicit offer type when an upstream source has no contract metadata."""
    return contract_evidence({"title": title, "description": description}.get)[1] or None
