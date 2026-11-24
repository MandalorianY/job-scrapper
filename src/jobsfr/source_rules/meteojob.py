from __future__ import annotations

import re

from jobsfr.offers.text import clean_text

GENERIC_TITLE_RE = re.compile(r"^emploi\b.+\bà\b", re.IGNORECASE)


def is_placeholder_offer(
    title: object | None,
    company: object | None,
    date_posted: object | None,
    *,
    has_jobposting: bool,
) -> bool:
    date_text = clean_text(date_posted)
    missing_date = not date_text or date_text.casefold() in {"nat", "nan", "none", "<na>"}
    generic_title = bool(GENERIC_TITLE_RE.search(clean_text(title) or ""))
    generic_company = (clean_text(company) or "").casefold() == "meteojob"
    return not has_jobposting and missing_date and (generic_title or generic_company)
