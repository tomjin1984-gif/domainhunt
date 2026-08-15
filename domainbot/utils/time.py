from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


UTC = timezone.utc


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


def ensure_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def parse_local_datetime(value: str, timezone_name: str) -> datetime:
    zone = ZoneInfo(timezone_name)
    raw = value.strip()
    if "T" in raw:
        raw = raw.replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            local = datetime.strptime(raw, fmt).replace(tzinfo=zone)
            return local.astimezone(UTC)
        except ValueError:
            pass
    raise ValueError("start time must be YYYY-MM-DD HH:MM[:SS]")


def parse_local_time(value: str) -> time:
    raw = value.strip()
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(raw, fmt).time()
        except ValueError:
            pass
    raise ValueError("daily start time must be HH:MM[:SS]")


def next_daily_window_start(
    local_start: time,
    timezone_name: str,
    now: datetime | None = None,
) -> datetime:
    zone = ZoneInfo(timezone_name)
    current = ensure_aware_utc(now or utc_now()).astimezone(zone)
    candidate = datetime.combine(current.date(), local_start, zone)
    if candidate <= current:
        candidate += timedelta(days=1)
    return candidate.astimezone(UTC)


def next_daily_window_after(
    previous_start: datetime,
    timezone_name: str,
    now: datetime | None = None,
) -> datetime:
    zone = ZoneInfo(timezone_name)
    local_previous = ensure_aware_utc(previous_start).astimezone(zone)
    local_time = local_previous.time().replace(microsecond=0)
    return next_daily_window_start(local_time, timezone_name, now=now)


def serialize_time(value: time) -> str:
    return value.replace(microsecond=0).isoformat()

