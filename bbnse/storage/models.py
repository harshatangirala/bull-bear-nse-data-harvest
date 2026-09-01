"""SQLAlchemy models.

Two-layer storage on purpose:

* RawSnapshot keeps the exact bytes NSE returned, gzipped and content-hashed.
  Identical consecutive payloads are not re-stored -- the pre-open feed is
  2 MB and ~99% unchanged between polls, so naive storage would be GBs/week.
* Observation / DealObservation hold normalized, queryable rows. Reports read
  only these, so raw retention can be trimmed without breaking history.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON, BigInteger, Boolean, Date, DateTime, Float, ForeignKey, Index,
    Integer, LargeBinary, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class RawSnapshot(Base):
    """One stored payload. Deduped by (category, content_hash)."""
    __tablename__ = "raw_snapshot"

    id: Mapped[int] = mapped_column(primary_key=True)
    category: Mapped[str] = mapped_column(String(64), index=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow,
                                                 index=True)
    trade_date: Mapped[datetime | None] = mapped_column(Date, nullable=True,
                                                        index=True)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    payload_gz: Mapped[bytes] = mapped_column(LargeBinary)
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    bytes_raw: Mapped[int] = mapped_column(Integer, default=0)
    # How many polls returned this identical payload.
    seen_count: Mapped[int] = mapped_column(Integer, default=1)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    source_url: Mapped[str] = mapped_column(Text, default="")

    __table_args__ = (
        UniqueConstraint("category", "content_hash", name="uq_snapshot_hash"),
    )


class Observation(Base):
    """A normalized quote-shaped row (gainers, losers, 52w, spurts...)."""
    __tablename__ = "observation"

    id: Mapped[int] = mapped_column(primary_key=True)
    category: Mapped[str] = mapped_column(String(64), index=True)
    bucket: Mapped[str] = mapped_column(String(32), default="", index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    company: Mapped[str] = mapped_column(Text, default="")
    observed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow,
                                                  index=True)
    trade_date: Mapped[datetime | None] = mapped_column(Date, nullable=True,
                                                        index=True)

    last_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    prev_close: Mapped[float | None] = mapped_column(Float, nullable=True)
    change: Mapped[float | None] = mapped_column(Float, nullable=True)
    pct_change: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    traded_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 52-week specific
    extreme_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    prev_extreme: Mapped[float | None] = mapped_column(Float, nullable=True)
    prev_extreme_date: Mapped[str | None] = mapped_column(String(32),
                                                          nullable=True)
    extra: Mapped[dict] = mapped_column(JSON, default=dict)

    __table_args__ = (
        Index("ix_obs_cat_sym_time", "category", "symbol", "observed_at"),
        Index("ix_obs_cat_date", "category", "trade_date"),
    )


class DealObservation(Base):
    """Bulk / block / short deal row."""
    __tablename__ = "deal_observation"

    id: Mapped[int] = mapped_column(primary_key=True)
    deal_type: Mapped[str] = mapped_column(String(16), index=True)  # BULK/BLOCK/SHORT
    trade_date: Mapped[datetime | None] = mapped_column(Date, nullable=True,
                                                        index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    company: Mapped[str] = mapped_column(Text, default="")
    client_name: Mapped[str] = mapped_column(Text, default="")
    buy_sell: Mapped[str] = mapped_column(String(16), default="")
    quantity: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    value_cr: Mapped[float | None] = mapped_column(Float, nullable=True)
    remarks: Mapped[str] = mapped_column(Text, default="")
    observed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # Stable identity so re-polling the same day cannot duplicate rows.
    dedupe_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    __table_args__ = (
        Index("ix_deal_date_type", "trade_date", "deal_type"),
    )


class EventState(Base):
    """Open/closed state per (category, rule, entity, bucket).

    This is what makes alerts fire on transitions instead of every poll.
    """
    __tablename__ = "event_state"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    category: Mapped[str] = mapped_column(String(64), index=True)
    rule_id: Mapped[str] = mapped_column(String(64), index=True)
    entity: Mapped[str] = mapped_column(String(64), index=True)
    state_bucket: Mapped[str] = mapped_column(String(64), default="")

    state: Mapped[str] = mapped_column(String(16), default="open")  # open|closed
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_notified_at: Mapped[datetime | None] = mapped_column(DateTime,
                                                              nullable=True)
    notify_count: Mapped[int] = mapped_column(Integer, default=0)
    reminder_count: Mapped[int] = mapped_column(Integer, default=0)
    missed_polls: Mapped[int] = mapped_column(Integer, default=0)

    # Value at first alert, used for escalate_on_pct_move.
    trigger_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    session_date: Mapped[datetime | None] = mapped_column(Date, nullable=True,
                                                          index=True)


class EventCorrelation(Base):
    """One real-world event that several feeds may each report.

    NSE publishes overlapping data: a block deal above the bulk threshold
    appears in both deal feeds; a stock moving hard shows up in gainers, in
    volume spurts and at its price band. Without correlation you get one alert
    per feed for one event. Rows here record which feed reported an event
    first, so later feeds within the window corroborate instead of re-alerting.
    """
    __tablename__ = "event_correlation"

    id: Mapped[int] = mapped_column(primary_key=True)
    dedup_key: Mapped[str] = mapped_column(String(96), unique=True, index=True)
    dedup_group: Mapped[str] = mapped_column(String(32), index=True)
    first_category: Mapped[str] = mapped_column(String(64))
    entity: Mapped[str] = mapped_column(String(64), index=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow,
                                                    index=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    first_alert_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    top_severity: Mapped[str] = mapped_column(String(16), default="info")
    # Rank of the feed that currently owns the headline. A later feed that
    # outranks it is reporting something the incumbent could not (a circuit
    # lock is not just a big move), so it is allowed through once.
    top_priority: Mapped[int] = mapped_column(Integer, default=0)
    # Categories that later reported the same event.
    corroborations: Mapped[list] = mapped_column(JSON, default=list)
    session_date: Mapped[datetime | None] = mapped_column(Date, nullable=True,
                                                          index=True)

    __table_args__ = (
        Index("ix_corr_group_session", "dedup_group", "session_date"),
    )


class Alert(Base):
    __tablename__ = "alert"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_key: Mapped[str] = mapped_column(String(64), index=True)
    category: Mapped[str] = mapped_column(String(64), index=True)
    rule_id: Mapped[str] = mapped_column(String(64), index=True)
    entity: Mapped[str] = mapped_column(String(64), index=True)
    severity: Mapped[str] = mapped_column(String(16), index=True)
    kind: Mapped[str] = mapped_column(String(16), default="new")  # new|reminder|escalation
    title: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text, default="")
    value: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow,
                                                 index=True)
    trade_date: Mapped[datetime | None] = mapped_column(Date, nullable=True,
                                                        index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)

    deliveries: Mapped[list["Delivery"]] = relationship(
        back_populates="alert", cascade="all, delete-orphan"
    )


class Delivery(Base):
    __tablename__ = "delivery"

    id: Mapped[int] = mapped_column(primary_key=True)
    alert_id: Mapped[int] = mapped_column(ForeignKey("alert.id"), index=True)
    channel: Mapped[str] = mapped_column(String(32), index=True)
    delivered: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str] = mapped_column(Text, default="")
    attempted_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    alert: Mapped[Alert] = relationship(back_populates="deliveries")


class FetchHealth(Base):
    __tablename__ = "fetch_health"

    category: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime,
                                                             nullable=True)
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime,
                                                             nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    total_success: Mapped[int] = mapped_column(Integer, default=0)
    total_failure: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(Text, default="")
    last_row_count: Mapped[int] = mapped_column(Integer, default=0)
    last_elapsed_sec: Mapped[float] = mapped_column(Float, default=0.0)


class JobRun(Base):
    """Records scheduler runs so an intermittent laptop can backfill."""
    __tablename__ = "job_run"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[str] = mapped_column(String(64), index=True)
    run_date: Mapped[datetime] = mapped_column(Date, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime,
                                                         nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="running")
    detail: Mapped[str] = mapped_column(Text, default="")

    __table_args__ = (
        UniqueConstraint("job_id", "run_date", name="uq_job_run"),
    )
