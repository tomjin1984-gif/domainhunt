from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from domainbot.state_machine import DomainStatus, ScheduleType
from domainbot.utils.time import utc_now


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
        nullable=False,
    )


class Domain(Base, TimestampMixin):
    __tablename__ = "domains"
    __table_args__ = (
        UniqueConstraint("domain", name="uq_domains_domain"),
        Index("ix_domains_next_check_at_status", "next_check_at", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    domain: Mapped[str] = mapped_column(String(255), nullable=False)
    tld: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(64), default=DomainStatus.PENDING.value, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    schedule_type: Mapped[str] = mapped_column(String(16), default=ScheduleType.ONCE.value, nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Singapore", nullable=False)
    daily_start_time: Mapped[str | None] = mapped_column(String(16), nullable=True)

    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    effective_interval_seconds: Mapped[float] = mapped_column(Numeric(10, 3), nullable=False)

    next_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_available_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    registration_years: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    max_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    last_observed_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    last_observed_currency: Mapped[str | None] = mapped_column(String(8), nullable=True)

    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    registration_attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    registered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dynadot_order_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    availability_checks: Mapped[list["AvailabilityCheck"]] = relationship(
        back_populates="domain",
        cascade="all, delete-orphan",
        order_by="AvailabilityCheck.checked_at",
    )
    registration_attempts: Mapped[list["RegistrationAttempt"]] = relationship(
        back_populates="domain",
        cascade="all, delete-orphan",
        order_by="RegistrationAttempt.attempted_at",
    )


class RegistrationAttempt(Base):
    __tablename__ = "registration_attempts"
    __table_args__ = (Index("ix_registration_attempts_domain_attempted", "domain_id", "attempted_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    domain_id: Mapped[int] = mapped_column(ForeignKey("domains.id", ondelete="CASCADE"), nullable=False)
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)

    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    dynadot_order_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    api_response_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    api_response_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)

    success: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    confirmation_status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    raw_response_redacted: Mapped[str | None] = mapped_column(Text, nullable=True)

    domain: Mapped[Domain] = relationship(back_populates="registration_attempts")


class AvailabilityCheck(Base):
    __tablename__ = "availability_checks"
    __table_args__ = (Index("ix_availability_checks_domain_checked", "domain_id", "checked_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    domain_id: Mapped[int] = mapped_column(ForeignKey("domains.id", ondelete="CASCADE"), nullable=False)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    available: Mapped[bool] = mapped_column(Boolean, nullable=False)

    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    api_response_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    response_time_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    domain: Mapped[Domain] = relationship(back_populates="availability_checks")

