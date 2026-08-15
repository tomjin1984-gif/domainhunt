from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
import httpx
from sqlalchemy import select

from domainbot.config import Settings
from domainbot.database import create_engine, create_session_factory, init_db
from domainbot.dynadot.client import DynadotClient
from domainbot.dynadot.exceptions import DynadotNetworkError, DynadotTimeoutError
from domainbot.dynadot.queue import DynadotAPIQueue
from domainbot.dynadot.schemas import APIPriority, AvailabilityResult, ConfirmationResult, RegistrationResult
from domainbot.models import Domain, RegistrationAttempt
from domainbot.scheduler import DomainScheduler, effective_interval
from domainbot.services.availability import AvailabilityService
from domainbot.services.notifications import NotificationService
from domainbot.services.registration import RegistrationService
from domainbot.state_machine import DomainStatus, ScheduleType, apply_transition
from domainbot.utils.domain import domain_tld, normalize_domain
from domainbot.utils.time import ensure_aware_utc


UTC = timezone.utc


class DummyNotifications(NotificationService):
    def __init__(self, settings: Settings):
        super().__init__(settings)
        self.messages: list[str] = []

    async def send(self, message: str) -> None:
        self.messages.append(message)


class FakeDynadotClient:
    def __init__(self):
        self.search_results: list[AvailabilityResult | Exception | bool] = []
        self.register_results: list[RegistrationResult | Exception | bool] = []
        self.confirm_results: list[ConfirmationResult | Exception | bool | None] = []
        self.calls: list[tuple[str, str]] = []

    async def search_domain(self, domain: str) -> AvailabilityResult:
        self.calls.append(("search", domain))
        value = self.search_results.pop(0)
        if isinstance(value, Exception):
            raise value
        if isinstance(value, AvailabilityResult):
            return value
        return availability(domain, available=bool(value))

    async def register_domain(self, domain: str, *, years: int, premium: bool = False) -> RegistrationResult:
        self.calls.append(("register", domain))
        value = self.register_results.pop(0)
        if isinstance(value, Exception):
            raise value
        if isinstance(value, RegistrationResult):
            return value
        return registration(domain, success=bool(value))

    async def confirm_registration(self, domain: str, order_id: str | None = None) -> ConfirmationResult:
        self.calls.append(("confirm", domain))
        value = self.confirm_results.pop(0)
        if isinstance(value, Exception):
            raise value
        if isinstance(value, ConfirmationResult):
            return value
        return confirmation(domain, registered=value)


def availability(domain: str, *, available: bool, price: Decimal | None = None, premium: bool = False) -> AvailabilityResult:
    return AvailabilityResult(
        http_status=200,
        api_code="0",
        api_message="success",
        response_time_ms=12,
        raw={},
        raw_redacted="{}",
        domain=domain,
        available=available,
        premium=premium,
        price=price,
        currency="USD",
    )


def registration(domain: str, *, success: bool, order_id: str | None = "order-1") -> RegistrationResult:
    return RegistrationResult(
        http_status=200,
        api_code="0" if success else "-1",
        api_message="success" if success else "not available",
        response_time_ms=20,
        raw={},
        raw_redacted="{}",
        domain=domain,
        success=success,
        order_id=order_id,
        price=Decimal("10.00"),
        currency="USD",
    )


def confirmation(domain: str, *, registered: bool | None) -> ConfirmationResult:
    return ConfirmationResult(
        http_status=200,
        api_code="0",
        api_message="ok",
        response_time_ms=10,
        raw={},
        raw_redacted="{}",
        domain=domain,
        registered=registered,
        order_id="confirmed-order",
        confirmation_status="registered" if registered else "not_found",
    )


@dataclass
class Runtime:
    settings: Settings
    client: FakeDynadotClient
    queue: DynadotAPIQueue
    notifications: DummyNotifications
    scheduler: DomainScheduler
    registration: RegistrationService
    session_factory: object
    engine: object

    async def close(self) -> None:
        await self.queue.close()
        await self.engine.dispose()


async def make_runtime(tmp_path: Path, *, dry_run: bool = False) -> Runtime:
    settings = Settings(
        dynadot_api_key="test-key",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'domainbot.db'}",
        log_file=tmp_path / "domainbot.log",
        dynadot_rate_limit_rpm=60000,
        max_check_history_per_domain=5,
        register_max_attempts=3,
    )
    engine = create_engine(settings)
    await init_db(engine=engine)
    session_factory = create_session_factory(engine)
    client = FakeDynadotClient()
    queue = DynadotAPIQueue(min_interval_seconds=0)
    notifications = DummyNotifications(settings)
    availability_service = AvailabilityService(
        settings=settings,
        session_factory=session_factory,
        client=client,  # type: ignore[arg-type]
        api_queue=queue,
        search_retry_delays=[],
    )
    registration_service = RegistrationService(
        settings=settings,
        session_factory=session_factory,
        client=client,  # type: ignore[arg-type]
        api_queue=queue,
        notifications=notifications,
        dry_run=dry_run,
    )
    scheduler = DomainScheduler(
        settings=settings,
        session_factory=session_factory,
        availability=availability_service,
        registration=registration_service,
        notifications=notifications,
    )
    return Runtime(settings, client, queue, notifications, scheduler, registration_service, session_factory, engine)


async def add_domain(
    rt: Runtime,
    domain: str,
    *,
    now: datetime,
    start_offset: int = 0,
    interval: int = 5,
    duration: int = 1200,
    status: DomainStatus = DomainStatus.WATCHING,
    schedule_type: ScheduleType = ScheduleType.ONCE,
    max_price: Decimal | None = None,
) -> int:
    start_at = now + timedelta(seconds=start_offset)
    async with rt.session_factory() as session:
        item = Domain(
            domain=normalize_domain(domain),
            tld=domain_tld(domain),
            status=status.value,
            enabled=True,
            schedule_type=schedule_type.value,
            start_at=start_at,
            window_end=start_at + timedelta(seconds=duration),
            duration_seconds=duration,
            interval_seconds=interval,
            effective_interval_seconds=effective_interval(interval, rt.settings),
            timezone="UTC",
            daily_start_time=start_at.time().replace(microsecond=0).isoformat() if schedule_type == ScheduleType.DAILY else None,
            next_check_at=start_at,
            registration_years=1,
            max_price=max_price,
        )
        session.add(item)
        await session.commit()
        return item.id


async def get_domain(rt: Runtime, domain_id: int) -> Domain:
    async with rt.session_factory() as session:
        item = await session.get(Domain, domain_id)
        assert item is not None
        return item


def test_domain_normalization() -> None:
    assert normalize_domain(" LocalFab.AI. ") == "localfab.ai"
    assert domain_tld("example.COM") == "com"
    with pytest.raises(ValueError):
        normalize_domain("bad domain")


@pytest.mark.asyncio
async def test_dynadot_search_price_string_is_parsed(tmp_path: Path) -> None:
    settings = Settings(
        dynadot_api_key="test-key",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'domainbot.db'}",
        log_file=tmp_path / "domainbot.log",
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["key"] == "test-key"
        assert request.url.params["command"] == "search"
        assert request.url.path == "/api3.json"
        return httpx.Response(
            200,
            json={
                "SearchResponse": {
                    "ResponseCode": "0",
                    "SearchResults": [
                        {
                            "DomainName": "premium.ai",
                            "Available": "yes",
                            "Price": "800.00 in USD and domain is premium",
                        }
                    ],
                }
            },
        )

    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport, base_url=settings.dynadot_effective_base_url)
    client = DynadotClient(settings, http_client=http_client)
    try:
        result = await client.search_domain("Premium.AI")
    finally:
        await client.aclose()
    assert result.available is True
    assert result.price == Decimal("800.00")
    assert result.premium is True


@pytest.mark.asyncio
async def test_queue_concurrency_is_always_one() -> None:
    queue = DynadotAPIQueue(min_interval_seconds=0)
    active = 0
    max_active = 0

    async def op(i: int) -> int:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return i

    try:
        results = await asyncio.gather(*(queue.call(APIPriority.SEARCH, f"op-{i}", lambda i=i: op(i)) for i in range(8)))
    finally:
        await queue.close()
    assert sorted(results) == list(range(8))
    assert max_active == 1
    assert queue.max_observed_active_requests == 1


@pytest.mark.asyncio
async def test_register_priority_happens_before_next_search(tmp_path: Path) -> None:
    rt = await make_runtime(tmp_path)
    now = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    try:
        first = await add_domain(rt, "a.ai", now=now)
        await add_domain(rt, "b.ai", now=now)
        rt.client.search_results = [availability("a.ai", available=True, price=Decimal("10.00"))]
        rt.client.register_results = [registration("a.ai", success=True)]

        await rt.scheduler.run_once(now)

        assert rt.client.calls == [("search", "a.ai"), ("register", "a.ai")]
        item = await get_domain(rt, first)
        assert item.status == DomainStatus.REGISTERED.value
        assert item.next_check_at is None
    finally:
        await rt.close()


@pytest.mark.asyncio
async def test_unavailable_schedules_five_second_interval_without_catchup(tmp_path: Path) -> None:
    rt = await make_runtime(tmp_path)
    now = datetime(2026, 1, 1, 0, 7, 0, tzinfo=UTC)
    try:
        domain_id = await add_domain(rt, "late.ai", now=now - timedelta(minutes=7), interval=5)
        async with rt.session_factory() as session:
            item = await session.get(Domain, domain_id)
            item.next_check_at = now - timedelta(minutes=6)
            await session.commit()

        await rt.scheduler.recover(now)
        recovered = await get_domain(rt, domain_id)
        assert ensure_aware_utc(recovered.next_check_at) == now

        rt.client.search_results = [availability("late.ai", available=False)]
        await rt.scheduler.run_once(now)

        item = await get_domain(rt, domain_id)
        assert item.status == DomainStatus.WATCHING.value
        assert ensure_aware_utc(item.next_check_at) == now + timedelta(seconds=5)
        assert rt.client.calls == [("search", "late.ai")]
    finally:
        await rt.close()


@pytest.mark.asyncio
async def test_once_window_expires_without_search(tmp_path: Path) -> None:
    rt = await make_runtime(tmp_path)
    now = datetime(2026, 1, 1, 0, 20, 0, tzinfo=UTC)
    try:
        domain_id = await add_domain(rt, "expired.ai", now=now - timedelta(minutes=20), duration=1200)
        did_work = await rt.scheduler.run_once(now)
        item = await get_domain(rt, domain_id)
        assert did_work is False or item.status == DomainStatus.EXPIRED.value
        assert item.status == DomainStatus.EXPIRED.value
        assert rt.client.calls == []
    finally:
        await rt.close()


@pytest.mark.asyncio
async def test_daily_schedule_rolls_to_next_day_on_recovery(tmp_path: Path) -> None:
    rt = await make_runtime(tmp_path)
    start = datetime(2026, 1, 1, 14, 0, 0, tzinfo=UTC)
    now = datetime(2026, 1, 3, 15, 0, 0, tzinfo=UTC)
    try:
        domain_id = await add_domain(rt, "daily.ai", now=start, schedule_type=ScheduleType.DAILY, duration=1200)
        await rt.scheduler.recover(now)
        item = await get_domain(rt, domain_id)
        assert item.status == DomainStatus.SCHEDULED.value
        assert ensure_aware_utc(item.start_at) == datetime(2026, 1, 4, 14, 0, 0, tzinfo=UTC)
        assert ensure_aware_utc(item.next_check_at) == ensure_aware_utc(item.start_at)
    finally:
        await rt.close()


@pytest.mark.asyncio
async def test_successful_registration_stops_future_search(tmp_path: Path) -> None:
    rt = await make_runtime(tmp_path)
    now = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    try:
        domain_id = await add_domain(rt, "win.ai", now=now)
        rt.client.search_results = [availability("win.ai", available=True)]
        rt.client.register_results = [registration("win.ai", success=True)]
        await rt.scheduler.run_once(now)
        await rt.scheduler.run_once(now + timedelta(seconds=1))
        item = await get_domain(rt, domain_id)
        assert item.status == DomainStatus.REGISTERED.value
        assert rt.client.calls == [("search", "win.ai"), ("register", "win.ai")]
    finally:
        await rt.close()


@pytest.mark.asyncio
async def test_register_timeout_confirms_before_retrying(tmp_path: Path) -> None:
    rt = await make_runtime(tmp_path)
    now = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    try:
        domain_id = await add_domain(rt, "uncertain.ai", now=now)
        rt.client.search_results = [availability("uncertain.ai", available=True)]
        rt.client.register_results = [DynadotTimeoutError("timeout")]
        rt.client.confirm_results = [confirmation("uncertain.ai", registered=True)]
        await rt.scheduler.run_once(now)
        item = await get_domain(rt, domain_id)
        assert item.status == DomainStatus.REGISTERED.value
        assert rt.client.calls == [
            ("search", "uncertain.ai"),
            ("register", "uncertain.ai"),
            ("confirm", "uncertain.ai"),
        ]
    finally:
        await rt.close()


@pytest.mark.asyncio
async def test_duplicate_registration_prevention(tmp_path: Path) -> None:
    rt = await make_runtime(tmp_path)
    now = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    try:
        domain_id = await add_domain(rt, "locked.ai", now=now, status=DomainStatus.REGISTERING)
        await rt.registration.handle_available(domain_id, availability("locked.ai", available=True))
        assert rt.client.calls == []
        item = await get_domain(rt, domain_id)
        assert item.status == DomainStatus.REGISTERING.value
    finally:
        await rt.close()


@pytest.mark.asyncio
async def test_price_exceeded_never_registers(tmp_path: Path) -> None:
    rt = await make_runtime(tmp_path)
    now = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    try:
        domain_id = await add_domain(rt, "premium.ai", now=now, max_price=Decimal("200.00"))
        rt.client.search_results = [availability("premium.ai", available=True, price=Decimal("800.00"), premium=True)]
        await rt.scheduler.run_once(now)
        item = await get_domain(rt, domain_id)
        assert item.status == DomainStatus.PRICE_EXCEEDED.value
        assert rt.client.calls == [("search", "premium.ai")]
    finally:
        await rt.close()


@pytest.mark.asyncio
async def test_dry_run_records_simulated_register_only(tmp_path: Path) -> None:
    rt = await make_runtime(tmp_path, dry_run=True)
    now = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    try:
        domain_id = await add_domain(rt, "dry.ai", now=now)
        rt.client.search_results = [availability("dry.ai", available=True)]
        await rt.scheduler.run_once(now)
        item = await get_domain(rt, domain_id)
        assert item.status == DomainStatus.AVAILABLE.value
        assert rt.client.calls == [("search", "dry.ai")]
        async with rt.session_factory() as session:
            attempts = list((await session.execute(select(RegistrationAttempt))).scalars())
        assert attempts[0].confirmation_status == "dry_run"
    finally:
        await rt.close()


@pytest.mark.asyncio
async def test_search_network_failure_is_bounded_and_rescheduled(tmp_path: Path) -> None:
    rt = await make_runtime(tmp_path)
    now = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    try:
        domain_id = await add_domain(rt, "flaky.ai", now=now)
        rt.client.search_results = [DynadotNetworkError("connection reset")]
        await rt.scheduler.run_once(now)
        item = await get_domain(rt, domain_id)
        assert item.status == DomainStatus.WATCHING.value
        assert ensure_aware_utc(item.next_check_at) == now + timedelta(seconds=5)
        assert item.last_error == "connection reset"
        assert rt.client.calls == [("search", "flaky.ai")]
    finally:
        await rt.close()


@pytest.mark.asyncio
async def test_paused_and_registered_domains_are_skipped(tmp_path: Path) -> None:
    rt = await make_runtime(tmp_path)
    now = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    try:
        await add_domain(rt, "paused.ai", now=now, status=DomainStatus.PAUSED)
        await add_domain(rt, "registered.ai", now=now, status=DomainStatus.REGISTERED)
        did_work = await rt.scheduler.run_once(now)
        assert did_work is False
        assert rt.client.calls == []
    finally:
        await rt.close()


def test_state_machine_taken_path() -> None:
    class Obj:
        status = DomainStatus.WATCHING.value

    obj = Obj()
    apply_transition(obj, DomainStatus.TAKEN)
    assert obj.status == DomainStatus.TAKEN.value
    apply_transition(obj, DomainStatus.WATCHING)
    assert obj.status == DomainStatus.WATCHING.value
