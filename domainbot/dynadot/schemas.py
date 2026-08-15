from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import IntEnum
from typing import Any


class APIPriority(IntEnum):
    REGISTER = 0
    STATUS_CONFIRM = 1
    SEARCH = 10


@dataclass(slots=True)
class APIResult:
    http_status: int | None
    api_code: str | None
    api_message: str | None
    response_time_ms: int | None
    raw: dict[str, Any] = field(default_factory=dict)
    raw_redacted: str = ""


@dataclass(slots=True)
class AvailabilityResult(APIResult):
    domain: str = ""
    available: bool = False
    premium: bool = False
    price: Decimal | None = None
    currency: str | None = None


@dataclass(slots=True)
class RegistrationResult(APIResult):
    domain: str = ""
    success: bool = False
    order_id: str | None = None
    price: Decimal | None = None
    currency: str | None = None
    expiration_date: str | None = None


@dataclass(slots=True)
class ConfirmationResult(APIResult):
    domain: str = ""
    registered: bool | None = None
    order_id: str | None = None
    confirmation_status: str | None = None

