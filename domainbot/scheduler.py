from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domainbot.config import Settings
from domainbot.dynadot.schemas import AvailabilityResult
from domainbot.models import Domain
from domainbot.services.availability import AvailabilityService
from domainbot.services.notifications import NotificationService
from domainbot.services.registration import RegistrationService
from domainbot.state_machine import ACTIVE_MONITOR_STATUSES, DomainStatus, ScheduleType, SKIP_STATUSES, apply_transition
from domainbot.utils.time import ensure_aware_utc, next_daily_window_after, parse_local_time, utc_now


logger = logging.getLogger(__name__)


class DomainScheduler:
    def __init__(
        self,
        *,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        availability: AvailabilityService,
        registration: RegistrationService,
        notifications: NotificationService,
    ):
        self.settings = settings
        self.session_factory = session_factory
        self.availability = availability
        self.registration = registration
        self.notifications = notifications
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def recover(self, now: datetime | None = None) -> None:
        current = ensure_aware_utc(now or utc_now())
        pending_confirmations: list[int] = []
        async with self.session_factory() as session:
            rows = await session.execute(select(Domain).where(Domain.enabled.is_(True)).order_by(Domain.id))
            domains = list(rows.scalars())
            for domain in domains:
                status = DomainStatus(domain.status)
                if status in {DomainStatus.REGISTERED, DomainStatus.PAUSED, DomainStatus.DISABLED, DomainStatus.PRICE_EXCEEDED}:
                    continue
                if status == DomainStatus.REGISTRATION_PENDING_CONFIRMATION:
                    pending_confirmations.append(domain.id)
                    continue
                if status == DomainStatus.AVAILABLE and str(domain.last_error or "").startswith("dry-run"):
                    continue
                self._align_domain_schedule(domain, current)
            await session.commit()

        for domain_id in pending_confirmations:
            await self.registration.confirm_uncertain_registration(domain_id)

    async def run_forever(self) -> None:
        await self.recover()
        logger.info("domain scheduler started")
        while not self._stop.is_set():
            did_work = await self.run_once()
            if did_work:
                continue
            sleep_for = await self.seconds_until_next_due()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=sleep_for)
            except TimeoutError:
                pass
        logger.info("domain scheduler stopped")

    async def seconds_until_next_due(self, now: datetime | None = None) -> float:
        current = ensure_aware_utc(now or utc_now())
        async with self.session_factory() as session:
            rows = await session.execute(
                select(Domain.next_check_at)
                .where(Domain.enabled.is_(True), Domain.next_check_at.is_not(None))
                .order_by(Domain.next_check_at.asc())
                .limit(1)
            )
            value = rows.scalar_one_or_none()
        if value is None:
            return 1.0
        due_at = ensure_aware_utc(value)
        return max(0.0, min(60.0, (due_at - current).total_seconds()))

    async def run_once(self, now: datetime | None = None) -> bool:
        current = ensure_aware_utc(now or utc_now())
        await self._expire_due_windows(current)
        selection = await self._select_due_domain(current)
        if selection is None:
            return False
        domain_id, previous_next, status = selection
        if status == DomainStatus.AVAILABLE.value:
            await self._register_previously_available(domain_id)
            return True
        async with self.session_factory() as session:
            domain = await session.get(Domain, domain_id)
            if domain is None:
                return True
            if DomainStatus(domain.status) in SKIP_STATUSES:
                return True
            if current < ensure_aware_utc(domain.start_at):
                domain.next_check_at = domain.start_at
                await session.commit()
                return False
            if current >= ensure_aware_utc(domain.window_end):
                await self._expire_or_rollover(session, domain, current)
                await session.commit()
                return True
            if domain.status != DomainStatus.WATCHING.value:
                apply_transition(domain, DomainStatus.WATCHING)
                await session.commit()
                await self.notifications.watching(domain.domain)
            else:
                await session.commit()

        try:
            result = await self.availability.check_domain(domain_id)
        except Exception as exc:
            logger.warning("availability check failed domain_id=%s error=%s", domain_id, exc)
            await self._schedule_after_failure(domain_id, previous_next, str(exc), current)
            return True

        if result.available:
            await self._mark_available(domain_id, result)
            await self.registration.handle_available(domain_id, result)
            return True

        await self._schedule_next_check(domain_id, previous_next, current)
        return True

    async def _select_due_domain(self, now: datetime) -> tuple[int, datetime | None, str] | None:
        active_values = [status.value for status in ACTIVE_MONITOR_STATUSES]
        async with self.session_factory() as session:
            rows = await session.execute(
                select(Domain)
                .where(
                    Domain.enabled.is_(True),
                    Domain.status.in_(active_values),
                    Domain.next_check_at.is_not(None),
                    Domain.next_check_at <= now,
                )
                .order_by(Domain.next_check_at.asc(), Domain.id.asc())
                .limit(1)
            )
            domain = rows.scalar_one_or_none()
            if domain is None:
                return None
            return domain.id, domain.next_check_at, domain.status

    async def _expire_due_windows(self, now: datetime) -> None:
        async with self.session_factory() as session:
            rows = await session.execute(
                select(Domain).where(
                    Domain.enabled.is_(True),
                    Domain.status.in_([status.value for status in ACTIVE_MONITOR_STATUSES]),
                    Domain.window_end <= now,
                )
            )
            expired = list(rows.scalars())
            notifications: list[tuple[str, int]] = []
            for domain in expired:
                if DomainStatus(domain.status) == DomainStatus.AVAILABLE:
                    continue
                if await self._expire_or_rollover(session, domain, now):
                    notifications.append((domain.domain, domain.duration_seconds))
            await session.commit()
        for domain, duration in notifications:
            await self.notifications.timeout(domain, duration)

    async def _expire_or_rollover(self, session: AsyncSession, domain: Domain, now: datetime) -> bool:
        if domain.schedule_type == ScheduleType.DAILY.value and DomainStatus(domain.status) != DomainStatus.REGISTERED:
            next_start = next_daily_window_after(domain.start_at, domain.timezone, now=now)
            domain.start_at = next_start
            domain.window_end = next_start + timedelta(seconds=domain.duration_seconds)
            domain.next_check_at = next_start
            apply_transition(domain, DomainStatus.SCHEDULED, allowed_from_any={DomainStatus.WATCHING, DomainStatus.EXPIRED, DomainStatus.ERROR, DomainStatus.TAKEN})
            return False
        apply_transition(domain, DomainStatus.EXPIRED, allowed_from_any={DomainStatus.WATCHING, DomainStatus.SCHEDULED, DomainStatus.ERROR, DomainStatus.TAKEN})
        domain.next_check_at = None
        return True

    def _align_domain_schedule(self, domain: Domain, now: datetime) -> None:
        start = ensure_aware_utc(domain.start_at)
        end = ensure_aware_utc(domain.window_end)
        if domain.schedule_type == ScheduleType.DAILY.value and now >= end:
            next_start = next_daily_window_after(start, domain.timezone, now=now)
            domain.start_at = next_start
            domain.window_end = next_start + timedelta(seconds=domain.duration_seconds)
            domain.next_check_at = next_start
            apply_transition(domain, DomainStatus.SCHEDULED, allowed_from_any={DomainStatus.EXPIRED, DomainStatus.ERROR, DomainStatus.TAKEN, DomainStatus.WATCHING})
            return
        if now < start:
            domain.next_check_at = start
            if domain.status != DomainStatus.SCHEDULED.value:
                apply_transition(domain, DomainStatus.SCHEDULED, allowed_from_any={DomainStatus.PENDING, DomainStatus.ERROR, DomainStatus.TAKEN})
            return
        if start <= now < end:
            if domain.next_check_at is None or ensure_aware_utc(domain.next_check_at) < now:
                domain.next_check_at = now
            if domain.status != DomainStatus.WATCHING.value:
                apply_transition(domain, DomainStatus.WATCHING, allowed_from_any={DomainStatus.PENDING, DomainStatus.SCHEDULED, DomainStatus.ERROR, DomainStatus.TAKEN, DomainStatus.AVAILABLE})
            return
        if domain.schedule_type == ScheduleType.ONCE.value:
            apply_transition(domain, DomainStatus.EXPIRED, allowed_from_any={DomainStatus.PENDING, DomainStatus.SCHEDULED, DomainStatus.WATCHING, DomainStatus.ERROR, DomainStatus.TAKEN})
            domain.next_check_at = None

    async def _schedule_next_check(self, domain_id: int, previous_next: datetime | None, completed_at: datetime | None = None) -> None:
        async with self.session_factory() as session:
            domain = await session.get(Domain, domain_id)
            if domain is None or DomainStatus(domain.status) in SKIP_STATUSES:
                return
            now = ensure_aware_utc(completed_at or utc_now())
            effective = float(domain.effective_interval_seconds)
            planned = ensure_aware_utc(previous_next or now) + timedelta(seconds=effective)
            safe = now + timedelta(seconds=self.settings.dynadot_min_safe_interval_seconds)
            next_check = max(planned, safe)
            window_end = ensure_aware_utc(domain.window_end)
            domain.next_check_at = min(next_check, window_end)
            domain.status = DomainStatus.WATCHING.value
            await session.commit()

    async def _schedule_after_failure(
        self,
        domain_id: int,
        previous_next: datetime | None,
        message: str,
        completed_at: datetime | None = None,
    ) -> None:
        async with self.session_factory() as session:
            domain = await session.get(Domain, domain_id)
            if domain is None or DomainStatus(domain.status) in SKIP_STATUSES:
                return
            now = ensure_aware_utc(completed_at or utc_now())
            effective = float(domain.effective_interval_seconds)
            planned = ensure_aware_utc(previous_next or now) + timedelta(seconds=effective)
            next_check = max(planned, now + timedelta(seconds=self.settings.dynadot_min_safe_interval_seconds))
            domain.next_check_at = min(next_check, ensure_aware_utc(domain.window_end))
            domain.last_error = message
            domain.status = DomainStatus.WATCHING.value
            await session.commit()

    async def _mark_available(self, domain_id: int, result: AvailabilityResult) -> None:
        async with self.session_factory() as session:
            domain = await session.get(Domain, domain_id)
            if domain is None:
                return
            if DomainStatus(domain.status) == DomainStatus.WATCHING:
                apply_transition(domain, DomainStatus.AVAILABLE)
            domain.next_check_at = None
            if result.price is not None:
                domain.last_observed_price = Decimal(result.price)
                domain.last_observed_currency = result.currency
            await session.commit()
            await self.notifications.available(domain.domain)

    async def _register_previously_available(self, domain_id: int) -> None:
        async with self.session_factory() as session:
            domain = await session.get(Domain, domain_id)
            if domain is None:
                return
            availability = AvailabilityResult(
                http_status=None,
                api_code=None,
                api_message="previous available state",
                response_time_ms=None,
                domain=domain.domain,
                available=True,
                premium=False,
                price=domain.last_observed_price,
                currency=domain.last_observed_currency or self.settings.dynadot_currency,
            )
        await self.registration.handle_available(domain_id, availability)


def effective_interval(requested_seconds: int, settings: Settings) -> float:
    return max(float(requested_seconds), settings.dynadot_min_safe_interval_seconds)


def daily_start_time_from_string(value: str) -> str:
    return parse_local_time(value).isoformat()
