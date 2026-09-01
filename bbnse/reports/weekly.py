"""Weekly report -- Monday to Friday of the anchor date's trading week."""
from __future__ import annotations

from datetime import date, timedelta

from .periodic import Period, PeriodReport


class WeeklyReport(PeriodReport):
    slug = "weekly"
    noun = "Week"

    def period_for(self, anchor: date) -> Period:
        # Anchor is typically Friday close or Saturday morning; either way we
        # want that Monday-to-Friday block.
        monday = anchor - timedelta(days=anchor.weekday())
        friday = monday + timedelta(days=4)
        return Period(monday, friday,
                      f"week of {monday:%d %b} – {friday:%d %b %Y}")

    def previous_period(self, period: Period) -> Period:
        start = period.start - timedelta(days=7)
        end = period.end - timedelta(days=7)
        return Period(start, end, f"week of {start:%d %b %Y}")
