from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domainbot.config import Settings
from domainbot.dynadot.client import DynadotClient
from domainbot.dynadot.exceptions import DynadotNetworkError
from domainbot.dynadot.queue import DynadotAPIQueue
from domainbot.dynadot.schemas import APIPriority, AvailabilityResult, ConfirmationResult, RegistrationResult
from domainbot.models import Domain, RegistrationAttempt
from domainbot.services.notifications import NotificationService
from domainbot.state_machine import DomainStatus, apply_transition
from domainbot.utils.time import utc_now


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RegistrationLock:
    domain_id: int
    domain: str
    years: int
    attempt_id: int
    request_id: str
    premium: bool


class RegistrationService:
    def __init__(
        self,
        *,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        client: DynadotClient,
        api_queue: DynadotAPIQueue,
        notifications: NotificationService,
        dry_run: bool = False,
    ):
        self.settings = settings
        self.session_factory = session_factory
        self.client = client
        self.api_queue = api_queue
        self.notifications = notifications
        self.dry_run = dry_run

    async def handle_available(self, domain_id: int, availability: AvailabilityResult) -> None:
        if await self._price_exceeded(domain_id, availability):
            return
        if self.dry_run:
            await self._mark_simulated(domain_id, availability)
            return

        lock = await self._begin_registration(domain_id, availability)
        if lock is None:
            return
        await self.notifications.registering(lock.domain)
        try:
            result = await self.api_queue.call(
                APIPriority.REGISTER,
                f"REGISTER {lock.domain}",
                lambda: self.client.register_domain(lock.domain, years=lock.years, premium=lock.premium),
            )
        except DynadotNetworkError as exc:
            logger.warning("register uncertain domain=%s error=%s", lock.domain, exc)
            await self._mark_pending_confirmation(lock, str(exc))
            await self.confirm_uncertain_registration(lock.domain_id, lock.attempt_id, order_id=None)
            return

        await self._finish_registration(lock, result)

    async def confirm_uncertain_registration(
        self,
        domain_id: int,
        attempt_id: int | None = None,
        order_id: str | None = None,
    ) -> ConfirmationResult | None:
        async with self.session_factory() as session:
            domain = await session.get(Domain, domain_id)
            if domain is None:
                return None
            domain_name = domain.domain

        try:
            result = await self.api_queue.call(
                APIPriority.STATUS_CONFIRM,
                f"STATUS_CONFIRM {domain_name}",
                lambda: self.client.confirm_registration(domain_name, order_id=order_id),
            )
        except DynadotNetworkError as exc:
            await self._finish_confirmation(domain_id, attempt_id, None, f"confirmation network error: {exc}")
            return None
        await self._finish_confirmation(domain_id, attempt_id, result, None)
        return result

    async def _price_exceeded(self, domain_id: int, availability: AvailabilityResult) -> bool:
        if availability.price is None:
            return False
        async with self.session_factory() as session:
            domain = await session.get(Domain, domain_id)
            if domain is None or domain.max_price is None:
                return False
            actual = Decimal(availability.price)
            limit = Decimal(domain.max_price)
            if actual <= limit:
                return False
            apply_transition(domain, DomainStatus.PRICE_EXCEEDED)
            domain.next_check_at = None
            domain.last_error = f"price exceeded: actual={actual} {availability.currency}, limit={limit}"
            await session.commit()
            await self.notifications.price_exceeded(
                domain.domain,
                actual=f"{actual} {availability.currency or self.settings.dynadot_currency}",
                limit=f"{limit} {availability.currency or self.settings.dynadot_currency}",
            )
            logger.info("DOMAIN=%s ACTION=PRICE_EXCEEDED STATUS=PRICE_EXCEEDED RESULT=actual:%s limit:%s", domain.domain, actual, limit)
            return True

    async def _mark_simulated(self, domain_id: int, availability: AvailabilityResult) -> None:
        async with self.session_factory() as session:
            domain = await session.get(Domain, domain_id)
            if domain is None:
                return
            domain.status = DomainStatus.AVAILABLE.value
            domain.next_check_at = None
            domain.last_error = "dry-run: simulated register only"
            session.add(
                RegistrationAttempt(
                    domain_id=domain_id,
                    attempted_at=utc_now(),
                    request_id=f"dry-run-{uuid4().hex}",
                    http_status=availability.http_status,
                    api_response_code=availability.api_code,
                    api_response_message="SIMULATED_REGISTER",
                    price=availability.price,
                    currency=availability.currency,
                    success=False,
                    confirmation_status="dry_run",
                    raw_response_redacted=availability.raw_redacted,
                )
            )
            await session.commit()
            logger.info("DOMAIN=%s ACTION=SIMULATED_REGISTER STATUS=AVAILABLE RESULT=dry-run", domain.domain)

    async def _begin_registration(self, domain_id: int, availability: AvailabilityResult) -> RegistrationLock | None:
        request_id = uuid4().hex
        async with self.session_factory() as session:
            async with session.begin():
                domain = await session.get(Domain, domain_id)
                if domain is None:
                    return None
                current = DomainStatus(domain.status)
                if current in {
                    DomainStatus.REGISTERED,
                    DomainStatus.REGISTERING,
                    DomainStatus.REGISTRATION_PENDING_CONFIRMATION,
                    DomainStatus.PRICE_EXCEEDED,
                    DomainStatus.PAUSED,
                    DomainStatus.DISABLED,
                }:
                    return None
                if domain.registration_attempt_count >= self.settings.register_max_attempts:
                    apply_transition(domain, DomainStatus.ERROR, allowed_from_any={current})
                    domain.last_error = "maximum registration attempts reached"
                    return None
                if current == DomainStatus.WATCHING:
                    apply_transition(domain, DomainStatus.AVAILABLE)
                apply_transition(domain, DomainStatus.REGISTERING)
                domain.registration_attempt_count += 1
                attempt = RegistrationAttempt(
                    domain_id=domain.id,
                    attempted_at=utc_now(),
                    request_id=request_id,
                    price=availability.price,
                    currency=availability.currency,
                    success=False,
                    confirmation_status="started",
                    raw_response_redacted=availability.raw_redacted,
                )
                session.add(attempt)
                await session.flush()
                return RegistrationLock(
                    domain_id=domain.id,
                    domain=domain.domain,
                    years=domain.registration_years,
                    attempt_id=attempt.id,
                    request_id=request_id,
                    premium=availability.premium,
                )

    async def _finish_registration(self, lock: RegistrationLock, result: RegistrationResult) -> None:
        async with self.session_factory() as session:
            domain = await session.get(Domain, lock.domain_id)
            attempt = await session.get(RegistrationAttempt, lock.attempt_id)
            if domain is None or attempt is None:
                return
            attempt.http_status = result.http_status
            attempt.api_response_code = result.api_code
            attempt.api_response_message = result.api_message
            attempt.dynadot_order_id = result.order_id
            attempt.price = result.price or attempt.price
            attempt.currency = result.currency or attempt.currency
            attempt.raw_response_redacted = result.raw_redacted
            if result.success:
                attempt.success = True
                attempt.confirmation_status = "registered"
                apply_transition(domain, DomainStatus.REGISTERED)
                domain.registered_at = utc_now()
                domain.dynadot_order_id = result.order_id
                domain.next_check_at = None
                domain.last_error = None
                await session.commit()
                price_text = f"{result.price} {result.currency}" if result.price is not None else None
                await self.notifications.success(domain.domain, price=price_text, order_id=result.order_id)
                logger.info("DOMAIN=%s ACTION=REGISTER_SUCCESS STATUS=REGISTERED ORDER_ID=%s", domain.domain, result.order_id)
                return

            attempt.success = False
            attempt.confirmation_status = "failed"
            message = result.api_message or "register failed"
            safe_to_watch = "not available" in message.lower() or "already registered" in message.lower()
            if safe_to_watch:
                apply_transition(domain, DomainStatus.WATCHING)
            else:
                apply_transition(domain, DomainStatus.ERROR)
                domain.next_check_at = None
            domain.last_error = message
            await session.commit()
            await self.notifications.failed(domain.domain, message)
            logger.info("DOMAIN=%s ACTION=REGISTER_FAILED STATUS=%s API_CODE=%s RESULT=%s", domain.domain, domain.status, result.api_code, message)

    async def _mark_pending_confirmation(self, lock: RegistrationLock, message: str) -> None:
        async with self.session_factory() as session:
            domain = await session.get(Domain, lock.domain_id)
            attempt = await session.get(RegistrationAttempt, lock.attempt_id)
            if domain is None or attempt is None:
                return
            apply_transition(domain, DomainStatus.REGISTRATION_PENDING_CONFIRMATION)
            domain.last_error = message
            attempt.api_response_message = message
            attempt.confirmation_status = "pending"
            await session.commit()

    async def _finish_confirmation(
        self,
        domain_id: int,
        attempt_id: int | None,
        result: ConfirmationResult | None,
        error_message: str | None,
    ) -> None:
        async with self.session_factory() as session:
            domain = await session.get(Domain, domain_id)
            attempt = await session.get(RegistrationAttempt, attempt_id) if attempt_id is not None else None
            if domain is None:
                return
            if attempt is not None:
                attempt.confirmation_status = result.confirmation_status if result else "unknown"
                attempt.api_response_message = error_message or (result.api_message if result else None)
                attempt.http_status = result.http_status if result else None
                attempt.api_response_code = result.api_code if result else None
                attempt.raw_response_redacted = result.raw_redacted if result else attempt.raw_response_redacted
                attempt.dynadot_order_id = result.order_id if result else attempt.dynadot_order_id
            if result and result.registered is True:
                apply_transition(domain, DomainStatus.REGISTERED)
                domain.registered_at = utc_now()
                domain.dynadot_order_id = result.order_id
                domain.next_check_at = None
                domain.last_error = None
                if attempt is not None:
                    attempt.success = True
                await session.commit()
                await self.notifications.success(domain.domain, order_id=result.order_id)
                return
            if result and result.registered is False:
                apply_transition(domain, DomainStatus.WATCHING)
                domain.last_error = "registration not found after confirmation"
                await session.commit()
                await self.notifications.failed(domain.domain, "registration not found after confirmation")
                return
            domain.status = DomainStatus.REGISTRATION_PENDING_CONFIRMATION.value
            domain.last_error = error_message or "registration status could not be confirmed"
            await session.commit()
            await self.notifications.failed(domain.domain, domain.last_error)
