from __future__ import annotations


def normalize_domain(value: str) -> str:
    domain = value.strip().lower().rstrip(".")
    if not domain:
        raise ValueError("domain is required")
    if "/" in domain or " " in domain or "@" in domain:
        raise ValueError(f"invalid domain: {value}")
    labels = domain.split(".")
    if len(labels) < 2 or any(not label for label in labels):
        raise ValueError(f"invalid domain: {value}")
    ascii_domain = domain.encode("idna").decode("ascii")
    labels = ascii_domain.split(".")
    if any(label.startswith("-") or label.endswith("-") for label in labels):
        raise ValueError(f"invalid domain: {value}")
    return ascii_domain


def domain_tld(domain: str) -> str:
    normalized = normalize_domain(domain)
    return normalized.rsplit(".", 1)[1]

