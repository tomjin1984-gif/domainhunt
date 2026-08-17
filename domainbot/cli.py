from __future__ import annotations

import asyncio
import csv
import logging
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

import typer
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from domainbot.config import Settings, get_settings
from domainbot.database import create_engine, create_session_factory, init_db
from domainbot.dynadot.client import DynadotClient
from domainbot.dynadot.queue import DynadotAPIQueue
from domainbot.dynadot.schemas import AvailabilityResult
from domainbot.models import Domain
from domainbot.scheduler import DomainScheduler, effective_interval
from domainbot.services.availability import AvailabilityService
from domainbot.services.notifications import NotificationService
from domainbot.services.registration import RegistrationService
from domainbot.state_machine import DomainStatus, ScheduleType
from domainbot.utils.domain import domain_tld, normalize_domain
from domainbot.utils.duration import parse_duration_seconds
from domainbot.utils.logging import configure_logging
from domainbot.utils.time import current_or_next_daily_window_start, ensure_aware_utc, next_daily_window_start, parse_local_datetime, parse_local_time, utc_now
from domainbot.utils.tld_rules import effective_registration_years


app = typer.Typer(help="Dynadot multi-domain monitoring and registration daemon.")
logger = logging.getLogger(__name__)


def _settings() -> Settings:
    settings = get_settings()
    configure_logging(settings)
    return settings


def _run(coro):
    return asyncio.run(coro)


def _parse_start(
    start: str | None,
    *,
    schedule_type: ScheduleType,
    timezone_name: str,
    duration_seconds: int,
    now: datetime | None = None,
) -> tuple[datetime, str | None]:
    current = ensure_aware_utc(now or utc_now())
    if start is None or not start.strip():
        if schedule_type == ScheduleType.DAILY:
            local = current.astimezone(ZoneInfo(timezone_name)).time().replace(microsecond=0)
            return current, local.isoformat()
        return current, None
    raw = start.strip()
    if schedule_type == ScheduleType.DAILY and raw.count(":") >= 1 and "-" not in raw:
        local_time = parse_local_time(raw)
        if duration_seconds >= 24 * 60 * 60:
            start_at = current_or_next_daily_window_start(local_time, timezone_name, duration_seconds, now=current)
        else:
            start_at = next_daily_window_start(local_time, timezone_name, now=current)
        return start_at, local_time.replace(microsecond=0).isoformat()
    if raw.count(":") >= 1 and "-" not in raw:
        local_time = parse_local_time(raw)
        return next_daily_window_start(local_time, timezone_name, now=current), local_time.replace(microsecond=0).isoformat()
    start_at = parse_local_datetime(raw, timezone_name)
    daily_time = None
    if schedule_type == ScheduleType.DAILY:
        daily_time = start_at.astimezone(ZoneInfo(timezone_name)).time().replace(microsecond=0).isoformat()
    return start_at, daily_time


async def _add_domain(
    *,
    settings: Settings,
    domain_value: str,
    start: str | None,
    duration: str | int | None,
    interval: int | None,
    timezone_name: str | None,
    max_price: Decimal | None,
    registration_years: int | None,
    schedule_type_value: str,
) -> None:
    normalized = normalize_domain(domain_value)
    schedule_type = ScheduleType(schedule_type_value)
    tz = timezone_name or settings.default_timezone
    duration_seconds = parse_duration_seconds(duration, settings.default_duration_seconds)
    interval_seconds = int(interval or settings.default_interval_seconds)
    if interval_seconds <= 0:
        raise typer.BadParameter("interval must be positive")
    now = utc_now()
    start_at, daily_start_time = _parse_start(
        start,
        schedule_type=schedule_type,
        timezone_name=tz,
        duration_seconds=duration_seconds,
        now=now,
    )
    window_end = start_at + timedelta(seconds=duration_seconds)
    effective = effective_interval(interval_seconds, settings)
    if effective > interval_seconds:
        logger.warning(
            "requested interval=%ss adjusted to effective_interval=%ss because Dynadot rate limit is %s rpm",
            interval_seconds,
            effective,
            settings.dynadot_rate_limit_rpm,
        )

    status = DomainStatus.SCHEDULED if start_at > now else DomainStatus.WATCHING
    requested_registration_years = registration_years or settings.default_registration_years
    final_registration_years = effective_registration_years(normalized, requested_registration_years)
    if final_registration_years != requested_registration_years:
        logger.info(
            "registration years adjusted for TLD rule: domain=%s requested=%s effective=%s",
            normalized,
            requested_registration_years,
            final_registration_years,
        )
    domain = Domain(
        domain=normalized,
        tld=domain_tld(normalized),
        status=status.value,
        enabled=True,
        schedule_type=schedule_type.value,
        start_at=start_at,
        window_end=window_end,
        duration_seconds=duration_seconds,
        interval_seconds=interval_seconds,
        effective_interval_seconds=effective,
        timezone=tz,
        daily_start_time=daily_start_time,
        next_check_at=start_at if start_at > now else now,
        registration_years=final_registration_years,
        max_price=max_price,
    )
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as session:
            session.add(domain)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                raise typer.BadParameter(f"domain already exists: {normalized}") from None
    finally:
        await engine.dispose()
    typer.echo(f"added {normalized} status={status.value} next_check_at={domain.next_check_at}")


@app.command()
def init() -> None:
    """Initialize the SQLite database and log directory."""
    settings = _settings()
    _run(init_db(settings=settings))
    typer.echo("domainbot initialized")


@app.command()
def add(
    domain: Annotated[str, typer.Argument(help="Domain name to monitor.")],
    start: Annotated[str | None, typer.Option("--start", help="Local start time, e.g. 2026-08-20 14:00:00.")] = None,
    duration: Annotated[str | None, typer.Option("--duration", help="Duration such as 20m, 1200, 1h.")] = None,
    interval: Annotated[int | None, typer.Option("--interval", help="Search interval seconds.")] = None,
    timezone_name: Annotated[str | None, typer.Option("--timezone", help="IANA timezone name.")] = None,
    max_price: Annotated[str | None, typer.Option("--max-price", help="Maximum accepted registration price.")] = None,
    registration_years: Annotated[int | None, typer.Option("--registration-years", help="Registration duration in years.")] = None,
    schedule_type: Annotated[str, typer.Option("--schedule-type", help="once or daily.")] = ScheduleType.ONCE.value,
) -> None:
    settings = _settings()
    _run(
        _add_domain(
            settings=settings,
            domain_value=domain,
            start=start,
            duration=duration,
            interval=interval,
            timezone_name=timezone_name,
            max_price=Decimal(max_price) if max_price else None,
            registration_years=registration_years,
            schedule_type_value=schedule_type,
        )
    )


@app.command("import")
def import_domains(file: Annotated[Path, typer.Argument(help="CSV file with domain,start_at,duration,interval,timezone,max_price,registration_years,schedule_type")]) -> None:
    settings = _settings()
    with file.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            _run(
                _add_domain(
                    settings=settings,
                    domain_value=row["domain"],
                    start=row.get("start_at") or None,
                    duration=row.get("duration") or None,
                    interval=int(row["interval"]) if row.get("interval") else None,
                    timezone_name=row.get("timezone") or None,
                    max_price=Decimal(row["max_price"]) if row.get("max_price") else None,
                    registration_years=int(row["registration_years"]) if row.get("registration_years") else None,
                    schedule_type_value=row.get("schedule_type") or ScheduleType.ONCE.value,
                )
            )


@app.command()
def remove(
    domains: Annotated[list[str] | None, typer.Argument(help="Domain names to remove.")] = None,
    file: Annotated[Path | None, typer.Option("--file", help="Text file with one domain per line.")] = None,
) -> None:
    settings = _settings()
    targets = list(domains or [])
    if file:
        targets.extend(line.strip() for line in file.read_text(encoding="utf-8").splitlines() if line.strip())
    if not targets:
        raise typer.BadParameter("provide domains or --file")

    async def _remove() -> None:
        engine = create_engine(settings)
        session_factory = create_session_factory(engine)
        try:
            async with session_factory() as session:
                normalized = [normalize_domain(value) for value in targets]
                await session.execute(delete(Domain).where(Domain.domain.in_(normalized)))
                await session.commit()
                typer.echo(f"removed {len(normalized)} domain(s)")
        finally:
            await engine.dispose()

    _run(_remove())


async def _set_status(domain_value: str, status: DomainStatus) -> None:
    settings = _settings()
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as session:
            domain = await _get_domain(session, domain_value)
            if status == DomainStatus.PAUSED:
                domain.status = DomainStatus.PAUSED.value
                domain.next_check_at = None
            elif status == DomainStatus.PENDING:
                domain.status = DomainStatus.PENDING.value
                domain.enabled = True
                domain.next_check_at = None
            await session.commit()
        if status == DomainStatus.PENDING:
            client = DynadotClient(settings)
            queue = DynadotAPIQueue(min_interval_seconds=settings.dynadot_min_safe_interval_seconds)
            notifications = NotificationService(settings)
            availability = AvailabilityService(settings=settings, session_factory=session_factory, client=client, api_queue=queue)
            registration = RegistrationService(settings=settings, session_factory=session_factory, client=client, api_queue=queue, notifications=notifications)
            scheduler = DomainScheduler(settings=settings, session_factory=session_factory, availability=availability, registration=registration, notifications=notifications)
            await scheduler.recover()
            await queue.close()
            await client.aclose()
    finally:
        await engine.dispose()


@app.command()
def pause(domain: Annotated[str, typer.Argument()]) -> None:
    _run(_set_status(domain, DomainStatus.PAUSED))
    typer.echo(f"paused {normalize_domain(domain)}")


@app.command()
def resume(domain: Annotated[str, typer.Argument()]) -> None:
    _run(_set_status(domain, DomainStatus.PENDING))
    typer.echo(f"resumed {normalize_domain(domain)}")


@app.command("list")
def list_domains() -> None:
    settings = _settings()

    async def _list() -> None:
        engine = create_engine(settings)
        session_factory = create_session_factory(engine)
        try:
            async with session_factory() as session:
                rows = await session.execute(select(Domain).order_by(Domain.next_check_at.asc().nulls_last(), Domain.domain.asc()))
                domains = list(rows.scalars())
        finally:
            await engine.dispose()
        typer.echo("domain,status,next_check_at,last_check_at,attempts,registration_attempts")
        for item in domains:
            typer.echo(
                f"{item.domain},{item.status},{item.next_check_at},{item.last_check_at},"
                f"{item.attempt_count},{item.registration_attempt_count}"
            )

    _run(_list())


@app.command()
def status(domain: Annotated[str, typer.Argument()]) -> None:
    settings = _settings()

    async def _status() -> None:
        engine = create_engine(settings)
        session_factory = create_session_factory(engine)
        try:
            async with session_factory() as session:
                item = await _get_domain(session, domain)
                fields = [
                    "domain",
                    "status",
                    "enabled",
                    "schedule_type",
                    "start_at",
                    "window_end",
                    "next_check_at",
                    "last_check_at",
                    "last_available_at",
                    "registration_years",
                    "max_price",
                    "last_observed_price",
                    "last_observed_currency",
                    "registration_attempt_count",
                    "registered_at",
                    "dynadot_order_id",
                    "last_error",
                ]
                for field in fields:
                    typer.echo(f"{field}: {getattr(item, field)}")
        finally:
            await engine.dispose()

    _run(_status())


@app.command()
def run(domain: Annotated[str, typer.Argument(help="Run one immediate check for a domain.")], dry_run: Annotated[bool, typer.Option("--dry-run")] = False) -> None:
    settings = _settings()

    async def _run_one() -> None:
        engine = create_engine(settings)
        session_factory = create_session_factory(engine)
        client = DynadotClient(settings)
        queue = DynadotAPIQueue(min_interval_seconds=settings.dynadot_min_safe_interval_seconds)
        notifications = NotificationService(settings)
        availability = AvailabilityService(settings=settings, session_factory=session_factory, client=client, api_queue=queue)
        registration = RegistrationService(
            settings=settings,
            session_factory=session_factory,
            client=client,
            api_queue=queue,
            notifications=notifications,
            dry_run=dry_run,
        )
        try:
            async with session_factory() as session:
                item = await _get_domain(session, domain)
                item.status = DomainStatus.WATCHING.value
                item.next_check_at = utc_now()
                if ensure_aware_utc(item.window_end) <= utc_now():
                    item.window_end = utc_now() + timedelta(seconds=item.duration_seconds)
                await session.commit()
                domain_id = item.id
            result = await availability.check_domain(domain_id)
            typer.echo(f"{result.domain}: {'available' if result.available else 'unavailable'}")
            if result.available:
                async with session_factory() as session:
                    item = await session.get(Domain, domain_id)
                    if item is not None:
                        item.status = DomainStatus.AVAILABLE.value
                        item.next_check_at = None
                        await session.commit()
                await registration.handle_available(domain_id, result)
        finally:
            await queue.close()
            await client.aclose()
            await engine.dispose()

    _run(_run_one())


@app.command()
def stop(domain: Annotated[str, typer.Argument(help="Pause monitoring for a domain.")]) -> None:
    _run(_set_status(domain, DomainStatus.PAUSED))
    typer.echo(f"stopped {normalize_domain(domain)}")


@app.command()
def daemon(dry_run: Annotated[bool, typer.Option("--dry-run", help="Search normally but never send Dynadot Register.")] = False) -> None:
    settings = _settings()

    async def _daemon() -> None:
        await init_db(settings=settings)
        engine = create_engine(settings)
        session_factory = create_session_factory(engine)
        client = DynadotClient(settings)
        queue = DynadotAPIQueue(min_interval_seconds=settings.dynadot_min_safe_interval_seconds)
        notifications = NotificationService(settings)
        availability = AvailabilityService(settings=settings, session_factory=session_factory, client=client, api_queue=queue)
        registration = RegistrationService(
            settings=settings,
            session_factory=session_factory,
            client=client,
            api_queue=queue,
            notifications=notifications,
            dry_run=dry_run,
        )
        scheduler = DomainScheduler(settings=settings, session_factory=session_factory, availability=availability, registration=registration, notifications=notifications)
        try:
            if settings.default_interval_seconds < settings.dynadot_min_safe_interval_seconds:
                logger.warning(
                    "default interval adjusted by Dynadot rate limit: requested=%s effective=%s",
                    settings.default_interval_seconds,
                    settings.dynadot_min_safe_interval_seconds,
                )
            await scheduler.run_forever()
        finally:
            await queue.close()
            await client.aclose()
            await engine.dispose()

    try:
        _run(_daemon())
    except KeyboardInterrupt:
        typer.echo("domainbot daemon stopped")


@app.command("test-api")
def test_api() -> None:
    settings = _settings()

    async def _test() -> None:
        client = DynadotClient(settings)
        queue = DynadotAPIQueue(min_interval_seconds=settings.dynadot_min_safe_interval_seconds)
        try:
            result = await queue.call(10, "TEST_API SEARCH example.com", client.test_api)
            typer.echo("Dynadot API reachable")
            typer.echo(f"HTTP: {result.http_status}")
            typer.echo(f"API code: {result.api_code}")
            typer.echo(f"example.com available: {result.available}")
            typer.echo(f"test mode: {settings.dynadot_test_mode}")
        finally:
            await queue.close()
            await client.aclose()

    _run(_test())


@app.command()
def logs(lines: Annotated[int, typer.Option("--lines", "-n", help="Number of log lines.")] = 100) -> None:
    settings = _settings()
    path = settings.log_file
    if not path.exists():
        typer.echo(f"log file not found: {path}")
        return
    content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in content[-lines:]:
        typer.echo(line)


async def _get_domain(session, domain_value: str) -> Domain:
    normalized = normalize_domain(domain_value)
    row = await session.execute(select(Domain).where(Domain.domain == normalized))
    domain = row.scalar_one_or_none()
    if domain is None:
        raise typer.BadParameter(f"domain not found: {normalized}")
    return domain


if __name__ == "__main__":
    app()
