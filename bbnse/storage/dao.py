"""Data access layer. All SQL lives here; nothing else opens a Session."""
from __future__ import annotations

import gzip
import hashlib
import json
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from ..core.logging_setup import get_logger
from .models import (
    Alert, Base, DealObservation, Delivery, EventCorrelation, EventState,
    FetchHealth, JobRun, Observation, RawSnapshot, utcnow,
)

log = get_logger(__name__)

# Local copy so storage does not import from processors.
SEVERITY_RANK = {"info": 0, "notable": 1, "critical": 2}


def _canonical(node: Any) -> Any:
    """Order-insensitive view of a payload, for hashing only.

    NSE does not guarantee stable row ordering: consecutive polls of
    live-analysis-variations return the same symbol set in a different order
    within the allSec bucket. Hashing the raw payload therefore reports every
    poll as "changed" and defeats snapshot dedup entirely. Sorting nested
    lists by their serialized form makes the hash reflect content, not order.
    The stored payload itself is never reordered.
    """
    if isinstance(node, dict):
        return {k: _canonical(v) for k, v in sorted(node.items())}
    if isinstance(node, list):
        items = [_canonical(v) for v in node]
        return sorted(items, key=lambda v: json.dumps(v, sort_keys=True,
                                                      default=str))
    return node


def content_hash(payload: Any) -> str:
    blob = json.dumps(_canonical(payload), sort_keys=True,
                      default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


class Dao:
    def __init__(self, db_url: str, echo: bool = False):
        # check_same_thread=False: APScheduler runs jobs on worker threads.
        connect_args = ({"check_same_thread": False}
                        if db_url.startswith("sqlite") else {})
        self.engine = create_engine(db_url, echo=echo, future=True,
                                    connect_args=connect_args)
        if db_url.startswith("sqlite"):
            from sqlalchemy import event

            @event.listens_for(self.engine, "connect")
            def _pragmas(conn, _rec):          # noqa: ANN001
                cur = conn.cursor()
                # WAL lets the report generator read while the poller writes.
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA synchronous=NORMAL")
                cur.close()

        self._sm = sessionmaker(bind=self.engine, expire_on_commit=False,
                                future=True)

    def create_all(self) -> None:
        Base.metadata.create_all(self.engine)

    @contextmanager
    def session(self) -> Iterable[Session]:
        s = self._sm()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    # -- raw snapshots -------------------------------------------------------
    def store_snapshot(self, category: str, payload: Any, *,
                       url: str = "", row_count: int = 0,
                       trade_date: date | None = None) -> tuple[int, bool]:
        """Store a payload. Returns (snapshot_id, was_new).

        Identical consecutive payloads bump seen_count instead of inserting.
        """
        h = content_hash(payload)
        raw = json.dumps(payload, default=str).encode("utf-8")
        with self.session() as s:
            existing = s.scalar(
                select(RawSnapshot).where(RawSnapshot.category == category,
                                          RawSnapshot.content_hash == h)
            )
            if existing:
                existing.seen_count += 1
                existing.last_seen_at = utcnow()
                return existing.id, False

            snap = RawSnapshot(
                category=category, content_hash=h,
                payload_gz=gzip.compress(raw, compresslevel=6),
                row_count=row_count, bytes_raw=len(raw), source_url=url,
                trade_date=trade_date,
            )
            s.add(snap)
            s.flush()
            return snap.id, True

    def load_snapshot(self, snapshot_id: int) -> Any:
        with self.session() as s:
            snap = s.get(RawSnapshot, snapshot_id)
            if snap is None:
                return None
            return json.loads(gzip.decompress(snap.payload_gz).decode("utf-8"))

    # -- observations --------------------------------------------------------
    def add_observations(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        with self.session() as s:
            s.add_all([Observation(**r) for r in rows])
        return len(rows)

    def add_deals(self, rows: list[dict]) -> int:
        """Insert deals, skipping ones already stored (stable dedupe_key)."""
        if not rows:
            return 0
        keys = [r["dedupe_key"] for r in rows]
        with self.session() as s:
            existing = set(s.scalars(
                select(DealObservation.dedupe_key)
                .where(DealObservation.dedupe_key.in_(keys))
            ).all())
            fresh = [r for r in rows if r["dedupe_key"] not in existing]
            if fresh:
                s.add_all([DealObservation(**r) for r in fresh])
        return len(fresh)

    def observations_for_date(self, trade_date: date,
                              category: str | None = None) -> list[Observation]:
        with self.session() as s:
            q = select(Observation).where(Observation.trade_date == trade_date)
            if category:
                q = q.where(Observation.category == category)
            return list(s.scalars(q.order_by(Observation.observed_at)).all())

    def deals_for_date(self, trade_date: date) -> list[DealObservation]:
        with self.session() as s:
            return list(s.scalars(
                select(DealObservation)
                .where(DealObservation.trade_date == trade_date)
                .order_by(DealObservation.value_cr.desc())
            ).all())

    def deals_between(self, start: date, end: date) -> list[DealObservation]:
        with self.session() as s:
            return list(s.scalars(
                select(DealObservation)
                .where(DealObservation.trade_date >= start,
                       DealObservation.trade_date <= end)
                .order_by(DealObservation.value_cr.desc())
            ).all())

    # -- event state ---------------------------------------------------------
    def get_event_state(self, event_key: str) -> EventState | None:
        with self.session() as s:
            return s.scalar(
                select(EventState).where(EventState.event_key == event_key)
            )

    def upsert_event_state(self, **fields) -> EventState:
        key = fields.pop("event_key")
        with self.session() as s:
            st = s.scalar(select(EventState).where(EventState.event_key == key))
            if st is None:
                st = EventState(event_key=key, **fields)
                s.add(st)
            else:
                for k, v in fields.items():
                    setattr(st, k, v)
            s.flush()
            s.refresh(st)
            return st

    def open_states(self, category: str | None = None) -> list[EventState]:
        with self.session() as s:
            q = select(EventState).where(EventState.state == "open")
            if category:
                q = q.where(EventState.category == category)
            return list(s.scalars(q).all())

    def close_stale_states(self, category: str, seen_keys: set[str],
                           max_missed: int) -> int:
        """Increment missed counters; close what has not been seen recently."""
        closed = 0
        with self.session() as s:
            states = s.scalars(
                select(EventState).where(EventState.category == category,
                                         EventState.state == "open")
            ).all()
            for st in states:
                if st.event_key in seen_keys:
                    st.missed_polls = 0
                    continue
                st.missed_polls += 1
                if st.missed_polls >= max_missed:
                    st.state = "closed"
                    st.closed_at = utcnow()
                    closed += 1
        return closed

    def reset_intraday_states(self, session_date: date) -> int:
        """Close states carried over from a previous session."""
        with self.session() as s:
            states = s.scalars(
                select(EventState).where(EventState.state == "open")
            ).all()
            n = 0
            for st in states:
                if st.session_date and st.session_date != session_date:
                    st.state = "closed"
                    st.closed_at = utcnow()
                    n += 1
            return n

    # -- cross-feed correlation ----------------------------------------------
    def get_correlation(self, dedup_key: str) -> EventCorrelation | None:
        with self.session() as s:
            return s.scalar(
                select(EventCorrelation)
                .where(EventCorrelation.dedup_key == dedup_key)
            )

    def open_correlation(self, *, dedup_key: str, dedup_group: str,
                         category: str, entity: str, severity: str,
                         session_date: date | None,
                         priority: int = 0) -> EventCorrelation:
        with self.session() as s:
            corr = EventCorrelation(
                dedup_key=dedup_key, dedup_group=dedup_group,
                first_category=category, entity=entity,
                top_severity=severity, top_priority=priority,
                corroborations=[], session_date=session_date,
            )
            s.add(corr)
            s.flush()
            s.refresh(corr)
            return corr

    def add_corroboration(self, dedup_key: str, category: str,
                          severity: str,
                          priority: int | None = None) -> EventCorrelation | None:
        """Record that another feed reported an already-known event."""
        with self.session() as s:
            corr = s.scalar(select(EventCorrelation)
                            .where(EventCorrelation.dedup_key == dedup_key))
            if corr is None:
                return None
            # JSON columns need reassignment, not in-place mutation, for
            # SQLAlchemy to notice the change.
            if category not in (corr.corroborations or []):
                corr.corroborations = list(corr.corroborations or []) + [category]
            corr.last_seen_at = utcnow()
            if (SEVERITY_RANK.get(severity, 0)
                    > SEVERITY_RANK.get(corr.top_severity, 0)):
                corr.top_severity = severity
            if priority is not None and priority > (corr.top_priority or 0):
                corr.top_priority = priority
            s.flush()
            s.refresh(corr)
            return corr

    def reopen_correlation(self, dedup_key: str, *, category: str,
                           severity: str, session_date: date | None,
                           priority: int = 0) -> EventCorrelation | None:
        """Reset a correlation whose window has expired.

        The same symbol can legitimately spurt twice in a day. Once the
        window lapses the old record must not make the second occurrence look
        like a corroboration of the first.
        """
        with self.session() as s:
            corr = s.scalar(select(EventCorrelation)
                            .where(EventCorrelation.dedup_key == dedup_key))
            if corr is None:
                return None
            corr.first_category = category
            corr.first_seen_at = utcnow()
            corr.last_seen_at = utcnow()
            corr.top_severity = severity
            corr.top_priority = priority
            corr.corroborations = []
            corr.first_alert_id = None
            corr.session_date = session_date
            s.flush()
            s.refresh(corr)
            return corr

    def prune_correlations(self, older_than: datetime) -> int:
        with self.session() as s:
            res = s.execute(delete(EventCorrelation).where(
                EventCorrelation.first_seen_at < older_than))
            return res.rowcount or 0

    # -- alerts --------------------------------------------------------------
    def record_alert(self, **fields) -> Alert:
        with self.session() as s:
            a = Alert(**fields)
            s.add(a)
            s.flush()
            s.refresh(a)
            return a

    def record_delivery(self, alert_id: int, channel: str, *,
                        delivered: bool, error: str = "") -> None:
        with self.session() as s:
            s.add(Delivery(alert_id=alert_id, channel=channel,
                           delivered=delivered, error=error))

    def alerts_for_date(self, trade_date: date) -> list[Alert]:
        with self.session() as s:
            return list(s.scalars(
                select(Alert).where(Alert.trade_date == trade_date)
                .order_by(Alert.created_at)
            ).all())

    def alerts_between(self, start: date, end: date) -> list[Alert]:
        with self.session() as s:
            return list(s.scalars(
                select(Alert).where(Alert.trade_date >= start,
                                    Alert.trade_date <= end)
                .order_by(Alert.created_at)
            ).all())

    def alert_counts_for_entities(self, entities: list[str],
                                  trade_date: date) -> dict[str, int]:
        """How many alerts each symbol has already produced today."""
        if not entities:
            return {}
        counts: dict[str, int] = {}
        with self.session() as s:
            # Chunked to stay clear of SQLite's variable limit.
            for i in range(0, len(entities), 400):
                chunk = entities[i:i + 400]
                rows = s.execute(
                    select(Alert.entity, func.count(Alert.id))
                    .where(Alert.trade_date == trade_date,
                           Alert.entity.in_(chunk))
                    .group_by(Alert.entity)
                ).all()
                counts.update({e: n for e, n in rows})
        return counts

    def alert_counts_by_category(self, start: date, end: date) -> dict[str, int]:
        with self.session() as s:
            rows = s.execute(
                select(Alert.category, func.count(Alert.id))
                .where(Alert.trade_date >= start, Alert.trade_date <= end)
                .group_by(Alert.category)
            ).all()
            return {c: n for c, n in rows}

    # -- health --------------------------------------------------------------
    def upsert_health(self, category: str, *, ok: bool, rows: int = 0,
                      elapsed: float = 0.0, error: str = "") -> FetchHealth:
        with self.session() as s:
            st = s.get(FetchHealth, category)
            if st is None:
                # Column defaults only materialise at flush, so set the
                # counters explicitly -- they are incremented below.
                st = FetchHealth(
                    category=category, consecutive_failures=0,
                    total_success=0, total_failure=0, last_error="",
                    last_row_count=0, last_elapsed_sec=0.0,
                )
                s.add(st)
            if ok:
                st.last_success_at = utcnow()
                st.consecutive_failures = 0
                st.total_success += 1
                st.last_row_count = rows
                st.last_elapsed_sec = elapsed
                st.last_error = ""
            else:
                st.last_failure_at = utcnow()
                st.consecutive_failures += 1
                st.total_failure += 1
                st.last_error = error[:1000]
            s.flush()
            s.refresh(st)
            return st

    def all_health(self) -> list[FetchHealth]:
        with self.session() as s:
            return list(s.scalars(select(FetchHealth)).all())

    # -- job runs (catch-up on an intermittent machine) ----------------------
    def job_ran(self, job_id: str, run_date: date) -> bool:
        with self.session() as s:
            return s.scalar(
                select(func.count(JobRun.id)).where(
                    JobRun.job_id == job_id, JobRun.run_date == run_date,
                    JobRun.status == "ok")
            ) > 0

    def mark_job(self, job_id: str, run_date: date, status: str,
                 detail: str = "") -> None:
        with self.session() as s:
            jr = s.scalar(select(JobRun).where(JobRun.job_id == job_id,
                                               JobRun.run_date == run_date))
            if jr is None:
                jr = JobRun(job_id=job_id, run_date=run_date, status="running",
                            detail="")
                s.add(jr)
            jr.status = status
            jr.detail = detail[:2000]
            jr.finished_at = utcnow()

    # -- retention -----------------------------------------------------------
    def prune(self, raw_days: int, obs_days: int) -> dict[str, int]:
        now = datetime.now(timezone.utc)
        with self.session() as s:
            r = s.execute(delete(RawSnapshot).where(
                RawSnapshot.fetched_at < now - timedelta(days=raw_days)))
            o = s.execute(delete(Observation).where(
                Observation.observed_at < now - timedelta(days=obs_days)))
            return {"raw_snapshot": r.rowcount or 0,
                    "observation": o.rowcount or 0}

    def stats(self) -> dict[str, int]:
        out = {}
        with self.session() as s:
            for model in (RawSnapshot, Observation, DealObservation,
                          EventState, EventCorrelation, Alert, Delivery,
                          FetchHealth):
                out[model.__tablename__] = s.scalar(
                    select(func.count()).select_from(model)) or 0
        return out
