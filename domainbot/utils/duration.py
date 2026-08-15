from __future__ import annotations

import re


_DURATION_RE = re.compile(r"^\s*(?P<num>\d+)\s*(?P<unit>s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hrs|hour|hours)?\s*$", re.I)


def parse_duration_seconds(value: str | int | None, default: int | None = None) -> int:
    if value is None:
        if default is None:
            raise ValueError("duration is required")
        return default
    if isinstance(value, int):
        if value <= 0:
            raise ValueError("duration must be positive")
        return value
    match = _DURATION_RE.match(value)
    if not match:
        raise ValueError(f"invalid duration: {value}")
    amount = int(match.group("num"))
    unit = (match.group("unit") or "s").lower()
    if amount <= 0:
        raise ValueError("duration must be positive")
    if unit.startswith("h"):
        return amount * 3600
    if unit.startswith("m"):
        return amount * 60
    return amount

