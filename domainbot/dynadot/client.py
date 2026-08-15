from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Iterable
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from domainbot.config import Settings
from domainbot.dynadot.exceptions import DynadotAPIError, DynadotNetworkError, DynadotTimeoutError
from domainbot.dynadot.schemas import AvailabilityResult, ConfirmationResult, RegistrationResult
from domainbot.utils.domain import normalize_domain


logger = logging.getLogger(__name__)
_DECIMAL_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _walk_dicts(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_dicts(item)


def _first_value(data: Any, names: set[str]) -> Any:
    lowered = {name.lower() for name in names}
    for item in _walk_dicts(data):
        for key, value in item.items():
            if key.lower() in lowered:
                return value
    return None


def _first_decimal(data: Any, names: set[str]) -> Decimal | None:
    value = _first_value(data, names)
    if value is None or value == "":
        return None
    if isinstance(value, str):
        match = _DECIMAL_RE.search(value.replace(",", ""))
        if match:
            value = match.group(0)
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y", "available", "success"}


def _redact_raw(data: Any, api_key: str) -> str:
    try:
        rendered = json.dumps(data, ensure_ascii=True, sort_keys=True)
    except TypeError:
        rendered = str(data)
    if api_key:
        rendered = rendered.replace(api_key, "[redacted]")
    return rendered


def _api_code(data: Any) -> str | None:
    value = _first_value(data, {"ResponseCode", "response_code", "code", "status_code"})
    return None if value is None else str(value)


def _api_message(data: Any) -> str | None:
    value = _first_value(data, {"ResponseText", "Message", "message", "Error", "error", "Status", "status"})
    return None if value is None else str(value)


def _order_id(data: Any) -> str | None:
    value = _first_value(data, {"OrderId", "OrderID", "order_id", "orderid", "OrderNumber", "order_number"})
    return None if value is None or value == "" else str(value)


def _currency(data: Any) -> str | None:
    value = _first_value(data, {"Currency", "currency"})
    return None if value is None or value == "" else str(value).upper()


def _contains_domain(data: Any, domain: str) -> bool:
    needle = normalize_domain(domain)
    for item in _walk_dicts(data):
        for value in item.values():
            if isinstance(value, str) and "." in value:
                try:
                    if normalize_domain(value) == needle:
                        return True
                except ValueError:
                    pass
            if isinstance(value, list) and any(isinstance(v, str) and v.lower().rstrip(".") == needle for v in value):
                return True
    text = json.dumps(data, ensure_ascii=True).lower()
    return f'"{needle}"' in text or needle in text


class DynadotClient:
    """Dynadot Legacy Domain API client.

    The queue enforces global serial execution; this class only builds and parses
    official API3 requests.
    """

    def __init__(self, settings: Settings, http_client: httpx.AsyncClient | None = None):
        self.settings = settings
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(timeout=settings.dynadot_timeout_seconds)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _request(self, params: dict[str, Any]) -> tuple[dict[str, Any], int, int, str]:
        if not self.settings.dynadot_api_key:
            raise DynadotAPIError("DYNADOT_API_KEY is required")

        safe_params = dict(params)
        request_params = {"key": self.settings.dynadot_api_key, **params}
        start = time.perf_counter()
        try:
            response = await self._client.get(self.settings.dynadot_effective_base_url, params=request_params)
            elapsed_ms = int((time.perf_counter() - start) * 1000)
        except httpx.TimeoutException as exc:
            raise DynadotTimeoutError(str(exc)) from exc
        except httpx.HTTPError as exc:
            raise DynadotNetworkError(str(exc)) from exc

        try:
            data = response.json()
        except ValueError as exc:
            raise DynadotAPIError(
                "Dynadot returned a non-JSON response",
                http_status=response.status_code,
            ) from exc

        redacted = _redact_raw(data, self.settings.dynadot_api_key)
        logger.debug("dynadot command=%s http=%s response=%s", safe_params.get("command"), response.status_code, redacted)
        return data, response.status_code, elapsed_ms, redacted

    async def search_domain(self, domain: str) -> AvailabilityResult:
        normalized = normalize_domain(domain)
        data, http_status, elapsed_ms, redacted = await self._request(
            {
                "command": "search",
                "domain0": normalized,
                "show_price": "1",
                "currency": self.settings.dynadot_currency,
            }
        )
        code = _api_code(data)
        message = _api_message(data)
        available_value = _first_value(data, {"Available", "available", "IsAvailable", "is_available"})
        status_value = _first_value(data, {"Status", "status"})
        available = _truthy(available_value) or str(status_value or "").strip().lower() == "available"
        premium_value = _first_value(data, {"Premium", "premium", "IsPremium", "is_premium"})
        price_text = str(_first_value(data, {"Price", "price", "RegisterPrice", "RegistrationPrice", "registration_price"}) or "")
        price = _first_decimal(data, {"Price", "price", "RegisterPrice", "RegistrationPrice", "registration_price"})
        currency = _currency(data) or self.settings.dynadot_currency
        return AvailabilityResult(
            http_status=http_status,
            api_code=code,
            api_message=message,
            response_time_ms=elapsed_ms,
            raw=data,
            raw_redacted=redacted,
            domain=normalized,
            available=available,
            premium=_truthy(premium_value) or ("premium" in price_text.lower() and "not premium" not in price_text.lower()),
            price=price,
            currency=currency,
        )

    async def register_domain(
        self,
        domain: str,
        *,
        years: int,
        premium: bool = False,
    ) -> RegistrationResult:
        normalized = normalize_domain(domain)
        params: dict[str, Any] = {
            "command": "register",
            "domain": normalized,
            "duration": str(years),
            "currency": self.settings.dynadot_currency,
        }
        if premium:
            params["premium"] = "1"
        data, http_status, elapsed_ms, redacted = await self._request(params)
        code = _api_code(data)
        message = _api_message(data)
        status_text = str(_first_value(data, {"Status", "status", "Result", "result"}) or "").lower()
        success = code == "0" and (
            "success" in status_text
            or "registered" in status_text
            or _first_value(data, {"Expiration", "ExpirationDate", "expiration_date"}) is not None
        )
        return RegistrationResult(
            http_status=http_status,
            api_code=code,
            api_message=message,
            response_time_ms=elapsed_ms,
            raw=data,
            raw_redacted=redacted,
            domain=normalized,
            success=success,
            order_id=_order_id(data),
            price=_first_decimal(data, {"Price", "price", "Total", "total", "Amount", "amount"}),
            currency=_currency(data) or self.settings.dynadot_currency,
            expiration_date=_first_value(data, {"Expiration", "ExpirationDate", "expiration_date"}),
        )

    async def confirm_registration(self, domain: str, order_id: str | None = None) -> ConfirmationResult:
        normalized = normalize_domain(domain)
        checks: list[dict[str, Any]] = []
        if order_id:
            checks.append({"command": "order_list", "search_by": "order_id", "order_id": order_id})
        checks.extend(
            [
                {"command": "domain_info", "domain": normalized},
                {"command": "list_domain"},
                {"command": "order_list", "search_by": "domain", "domain": normalized},
            ]
        )

        last: tuple[dict[str, Any], int, int, str] | None = None
        for params in checks:
            try:
                data, http_status, elapsed_ms, redacted = await self._request(params)
            except DynadotAPIError as exc:
                logger.debug("registration confirmation command failed: %s", exc)
                continue
            last = (data, http_status, elapsed_ms, redacted)
            code = _api_code(data)
            message = _api_message(data)
            found = _contains_domain(data, normalized)
            if found and (code in {None, "0"}):
                return ConfirmationResult(
                    http_status=http_status,
                    api_code=code,
                    api_message=message,
                    response_time_ms=elapsed_ms,
                    raw=data,
                    raw_redacted=redacted,
                    domain=normalized,
                    registered=True,
                    order_id=order_id or _order_id(data),
                    confirmation_status="registered",
                )

        if last is None:
            return ConfirmationResult(
                http_status=None,
                api_code=None,
                api_message="unable to query Dynadot registration status",
                response_time_ms=None,
                domain=normalized,
                registered=None,
                order_id=order_id,
                confirmation_status="unknown",
            )
        data, http_status, elapsed_ms, redacted = last
        return ConfirmationResult(
            http_status=http_status,
            api_code=_api_code(data),
            api_message=_api_message(data),
            response_time_ms=elapsed_ms,
            raw=data,
            raw_redacted=redacted,
            domain=normalized,
            registered=False,
            order_id=order_id or _order_id(data),
            confirmation_status="not_found",
        )

    async def test_api(self) -> AvailabilityResult:
        return await self.search_domain("example.com")
