from __future__ import annotations

from domainbot.utils.domain import normalize_domain


MIN_REGISTRATION_YEARS_BY_TLD = {
    "ai": 2,
}


def minimum_registration_years(domain: str) -> int:
    normalized = normalize_domain(domain)
    tld = normalized.rsplit(".", 1)[1]
    return MIN_REGISTRATION_YEARS_BY_TLD.get(tld, 1)


def effective_registration_years(domain: str, requested_years: int) -> int:
    if requested_years <= 0:
        raise ValueError("registration years must be positive")
    return max(requested_years, minimum_registration_years(domain))

