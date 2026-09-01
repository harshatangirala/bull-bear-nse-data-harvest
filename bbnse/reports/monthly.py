"""Monthly report -- calendar month of the anchor date, vs the prior month."""
from __future__ import annotations

import calendar as _cal
from datetime import date

from .periodic import Period, PeriodReport


def _month_bounds(year: int, month: int) -> tuple[date, date]:
    last = _cal.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last)


class MonthlyReport(PeriodReport):
    slug = "monthly"
    noun = "Month"

    def period_for(self, anchor: date) -> Period:
        start, end = _month_bounds(anchor.year, anchor.month)
        return Period(start, end, f"{start:%B %Y}")

    def previous_period(self, period: Period) -> Period:
        year, month = period.start.year, period.start.month
        if month == 1:
            year, month = year - 1, 12
        else:
            month -= 1
        start, end = _month_bounds(year, month)
        return Period(start, end, f"{start:%B %Y}")
