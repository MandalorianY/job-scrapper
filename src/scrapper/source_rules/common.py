from __future__ import annotations

import re

from scrapper.text import clean_text


OFFER_REFERENCE_PREFIX_RE = re.compile(r"^(?:Offre\s+n[°o]\s*)?(?:[A-Z0-9]{6,8}|\d{7})\s+")
OFFER_LABEL_PREFIX_RE = re.compile(r"^offre\s+d[’']emploi\s*-\s*", re.IGNORECASE)
TRAILING_CATEGORY_RE = re.compile(
    r"^(?P<title>.+?\b(?:H/F|F/H|H/F/NB|F/H/NB|\(H/F\)|\(F/H\)))\s+-\s+[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ' /-]{2,80}\s+\((?:H/F|F/H)\)$"
)


def normalize_offer_title(value: object | None) -> str | None:
    text = clean_text(value)
    if not text:
        return None
    text = OFFER_REFERENCE_PREFIX_RE.sub("", text)
    text = OFFER_LABEL_PREFIX_RE.sub("", text)
    text = OFFER_REFERENCE_PREFIX_RE.sub("", text)
    match = TRAILING_CATEGORY_RE.match(text)
    if match:
        text = match.group("title")
    text = text.strip(" -|")
    return clean_text(text)
