from __future__ import annotations

from enum import StrEnum
from typing import Iterable


class DomainStatus(StrEnum):
    PENDING = "pending"
    SCHEDULED = "scheduled"
    WATCHING = "watching"
    AVAILABLE = "available"
    REGISTERING = "registering"
    REGISTRATION_PENDING_CONFIRMATION = "registration_pending_confirmation"
    REGISTERED = "registered"
    TAKEN = "taken"
    PAUSED = "paused"
    EXPIRED = "expired"
    PRICE_EXCEEDED = "price_exceeded"
    ERROR = "error"
    DISABLED = "disabled"


class ScheduleType(StrEnum):
    ONCE = "once"
    DAILY = "daily"


TERMINAL_STATUSES = {
    DomainStatus.REGISTERED,
    DomainStatus.DISABLED,
    DomainStatus.PRICE_EXCEEDED,
}

SKIP_STATUSES = {
    DomainStatus.REGISTERED,
    DomainStatus.PAUSED,
    DomainStatus.DISABLED,
    DomainStatus.EXPIRED,
    DomainStatus.PRICE_EXCEEDED,
}

ACTIVE_MONITOR_STATUSES = {
    DomainStatus.PENDING,
    DomainStatus.SCHEDULED,
    DomainStatus.WATCHING,
    DomainStatus.AVAILABLE,
    DomainStatus.ERROR,
}

ALLOWED_TRANSITIONS: dict[DomainStatus, set[DomainStatus]] = {
    DomainStatus.PENDING: {
        DomainStatus.SCHEDULED,
        DomainStatus.WATCHING,
        DomainStatus.PAUSED,
        DomainStatus.DISABLED,
        DomainStatus.ERROR,
    },
    DomainStatus.SCHEDULED: {
        DomainStatus.WATCHING,
        DomainStatus.PAUSED,
        DomainStatus.EXPIRED,
        DomainStatus.DISABLED,
        DomainStatus.ERROR,
    },
    DomainStatus.WATCHING: {
        DomainStatus.AVAILABLE,
        DomainStatus.PRICE_EXCEEDED,
        DomainStatus.PAUSED,
        DomainStatus.EXPIRED,
        DomainStatus.TAKEN,
        DomainStatus.DISABLED,
        DomainStatus.ERROR,
        DomainStatus.WATCHING,
    },
    DomainStatus.AVAILABLE: {
        DomainStatus.REGISTERING,
        DomainStatus.PAUSED,
        DomainStatus.EXPIRED,
        DomainStatus.PRICE_EXCEEDED,
        DomainStatus.ERROR,
    },
    DomainStatus.REGISTERING: {
        DomainStatus.REGISTERED,
        DomainStatus.REGISTRATION_PENDING_CONFIRMATION,
        DomainStatus.WATCHING,
        DomainStatus.ERROR,
        DomainStatus.PRICE_EXCEEDED,
    },
    DomainStatus.REGISTRATION_PENDING_CONFIRMATION: {
        DomainStatus.REGISTERED,
        DomainStatus.WATCHING,
        DomainStatus.ERROR,
        DomainStatus.PAUSED,
    },
    DomainStatus.TAKEN: {
        DomainStatus.WATCHING,
        DomainStatus.SCHEDULED,
        DomainStatus.PAUSED,
        DomainStatus.EXPIRED,
        DomainStatus.DISABLED,
    },
    DomainStatus.PAUSED: {
        DomainStatus.SCHEDULED,
        DomainStatus.WATCHING,
        DomainStatus.DISABLED,
    },
    DomainStatus.EXPIRED: {
        DomainStatus.SCHEDULED,
        DomainStatus.DISABLED,
    },
    DomainStatus.PRICE_EXCEEDED: {
        DomainStatus.PAUSED,
        DomainStatus.DISABLED,
    },
    DomainStatus.ERROR: {
        DomainStatus.WATCHING,
        DomainStatus.SCHEDULED,
        DomainStatus.PAUSED,
        DomainStatus.DISABLED,
        DomainStatus.REGISTRATION_PENDING_CONFIRMATION,
    },
    DomainStatus.REGISTERED: set(),
    DomainStatus.DISABLED: set(),
}


class InvalidTransition(ValueError):
    pass


def coerce_status(value: str | DomainStatus) -> DomainStatus:
    if isinstance(value, DomainStatus):
        return value
    return DomainStatus(value)


def validate_transition(current: str | DomainStatus, target: str | DomainStatus) -> None:
    current_status = coerce_status(current)
    target_status = coerce_status(target)
    if current_status == target_status:
        return
    allowed = ALLOWED_TRANSITIONS.get(current_status, set())
    if target_status not in allowed:
        raise InvalidTransition(f"invalid domain status transition: {current_status} -> {target_status}")


def apply_transition(obj: object, target: str | DomainStatus, allowed_from_any: Iterable[DomainStatus] | None = None) -> None:
    current = coerce_status(getattr(obj, "status"))
    target_status = coerce_status(target)
    if allowed_from_any is not None and current in allowed_from_any:
        setattr(obj, "status", target_status.value)
        return
    validate_transition(current, target_status)
    setattr(obj, "status", target_status.value)
