from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domainbot.config import Settings
from domainbot.dynadot.client import DynadotClient
from domainbot.dynadot.exceptions import DynadotNetworkError
from domainbot.dynadot.queue import DynadotAPIQueue
from domainbot.dynadot.schemas import APIPriority, AvailabilityResult
from domainbot.models import AvailabilityCheck, Domain
from domainbot.utils.time import utc_now


logger = logging.getLogger(__name__)


class AvailabilityService:
    def __init__(
        self,
        *,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        client: DynadotClient,
        api_queue: DynadotAPIQueue,
        search_retry_delays: list[float] | None = None,
    ):
        self.settings = settings
        self.session_factory = session_factory
        self.client = client
        self.api_queue = api_queue
        self.search_retry_delays = [1, 2, 5] if search_retry_delays is None else search_retry_delays

    async def check_domain(self, domain_id: int) -> AvailabilityResult:
        async with self.session_factory() as session:
            domain = await session.get(Domain, domain_id)
            if domain is None:
                raise ValueError(f"domain id not found: {domain_id}")
            domain_name = domain.domain

        last_error: Exception | None = None
        for attempt, delay in enumerate([0, *self.search_retry_delays], start=1):
            if delay:
                await asyncio.sleep(delay)
            try:
                result = await self.api_queue.call(
                    APIPriority.SEARCH,
                    f"SEARCH {domain_name}",
                    lambda: self.client.search_domain(domain_name),
                )
                await self._record_result(domain_id, result, checked_at=utc_now())
                logger.info(
                    "DOMAIN=%s ACTION=SEARCH STATUS=%s HTTP_STATUS=%s API_CODE=%s DURATION=%sms RESULT=%s",
                    domain_name,
                    "AVAILABLE" if result.available else "UNAVAILABLE",
                    result.http_status,
                    result.api_code,
                    result.response_time_ms,
                    "available" if result.available else "unavailable",
                )
                return result
            except DynadotNetworkError as exc:
                last_error = exc
                logger.warning("search network failure domain=%s attempt=%s error=%s", domain_name, attempt, exc)
        assert last_error is not None
        await self._record_failure(domain_id, str(last_error))
        raise last_error

    async def _record_result(self, domain_id: int, result: AvailabilityResult, checked_at: datetime) -> None:
        async with self.session_factory() as session:
            domain = await session.get(Domain, domain_id)
            if domain is None:
                return
            domain.attempt_count += 1
            domain.last_check_at = checked_at
            domain.last_error = None
            if result.price is not None:
                domain.last_observed_price = result.price
                domain.last_observed_currency = result.currency
            if result.available:
                domain.last_available_at = checked_at
            session.add(
                AvailabilityCheck(
                    domain_id=domain_id,
                    checked_at=checked_at,
                    available=result.available,
                    http_status=result.http_status,
                    api_response_code=result.api_code,
                    response_time_ms=result.response_time_ms,
                )
            )
            await session.flush()
            await self._prune_history(session, domain_id)
            await session.commit()

    async def _record_failure(self, domain_id: int, message: str) -> None:
        async with self.session_factory() as session:
            domain = await session.get(Domain, domain_id)
            if domain is not None:
                domain.last_error = message
            await session.commit()

    async def _prune_history(self, session: AsyncSession, domain_id: int) -> None:
        limit = self.settings.max_check_history_per_domain
        if limit <= 0:
            await session.execute(delete(AvailabilityCheck).where(AvailabilityCheck.domain_id == domain_id))
            return
        keep_ids = (
            select(AvailabilityCheck.id)
            .where(AvailabilityCheck.domain_id == domain_id)
            .order_by(AvailabilityCheck.checked_at.desc(), AvailabilityCheck.id.desc())
            .limit(limit)
        )
        await session.execute(
            delete(AvailabilityCheck).where(
                AvailabilityCheck.domain_id == domain_id,
                AvailabilityCheck.id.not_in(keep_ids),
            )
        )
