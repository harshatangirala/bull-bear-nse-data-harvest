"""Fetcher health tracking.

The failure mode this guards against is the quiet one: a fetcher that has
been returning empty lists for three days because NSE moved its endpoint,
with nothing in the alert stream to tell you.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ..storage.dao import Dao
from .logging_setup import get_logger

log = get_logger(__name__)


class HealthMonitor:
    def __init__(self, cfg, dao: Dao):
        self.cfg = cfg
        self.dao = dao
        h = cfg.section("health")
        self.failure_threshold = int(h.get("consecutive_failure_alert", 3))
        self.staleness_minutes = int(h.get("staleness_alert_minutes", 90))
        self.severity = h.get("severity", "critical")

    def record_success(self, category: str, rows: int, elapsed: float) -> None:
        self.dao.upsert_health(category, ok=True, rows=rows, elapsed=elapsed)

    def record_failure(self, category: str, error: str) -> None:
        state = self.dao.upsert_health(category, ok=False, error=error)
        if state.consecutive_failures == self.failure_threshold:
            log.error("fetcher crossed failure threshold",
                      extra={"category": category,
                             "consecutive": state.consecutive_failures})

    def problems(self) -> list[dict]:
        """Categories that look broken right now."""
        out: list[dict] = []
        now = datetime.now(timezone.utc)
        for st in self.dao.all_health():
            if st.consecutive_failures >= self.failure_threshold:
                out.append({
                    "category": st.category, "kind": "failing",
                    "detail": (f"{st.consecutive_failures} consecutive failures; "
                               f"last error: {st.last_error or 'n/a'}"),
                })
                continue
            if st.last_success_at:
                last = st.last_success_at
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                if now - last > timedelta(minutes=self.staleness_minutes):
                    mins = int((now - last).total_seconds() // 60)
                    out.append({
                        "category": st.category, "kind": "stale",
                        "detail": f"no successful fetch in {mins} min",
                    })
        return out
