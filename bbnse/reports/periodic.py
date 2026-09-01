"""Shared machinery for the weekly and monthly roll-ups.

Both aggregate the same normalized tables over a date range and compare
against the preceding range; only the range arithmetic and labelling differ.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

from ..core.logging_setup import get_logger
from ..storage.dao import Dao
from .base import ReportBuilder, ReportOutput, Section

log = get_logger(__name__)


def _delta_text(current: float, previous: float, unit: str = "") -> str:
    if previous == 0:
        return f"{current:,.0f}{unit} (no prior period)"
    change = (current - previous) / abs(previous) * 100.0
    arrow = "▲" if change > 0 else "▼" if change < 0 else "="
    return f"{current:,.0f}{unit} vs {previous:,.0f}{unit} ({arrow} {abs(change):.0f}%)"


@dataclass
class Period:
    start: date
    end: date
    label: str

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1


class PeriodReport:
    """Base for weekly/monthly. Subclasses supply the period arithmetic."""

    slug = "period"
    noun = "Period"

    def __init__(self, cfg, dao: Dao):
        self.cfg = cfg
        self.dao = dao

    # -- subclass hooks ------------------------------------------------------
    def period_for(self, anchor: date) -> Period:
        raise NotImplementedError

    def previous_period(self, period: Period) -> Period:
        raise NotImplementedError

    # -- generation ----------------------------------------------------------
    def generate(self, anchor: date, *, write: bool = True) -> ReportOutput:
        period = self.period_for(anchor)
        prior = self.previous_period(period)

        alerts = self.dao.alerts_between(period.start, period.end)
        prior_alerts = self.dao.alerts_between(prior.start, prior.end)
        deals = self.dao.deals_between(period.start, period.end)
        prior_deals = self.dao.deals_between(prior.start, prior.end)

        title = f"NSE {self.noun} Report — {period.label}"
        sev = Counter(a.severity for a in alerts)
        subtitle = (f"{period.start:%d %b} to {period.end:%d %b %Y} · "
                    f"{len(alerts)} alerts · {len(deals)} deals")
        rb = ReportBuilder(self.cfg, title, subtitle)

        # --- period vs prior -------------------------------------------------
        deal_cr = sum(d.value_cr or 0 for d in deals)
        prior_deal_cr = sum(d.value_cr or 0 for d in prior_deals)
        rb.add(Section(
            heading=f"{self.noun}-over-{self.noun.lower()} comparison",
            columns=["Metric", f"This {self.noun.lower()}", "Change"],
            numeric_cols={1},
            rows=[
                ["Alerts fired", len(alerts),
                 _delta_text(len(alerts), len(prior_alerts))],
                ["Critical alerts", sev.get("critical", 0),
                 _delta_text(sev.get("critical", 0),
                             sum(1 for a in prior_alerts
                                 if a.severity == "critical"))],
                ["Deals recorded", len(deals),
                 _delta_text(len(deals), len(prior_deals))],
                ["Deal value (Rs cr)", round(deal_cr, 1),
                 _delta_text(deal_cr, prior_deal_cr, " cr")],
            ],
        ))

        # --- alert mix by category ------------------------------------------
        by_cat = Counter(a.category for a in alerts)
        prior_by_cat = Counter(a.category for a in prior_alerts)
        rb.add(Section(
            heading="Alert mix by category",
            columns=["Category", "Alerts", "Prior", "Change"],
            numeric_cols={1, 2},
            rows=[[cat, n, prior_by_cat.get(cat, 0),
                   _delta_text(n, prior_by_cat.get(cat, 0))]
                  for cat, n in by_cat.most_common()],
            empty_text="No alerts in this period.",
        ))

        # --- most active names ----------------------------------------------
        per_symbol: dict[str, list] = defaultdict(list)
        for a in alerts:
            per_symbol[a.entity].append(a)
        busiest = sorted(per_symbol.items(), key=lambda kv: len(kv[1]),
                         reverse=True)[:20]
        rb.add(Section(
            heading=f"Most active names this {self.noun.lower()}",
            columns=["Symbol", "Alerts", "Critical", "Categories", "Days seen"],
            numeric_cols={1, 2, 4},
            rows=[[sym, len(items),
                   sum(1 for i in items if i.severity == "critical"),
                   ", ".join(sorted({i.category for i in items})),
                   len({i.trade_date for i in items if i.trade_date})]
                  for sym, items in busiest],
            empty_text="No alert activity in this period.",
        ))

        # --- repeat 52-week breakouts ---------------------------------------
        breakout_days: dict[str, set] = defaultdict(set)
        for a in alerts:
            if a.category == "week52_high" and a.trade_date:
                breakout_days[a.entity].add(a.trade_date)
        repeats = sorted(((s, d) for s, d in breakout_days.items()
                          if len(d) > 1), key=lambda kv: len(kv[1]),
                         reverse=True)[:15]
        rb.add(Section(
            heading="Persistent 52-week high makers",
            note=("Names printing fresh 52-week highs on multiple days are the "
                  "clearest momentum signal in this dataset."),
            columns=["Symbol", "Days at new high"],
            numeric_cols={1},
            rows=[[sym, len(days)] for sym, days in repeats],
            empty_text="No symbol made new 52-week highs on more than one day.",
        ))

        # --- deal flow -------------------------------------------------------
        by_deal_symbol: dict[str, float] = defaultdict(float)
        for d in deals:
            by_deal_symbol[d.symbol] += d.value_cr or 0
        top_deals = sorted(by_deal_symbol.items(), key=lambda kv: kv[1],
                           reverse=True)[:15]
        rb.add(Section(
            heading="Cumulative deal value by symbol",
            columns=["Symbol", "Total deal value (Rs cr)", "Deals"],
            numeric_cols={1, 2},
            rows=[[sym, round(val, 1),
                   sum(1 for d in deals if d.symbol == sym)]
                  for sym, val in top_deals],
            empty_text="No deals recorded in this period.",
        ))

        # --- most active clients --------------------------------------------
        by_client: dict[str, float] = defaultdict(float)
        for d in deals:
            if d.client_name:
                by_client[d.client_name] += d.value_cr or 0
        top_clients = sorted(by_client.items(), key=lambda kv: kv[1],
                             reverse=True)[:12]
        rb.add(Section(
            heading="Most active deal participants",
            columns=["Client", "Total value (Rs cr)", "Deals"],
            numeric_cols={1, 2},
            rows=[[name[:52], round(val, 1),
                   sum(1 for d in deals if d.client_name == name)]
                  for name, val in top_clients],
            empty_text="No named participants recorded.",
        ))

        summary = self._summary(period, alerts, sev, by_cat, busiest, deal_cr,
                                prior_deal_cr)
        md_path = html_path = None
        if write:
            md_path, html_path = rb.write(self.slug, period.end)

        return ReportOutput(title=title, markdown=rb.to_markdown(),
                            summary=summary, md_path=md_path,
                            html_path=html_path)

    def _summary(self, period, alerts, sev, by_cat, busiest, deal_cr,
                 prior_deal_cr) -> str:
        lines = [f"NSE {self.noun} — {period.label}", ""]
        lines.append(f"Alerts: {len(alerts)} "
                     f"({sev.get('critical', 0)} critical)")
        if by_cat:
            lines.append("Mix: " + ", ".join(f"{c} {n}"
                                             for c, n in by_cat.most_common(5)))
        lines.append(f"Deal value: {_delta_text(deal_cr, prior_deal_cr, ' cr')}")
        if busiest:
            lines.append("")
            lines.append("Most active:")
            for sym, items in busiest[:5]:
                lines.append(f"  {sym} — {len(items)} alerts")
        return "\n".join(lines)
