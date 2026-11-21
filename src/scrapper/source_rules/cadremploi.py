from urllib.parse import parse_qs, urlsplit


def offer_id(url: str) -> str | None:
    try:
        query = parse_qs(urlsplit(url).query)
    except ValueError:
        return None
    for parameter in ("offreId", "annonceId"):
        if identifier := query.get(parameter, [""])[0].strip():
            return identifier
    return None
